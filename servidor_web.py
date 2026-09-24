"""
RNDC - SERVIDOR WEB LOCAL
==========================
Corre una página web en tu propia computadora (no sale a internet)
donde puedes:
 1. Iniciar sesión con tu usuario/contraseña de ESTE programa (distintos
    a los del RNDC), verificados contra tu tabla de Google Sheets.
 2. Llenar un formulario con los datos del viaje (reemplaza el Bloc de
    Notas).
 3. Ver el progreso en vivo mientras el navegador hace todo el trabajo.

CÓMO USARLO:
    1. pip install flask requests
    2. python servidor_web.py
    3. Abre en tu navegador: http://localhost:5000
"""

import sys
import os

# Cuando el programa corre empaquetado como .exe SIN ventana de consola,
# sys.stdout queda en None, y cualquier print() rompería el programa.
# En ese caso, se redirige todo a un archivo de registro en vez de a la
# consola (que no existe), para que el programa siga funcionando y quede
# un archivo consultable si algo falla.
if getattr(sys, "frozen", False) and sys.stdout is None:
    _carpeta_exe = os.path.dirname(sys.executable)
    _archivo_log = os.path.join(_carpeta_exe, "log_servidor.txt")
    _log_f = open(_archivo_log, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_f
    sys.stderr = _log_f

from flask import Flask, request, session, redirect, url_for, render_template, jsonify
import threading
import subprocess
import json
import time
import requests
from datetime import timedelta

import rndc_core
from backend.config import (
    RUTAS_SIN_ACTIVIDAD, URL_HOJA_USUARIOS, VERSION_APP, URL_HOJA_VERSION,
    URL_WEBHOOK_SHEETS, OBSERVACIONES_POR_DEFECTO,
)
from backend.historial import (
    registrar_historial, buscar_consecutivo_hoy, sugerir_conductor_para_placa,
    calcular_estadisticas,
)
from backend.sheets import registrar_en_sheets, revisar_actualizacion, validar_usuario_app
from backend import actualizador


def resource_path(nombre_carpeta):
    """Ruta a una carpeta de recursos (templates/, static/). Con el
    lanzador nuevo, servidor_web.py SIEMPRE corre desde la carpeta de
    código externa y actualizable (ver lanzador.py/backend/actualizador.py),
    nunca desde adentro sellado del .exe — así que basta con mirar al
    lado de este mismo archivo, tanto en modo normal como empaquetado."""
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, nombre_carpeta)


app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
)
def _obtener_secret_key():
    """La clave que Flask usa para "sellar" las sesiones de cada persona
    (para que nadie pueda falsificar su propia cookie de "ya inicié
    sesión"). Antes estaba escrita fija en este archivo — pero como el
    código ahora es público en GitHub, cualquiera podía verla ahí
    mismo. En vez de eso, cada computadora genera la SUYA propia la
    primera vez que corre, y la guarda por fuera de la carpeta de
    código (que el botón "Actualizar" reemplaza por completo), para
    que nunca quede visible en el repositorio y sobreviva a las
    actualizaciones."""
    import secrets
    ruta = os.path.join(actualizador.carpeta_datos_appdata(), "flask_secret.txt")
    if os.path.exists(ruta):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                clave_guardada = f.read().strip()
            if clave_guardada:
                return clave_guardada
        except Exception:
            pass
    clave_nueva = secrets.token_hex(32)
    try:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(clave_nueva)
    except Exception:
        pass  # si por algo no se puede guardar, se usa igual solo para esta sesión
    return clave_nueva


app.secret_key = _obtener_secret_key()
app.permanent_session_lifetime = timedelta(minutes=30)


@app.before_request
def revisar_inactividad():
    if "usuario_app" not in session:
        return
    ahora = time.time()
    ultima = session.get("ultima_actividad", ahora)

    hay_trabajos_activos = any(t["estado"] in ("pendiente", "corriendo") for t in trabajos)
    if (ahora - ultima) > app.permanent_session_lifetime.total_seconds() and not hay_trabajos_activos:
        session.clear()
        return redirect(url_for("login"))

    if request.endpoint not in RUTAS_SIN_ACTIVIDAD:
        session["ultima_actividad"] = ahora
        session.permanent = True


@app.after_request
def evitar_cache_del_navegador(respuesta):
    """Como el botón "Actualizar" puede cambiar el HTML/JS de la app
    mientras sigue corriendo el mismo servidor en localhost:5000, el
    navegador interno (WebView2/Edge) NO debe guardar en caché estas
    páginas — si lo hiciera, después de actualizar y reiniciar seguiría
    mostrando la versión vieja (con los bugs viejos) aunque el archivo
    en disco ya esté corregido, y ni cerrar sesión ni nada del lado del
    servidor lo arreglaría, porque el caché vive en el navegador."""
    respuesta.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    respuesta.headers["Pragma"] = "no-cache"
    return respuesta


# ============================================================
# FILA DE TRABAJOS: en vez de bloquear mientras corre un viaje,
# cada "Ejecutar" agrega un trabajo a esta fila. Un hilo dedicado los
# va sacando y procesando uno detrás de otro, automáticamente. Así
# puedes volver al formulario y meter el siguiente viaje sin esperar
# a que termine el anterior.
# ============================================================
trabajos = []          # lista de dicts: id, descripcion, estado, log, ok
siguiente_id_trabajo = 1
candado = threading.Lock()

# Recuerda los últimos datos que se escribieron en el formulario, para que
# si algo falla no haya que volver a escribir todo desde cero.
ultimo_formulario = {}


def agregar_trabajo(v, usuario, password, descripcion):
    global siguiente_id_trabajo
    with candado:
        trabajo = {
            "id": siguiente_id_trabajo,
            "descripcion": descripcion,
            "estado": "pendiente",  # pendiente -> corriendo -> terminado
            "log": [],
            "ok": None,
            "v": v,
            "usuario": usuario,
            "password": password,
            "inicio": None,
            "archivos": [],
        }
        trabajos.append(trabajo)
        siguiente_id_trabajo += 1
        posicion = sum(1 for t in trabajos if t["estado"] in ("pendiente", "corriendo"))
    return trabajo["id"], posicion


def trabajador_de_fondo():
    """Corre para siempre en un hilo aparte: va tomando trabajos
    pendientes de la fila y los procesa uno a la vez, en orden."""
    while True:
        trabajo = None
        with candado:
            for t in trabajos:
                if t["estado"] == "pendiente":
                    trabajo = t
                    t["estado"] = "corriendo"
                    t["inicio"] = time.time()
                    break
        if trabajo is None:
            time.sleep(1)
            continue

        try:
            def log_trabajo(mensaje, _t=trabajo):
                # print() puede fallar con UnicodeEncodeError en el .exe
                # empacado con --noconsole cuando el mensaje trae un emoji
                # (✅⚠️❌, que usa TODO el programa) — la salida estándar ahí
                # usa una codificación vieja (cp1252) que no los entiende.
                # Sin este try/except, ese error pasaba ANTES de guardar el
                # mensaje en el log que ve la pantalla, y además mataba este
                # hilo de fondo por completo (siendo un "while True" sin
                # nada que lo reinicie) — por eso un viaje se quedaba
                # congelado para siempre justo después del primer mensaje
                # con emoji, y ningún viaje más volvía a procesarse en toda
                # esa sesión de la app, sin ningún error visible.
                try:
                    print(mensaje)
                except Exception:
                    pass
                with candado:
                    _t["log"].append(mensaje)

            try:
                if trabajo["v"].get("TipoViaje") == "Cola":
                    resultado = rndc_core.ejecutar_cola(
                        trabajo["v"]["_viajes_cola"], trabajo["usuario"], trabajo["password"], log_trabajo
                    )
                elif (
                    trabajo["v"].get("UsarAPI")
                    and trabajo["v"].get("TipoViaje") in ("Normal", "IdaYRegreso")
                    and not trabajo["v"].get("Multiparada")
                    and not trabajo["v"].get("Cedula_Conductor2")
                ):
                    # Camino nuevo (Web Service) -- cubre viajes Normales e
                    # Ida y Regreso por ahora. Multiparada y segundo
                    # conductor todavía van por Selenium.
                    log_trabajo("    (usando el Web Service en vez del navegador)")
                    resultado = rndc_core.ejecutar_viaje_api(
                        trabajo["v"], trabajo["usuario"], trabajo["password"], log_trabajo
                    )
                else:
                    resultado = rndc_core.ejecutar_automatizacion(
                        trabajo["v"], trabajo["usuario"], trabajo["password"], log_trabajo
                    )
            except Exception as e:
                log_trabajo(f"❌ Error inesperado: {rndc_core.traducir_error(e)}")
                resultado = {"ok": False}

            with candado:
                trabajo["estado"] = "terminado"
                trabajo["ok"] = resultado.get("ok")
                trabajo["archivos"] = resultado.get("archivos", [])

            duracion = time.time() - trabajo["inicio"]

            if trabajo["v"].get("TipoViaje") == "Cola":
                # Se registra cada viaje de la cola por separado (con su
                # propia placa/conductor), no como un solo renglón genérico.
                viajes_cola = trabajo["v"].get("_viajes_cola", [])
                resultados_cola = resultado.get("resultados", [])
                duracion_por_viaje = duracion / len(viajes_cola) if viajes_cola else duracion
                for i, v_item in enumerate(viajes_cola):
                    resultado_item = resultados_cola[i] if i < len(resultados_cola) else {"ok": resultado.get("ok")}
                    ok_item = resultado_item.get("ok")
                    desc_item = f"Consecutivo {v_item.get('Consecutivo') or '?'} ({v_item.get('Origen', '?')} → {v_item.get('Destino', '?')})"
                    registrar_historial(
                        desc_item, ok_item, duracion_por_viaje,
                        placa=v_item.get("Placa"), cedula_conductor=v_item.get("Cedula_Conductor"),
                    )
                    if ok_item:
                        registrar_en_sheets(v_item, resultado_item, log_trabajo)
            else:
                registrar_historial(
                    trabajo["descripcion"], resultado.get("ok"), duracion,
                    placa=trabajo["v"].get("Placa"), cedula_conductor=trabajo["v"].get("Cedula_Conductor"),
                )
                if resultado.get("ok"):
                    registrar_en_sheets(trabajo["v"], resultado, log_trabajo)
        except Exception:
            # Red de seguridad final: bajo NINGUNA circunstancia este hilo
            # debe morir — si muriera, ningún viaje más se procesaría en
            # el resto de la sesión de la app (se quedarían todos
            # "Esperando turno en la fila..." para siempre), sin ningún
            # aviso visible de que eso pasó. Mejor marcar este trabajo
            # como fallido y seguir con el siguiente.
            with candado:
                trabajo["estado"] = "terminado"
                trabajo["ok"] = False


threading.Thread(target=trabajador_de_fondo, daemon=True).start()


# Las plantillas HTML viven ahora en templates/ (login.html, formulario.html,
# progreso.html, cola.html) y se cargan con render_template().


# =========================================================================
# RUTAS
# =========================================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        password = request.form.get("password", "").strip()
        ok, mensaje_error, rndc_vinculado = validar_usuario_app(usuario, password)
        if ok:
            session["usuario_app"] = usuario
            if rndc_vinculado:
                session["usuario_rndc_vinculado"] = rndc_vinculado[0]
                session["password_rndc_vinculado"] = rndc_vinculado[1]
            else:
                session.pop("usuario_rndc_vinculado", None)
                session.pop("password_rndc_vinculado", None)
            return redirect(url_for("formulario"))
        else:
            error = mensaje_error
    return render_template('login.html', error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
def formulario():
    if "usuario_app" not in session:
        return redirect(url_for("login"))
    sedes_reales = []
    try:
        with open(rndc_core.ARCHIVO_SEDES, "r", encoding="utf-8") as f:
            sedes_reales = json.load(f)
    except Exception:
        pass
    conductores_reales = {}
    try:
        with open(rndc_core.ARCHIVO_CONDUCTORES, "r", encoding="utf-8") as f:
            conductores_reales = json.load(f)
    except Exception:
        pass
    return render_template(
        'formulario.html', v=ultimo_formulario,
        OBSERVACIONES_POR_DEFECTO=OBSERVACIONES_POR_DEFECTO,
        sedes_reales=sedes_reales,
        conductores_reales=conductores_reales,
        stats=calcular_estadisticas(),
        rndc_vinculado=session.get("usuario_rndc_vinculado"),
        actualizacion=revisar_actualizacion(),
        VERSION_APP=VERSION_APP,
    )


@app.route("/actualizar_sedes", methods=["POST"])
def actualizar_sedes():
    if "usuario_app" not in session:
        return jsonify({"ok": False, "error": "No autenticado"}), 401
    usuario_rndc = session.get("usuario_rndc_vinculado") or request.form.get("usuario_rndc", "").strip()
    password_rndc = session.get("password_rndc_vinculado") or request.form.get("password_rndc", "").strip()
    if not usuario_rndc or not password_rndc:
        return jsonify({"ok": False, "error": "Faltan tu usuario y contraseña del RNDC."})
    mensajes = []
    try:
        opciones = rndc_core.obtener_lista_sedes_empresa(usuario_rndc, password_rndc, mensajes.append)
        return jsonify({"ok": True, "cantidad": len(opciones)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "log": mensajes})


@app.route("/actualizar_conductores", methods=["POST"])
def actualizar_conductores():
    if "usuario_app" not in session:
        return jsonify({"ok": False, "error": "No autenticado"}), 401
    usuario_rndc = session.get("usuario_rndc_vinculado") or request.form.get("usuario_rndc", "").strip()
    password_rndc = session.get("password_rndc_vinculado") or request.form.get("password_rndc", "").strip()
    if not usuario_rndc or not password_rndc:
        return jsonify({"ok": False, "error": "Faltan tu usuario y contraseña del RNDC."})
    mensajes = []
    try:
        conductores = rndc_core.obtener_lista_conductores_empresa(usuario_rndc, password_rndc, mensajes.append)
        return jsonify({"ok": True, "cantidad": len(conductores), "log": mensajes})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "log": mensajes})


@app.route("/verificar_tercero", methods=["POST"])
def verificar_tercero_route():
    if "usuario_app" not in session:
        return jsonify({"ok": False, "error": "No autenticado"}), 401
    numero_id = request.form.get("numero_id", "").strip()
    tipo_id = request.form.get("tipo_id", "C").strip() or "C"
    usuario_rndc = session.get("usuario_rndc_vinculado") or request.form.get("usuario_rndc", "").strip()
    password_rndc = session.get("password_rndc_vinculado") or request.form.get("password_rndc", "").strip()
    if not numero_id:
        return jsonify({"ok": False, "error": "Escribe una cédula/NIT primero."})
    if not usuario_rndc or not password_rndc:
        return jsonify({"ok": False, "error": "Faltan tu usuario y contraseña del RNDC (más abajo)."})
    try:
        resultado = rndc_core.verificar_tercero(tipo_id, numero_id, usuario_rndc, password_rndc, print)
        return jsonify({"ok": True, **resultado})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/ejecutar", methods=["POST"])
def ejecutar():
    if "usuario_app" not in session:
        return redirect(url_for("login"))

    import json

    f = request.form
    tipo_viaje = f.get("TipoViaje", "Normal")
    multiparada = tipo_viaje == "Multiparada"
    ida_y_regreso = tipo_viaje == "IdaYRegreso"
    es_cola = tipo_viaje == "Cola"

    paradas = []
    if multiparada or ida_y_regreso:
        try:
            paradas = json.loads(f.get("ParadasJSON", "[]"))
        except Exception:
            paradas = []

    cola_viajes = []
    if es_cola:
        try:
            cola_viajes = json.loads(f.get("ColaJSON", "[]"))
        except Exception:
            cola_viajes = []

    def convertir_fecha_html_a_rndc(valor):
        """El campo de calendario del formulario da 'AAAA-MM-DD'. El RNDC
        necesita 'DD/MM/AAAA'. Si está vacío, se deja vacío (se usa la
        fecha por defecto)."""
        valor = (valor or "").strip()
        if not valor:
            return ""
        try:
            anio, mes, dia = valor.split("-")
            return f"{dia}/{mes}/{anio}"
        except Exception:
            return ""

    fecha_expedicion = convertir_fecha_html_a_rndc(f.get("FechaExpedicionFecha"))
    fecha_pago = convertir_fecha_html_a_rndc(f.get("FechaPagoFecha"))

    # Campos GLOBALES: se usan igual para todos los viajes (también para
    # cada uno de la Cola). Los que varían por viaje en modo Cola
    # (Consecutivo, Placa, Origen, Destino, Producto, Peso, Cédula del
    # conductor, Flete, Anticipo) se completan por separado más abajo.
    v = {
        "Consecutivo": f.get("Consecutivo", "").strip(),
        "Origen": f.get("Origen", "").strip(),
        "Destino": f.get("Destino", "").strip(),
        "Producto": f.get("Producto", "").strip(),
        "Peso": f.get("Peso", "").strip(),
        "TipoViaje": tipo_viaje,
        "Multiparada": multiparada,
        "IdaYRegreso": ida_y_regreso,
        "Paradas": paradas,
        "Cedula_Titular": f.get("Cedula_Titular", "").strip(),
        "Nombre_Titular": f.get("Nombre_Titular", "").strip(),
        "Apellido1_Titular": f.get("Apellido1_Titular", "").strip(),
        "Apellido2_Titular": f.get("Apellido2_Titular", "").strip(),
        "Municipio_Titular": f.get("Municipio_Titular", "").strip(),
        "Placa": f.get("Placa", "").strip(),
        "Placa_Remolque": f.get("Placa_Remolque", "").strip(),
        "Cedula_Conductor": f.get("Cedula_Conductor", "").strip(),
        "Cedula_Conductor2": f.get("Cedula_Conductor2", "").strip(),
        "Nombre_Conductor": f.get("Nombre_Conductor", "").strip(),
        "Apellido1_Conductor": f.get("Apellido1_Conductor", "").strip(),
        "Apellido2_Conductor": f.get("Apellido2_Conductor", "").strip(),
        "Municipio_Conductor": f.get("Municipio_Conductor", "").strip(),
        "Flete": f.get("Flete", "").strip(),
        "Anticipo": f.get("Anticipo", "").strip(),
        "Cliente": f.get("Cliente", "").strip(),
        "TipoID_Remitente_Cliente": f.get("TipoID_Remitente_Cliente", "").strip(),
        "NIT_Remitente_Cliente": f.get("NIT_Remitente_Cliente", "").strip(),
        "TipoID_Destinatario_Cliente": f.get("TipoID_Destinatario_Cliente", "").strip(),
        "NIT_Destinatario_Cliente": f.get("NIT_Destinatario_Cliente", "").strip(),
        "Observaciones": f.get("Observaciones", "").strip(),
        "FechaExpedicion": fecha_expedicion,
        "FechaPago": fecha_pago,
        "Invisible": f.get("Invisible") == "on",
        "ModoPractica": f.get("ModoPractica") == "on",
        "UsarAPI": f.get("UsarAPI") == "on",
        # Quién de la empresa hizo este viaje (con qué usuario inició
        # sesión en la app) — no es lo mismo que usuario_rndc, que es la
        # cuenta del RNDC en sí (varias personas pueden compartir la
        # misma cuenta vinculada del RNDC). Viaja junto con "v" hasta el
        # registro en Google Sheets al final, y también lo heredan los
        # viajes de la cola de Récord (todos comparten este mismo dato).
        "usuario_app": session.get("usuario_app", ""),
    }
    usuario_rndc = session.get("usuario_rndc_vinculado") or f.get("usuario_rndc", "").strip()
    password_rndc = session.get("password_rndc_vinculado") or f.get("password_rndc", "").strip()

    # Construir la lista de viajes del Récord: TODOS comparten el mismo
    # vehículo/conductor/titular/cliente (los campos generales de arriba,
    # ya copiados en "v"), y cada uno trae sus propios Consecutivo/Fecha/
    # Origen/Destino/Producto/Peso/Flete/Anticipo/Observaciones.
    lista_viajes_cola = []
    for item in cola_viajes:
        v_item = dict(v)
        fecha_item = convertir_fecha_html_a_rndc(item.get("fecha"))
        v_item.update({
            "TipoViaje": "Normal",
            "Multiparada": False,
            "IdaYRegreso": False,
            "Paradas": [],
            "Consecutivo": (item.get("consecutivo") or "").strip(),
            "Origen": (item.get("origen") or "").strip(),
            "Destino": (item.get("destino") or "").strip(),
            "Producto": (item.get("producto") or "").strip(),
            "Peso": (item.get("peso") or "").strip(),
            "Flete": (item.get("flete") or "").strip(),
            "Anticipo": (item.get("anticipo") or "").strip(),
            "TipoID_Remitente_Cliente": (item.get("tipoid_rem") or "").strip(),
            "NIT_Remitente_Cliente": (item.get("nit_rem") or "").strip(),
            "TipoID_Destinatario_Cliente": (item.get("tipoid_des") or "").strip(),
            "NIT_Destinatario_Cliente": (item.get("nit_des") or "").strip(),
            "Observaciones": (item.get("observaciones") or "").strip() or v["Observaciones"],
            "FechaExpedicion": fecha_item or v["FechaExpedicion"],
        })
        lista_viajes_cola.append(v_item)

    # Se recuerdan estos datos para pre-cargar el formulario la próxima
    # vez (por si algo falla y hay que volver a intentar). La contraseña
    # del RNDC NUNCA se guarda aquí, por seguridad.
    global ultimo_formulario
    ultimo_formulario = dict(v)
    ultimo_formulario["usuario_rndc"] = usuario_rndc
    ultimo_formulario["ParadasJSON"] = f.get("ParadasJSON", "[]")
    ultimo_formulario["ColaJSON"] = f.get("ColaJSON", "[]")

    if not usuario_rndc or not password_rndc:
        return "Faltan tu usuario y contraseña del RNDC.", 400

    if es_cola:
        v["_viajes_cola"] = lista_viajes_cola
        descripcion = f"Cola de {len(lista_viajes_cola)} viaje(s)"
    else:
        descripcion = f"Consecutivo {v.get('Consecutivo') or '?'} ({v.get('Origen', '?')} → {v.get('Destino', '?')})"

    id_trabajo, posicion = agregar_trabajo(v, usuario_rndc, password_rndc, descripcion)
    mensaje_inicial = (
        f"Agregado a la fila (posición {posicion})." if posicion > 1
        else "Arrancando ahora mismo..."
    )

    return render_template('progreso.html', id_trabajo=id_trabajo, mensaje_inicial=mensaje_inicial)


@app.route("/reintentar/<int:id_trabajo>")
def reintentar_trabajo(id_trabajo):
    """Retoma un viaje ya TERMINADO (exitoso o con error) de la fila,
    llevando a la persona de vuelta al formulario con todos sus datos
    ya puestos — para poder corregir lo que haga falta y darle
    "Ejecutar" de nuevo, en vez de tener que volver a escribir todo
    desde cero. Solo funciona con trabajos que ya terminaron (no tiene
    sentido "corregir" uno que sigue corriendo o esperando turno)."""
    if "usuario_app" not in session:
        return redirect(url_for("login"))
    with candado:
        trabajo = next((t for t in trabajos if t["id"] == id_trabajo), None)
    if not trabajo or trabajo["estado"] != "terminado":
        return redirect(url_for("ver_cola"))

    global ultimo_formulario
    nueva_v = dict(trabajo["v"])
    # El formulario reconstruye las filas de Multiparada/Ida y Regreso y
    # de Récord a partir de estos dos campos en formato texto (JSON) —
    # "Paradas"/"_viajes_cola" son la versión ya lista para Python que
    # se guardó junto con el trabajo, así que solo hace falta devolverla
    # a texto para que la página la vuelva a leer igual que la primera vez.
    if nueva_v.get("Paradas"):
        nueva_v["ParadasJSON"] = json.dumps(nueva_v["Paradas"])
    if nueva_v.get("_viajes_cola"):
        nueva_v["ColaJSON"] = json.dumps(nueva_v["_viajes_cola"])
    ultimo_formulario = nueva_v
    return redirect(url_for("formulario"))


@app.route("/cola")
def ver_cola():
    """Página con TODA la fila de trabajos (pendientes, corriendo y
    terminados), para ver de un vistazo cómo va todo."""
    if "usuario_app" not in session:
        return redirect(url_for("login"))
    return render_template('cola.html')


@app.route("/progreso")
def progreso_trabajo():
    """Ver el detalle/log de un trabajo puntual de la fila (al hacerle
    clic desde la lista)."""
    if "usuario_app" not in session:
        return redirect(url_for("login"))
    id_trabajo = request.args.get("id", type=int)
    with candado:
        trabajo = next((t for t in trabajos if t["id"] == id_trabajo), None)
    if not trabajo:
        return redirect(url_for("ver_cola"))
    mensajes_iniciales = {
        "pendiente": "Esperando turno en la fila...",
        "corriendo": "Ejecutando...",
        "terminado": "Terminado",
    }
    return render_template(
        'progreso.html', id_trabajo=id_trabajo,
        mensaje_inicial=mensajes_iniciales.get(trabajo["estado"], "..."),
    )


@app.route("/estado")
def estado():
    id_trabajo = request.args.get("id", type=int)
    with candado:
        lista = [
            {"id": t["id"], "descripcion": t["descripcion"], "estado": t["estado"], "ok": t["ok"]}
            for t in trabajos
        ]
        if id_trabajo:
            trabajo = next((t for t in trabajos if t["id"] == id_trabajo), None)
            log_trabajo = list(trabajo["log"]) if trabajo else []
            estado_trabajo = trabajo["estado"] if trabajo else "desconocido"
            archivos = list(trabajo.get("archivos", [])) if trabajo else []
        else:
            log_trabajo = []
            estado_trabajo = None
            archivos = []
    return jsonify({
        "trabajos": lista,
        "log": log_trabajo,
        "estado_trabajo": estado_trabajo,
        "terminado": estado_trabajo == "terminado",
        "archivos": archivos,
    })


@app.route("/verificar_consecutivo")
def verificar_consecutivo_route():
    if "usuario_app" not in session:
        return jsonify({"repetido": False}), 401
    consecutivo = request.args.get("consecutivo", "").strip()
    hora = buscar_consecutivo_hoy(consecutivo)
    return jsonify({"repetido": hora is not None, "hora": hora})


@app.route("/sugerir_conductor")
def sugerir_conductor_route():
    if "usuario_app" not in session:
        return jsonify({"conductor": None}), 401
    placa = request.args.get("placa", "").strip()
    conductor = sugerir_conductor_para_placa(placa)
    return jsonify({"conductor": conductor})


@app.route("/abrir_carpeta", methods=["POST"])
def abrir_carpeta():
    """Abre el Explorador de Windows con el PDF ya seleccionado, listo
    para que el usuario lo arrastre él mismo a donde quiera (correo,
    chat, otra carpeta) usando el propio Explorador, que sí soporta
    arrastrar de forma confiable (a diferencia del navegador)."""
    if "usuario_app" not in session:
        return jsonify({"ok": False, "error": "No autenticado"}), 401
    nombre = request.form.get("archivo", "")
    nombre_seguro = os.path.basename(nombre)
    if not nombre_seguro.lower().endswith(".pdf"):
        return jsonify({"ok": False, "error": "Solo se pueden abrir archivos PDF."})
    ruta = os.path.join(rndc_core.CARPETA_DESCARGAS, nombre_seguro)
    if not os.path.isfile(ruta):
        return jsonify({"ok": False, "error": "No se encontró ese archivo en Descargas."})
    try:
        import subprocess
        subprocess.Popen(f'explorer /select,"{ruta}"')
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/ver_pdf")
def ver_pdf():
    """Sirve un PDF ya descargado, SOLO desde la carpeta de descargas real
    y SOLO si termina en .pdf, para no arriesgarse a mostrar cualquier
    archivo de la computadora."""
    if "usuario_app" not in session:
        return redirect(url_for("login"))
    nombre = request.args.get("archivo", "")
    # Se usa solo el nombre del archivo (sin carpetas) para evitar que
    # alguien intente pedir un archivo de otro lugar de la computadora.
    nombre_seguro = os.path.basename(nombre)
    if not nombre_seguro.lower().endswith(".pdf"):
        return "Solo se pueden ver archivos PDF.", 400
    ruta = os.path.join(rndc_core.CARPETA_DESCARGAS, nombre_seguro)
    if not os.path.isfile(ruta):
        return "No se encontró ese archivo.", 404
    from flask import send_file
    return send_file(ruta, mimetype="application/pdf")


@app.route("/estado_actualizacion", methods=["GET"])
def estado_actualizacion():
    try:
        return jsonify({
            "token_configurado": bool(actualizador.leer_token()),
            "version": actualizador.leer_version_actual(),
        })
    except Exception as e:
        return jsonify({"token_configurado": False, "version": {}, "error": str(e)})


@app.route("/guardar_token_actualizacion", methods=["POST"])
def guardar_token_actualizacion():
    if "usuario_app" not in session:
        return jsonify({"ok": False, "mensaje": "Sesión no válida."}), 403
    try:
        token = (request.form.get("token") or "").strip()
        if not token:
            return jsonify({"ok": False, "mensaje": "El token no puede estar vacío."})
        actualizador.guardar_token(token)
        return jsonify({"ok": True, "mensaje": "Token guardado en esta computadora."})
    except Exception as e:
        return jsonify({"ok": False, "mensaje": f"Error inesperado guardando el token: {e}"})


@app.route("/actualizar_codigo", methods=["POST"])
def actualizar_codigo():
    if "usuario_app" not in session:
        return jsonify({"ok": False, "mensaje": "Sesión no válida."}), 403
    try:
        ok, mensaje = actualizador.actualizar_desde_github()
        return jsonify({"ok": ok, "mensaje": mensaje})
    except Exception as e:
        import traceback
        detalle = traceback.format_exc()[-500:]
        return jsonify({"ok": False, "mensaje": f"Error inesperado actualizando: {e}\n{detalle}"})


def iniciar_servidor_flask_en_hilo():
    """Arranca el servidor Flask en un hilo aparte y espera de verdad a
    que responda antes de devolver el control (en vez de una pausa fija,
    que a veces no alcanza — sobre todo la primera vez que corre en una
    computadora, cuando el antivirus revisa más a fondo un programa
    nuevo). Deliberadamente NO toca nada de la ventana (pywebview): eso
    vive aparte, en lanzador.py, con sus propios imports "estáticos" —
    así, aunque este archivo se cargue desde una carpeta actualizable,
    la parte de la ventana (la única que alguna vez dio problemas al
    cargarse así) nunca se ve afectada.

    Devuelve (ok, mensaje_error_o_None)."""
    error_arranque_flask = []

    def iniciar_servidor_flask():
        try:
            app.run(host="127.0.0.1", port=5000, debug=False, threaded=True, use_reloader=False)
        except Exception as e:
            error_arranque_flask.append(str(e))

    hilo_flask = threading.Thread(target=iniciar_servidor_flask, daemon=True)
    hilo_flask.start()

    servidor_listo = False
    for intento in range(60):  # hasta 30 segundos de margen (60 x 0.5s)
        if error_arranque_flask:
            break
        try:
            requests.get("http://127.0.0.1:5000/login", timeout=1)
            servidor_listo = True
            break
        except Exception:
            time.sleep(0.5)

    if servidor_listo:
        return True, None

    mensaje_error = (
        "El programa no pudo iniciar su servidor interno, así que la "
        "ventana habría quedado en blanco con un error de conexión.\n\n"
    )
    if error_arranque_flask:
        texto_error = error_arranque_flask[0]
        if "Address already in use" in texto_error or "10048" in texto_error or "10013" in texto_error:
            mensaje_error += (
                "La causa más probable: ya hay OTRA copia de Automatización RNDC "
                "abierta (aunque no se vea en pantalla, puede seguir corriendo de "
                "fondo). Abre el Administrador de tareas (Ctrl+Shift+Esc), busca "
                "'AutomatizacionRNDC' o 'Innova', y termina esa tarea. Luego vuelve "
                "a abrir el programa."
            )
        else:
            mensaje_error += f"Motivo técnico: {texto_error}"
        return False, mensaje_error

    mensaje_error += (
        "El servidor tardó más de 30 segundos en responder (posiblemente el "
        "antivirus lo está revisando a fondo, sobre todo la primera vez que "
        "se usa en esta computadora)."
    )
    # Este caso (solo lentitud, sin un error real) no se trata como fatal:
    # quien llame puede decidir abrir la ventana de todas formas, por si
    # el servidor arranca en los próximos segundos mientras esta carga.
    return None, mensaje_error


if __name__ == "__main__":
    # Esto es solo para poder probar "python servidor_web.py" sueltos,
    # en desarrollo. El .exe de verdad usa lanzador.py como punto de
    # entrada (ver ese archivo para el arranque real de la ventana).
    servidor_listo, mensaje_error = iniciar_servidor_flask_en_hilo()

    if servidor_listo is False:
        try:
            print("AVISO:", mensaje_error)
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, mensaje_error, "Automatización RNDC - No se pudo iniciar", 0x10)
        except Exception:
            pass
        sys.exit(1)
    elif servidor_listo is None:
        try:
            print("AVISO:", mensaje_error)
        except Exception:
            pass

    try:
        import webview
        webview.create_window(
            "Innova - Automatización RNDC",
            "http://localhost:5000",
            width=1250, height=850, min_size=(950, 650),
        )
        webview.start()
        os._exit(0)
    except ImportError:
        # Si pywebview no está instalado (ej: corriendo con
        # "python servidor_web.py" sin haber instalado requirements.txt
        # completo), se cae de vuelta al navegador normal.
        import webbrowser
        webbrowser.open("http://localhost:5000")
        print("Servidor iniciado. Abre en tu navegador: http://localhost:5000")
        while True:
            time.sleep(3600)
