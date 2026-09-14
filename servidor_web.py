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


def resource_path(nombre_carpeta):
    """Ruta a una carpeta de recursos empaquetados (templates/, static/).
    Cuando el programa corre como .exe de un solo archivo (--onefile),
    PyInstaller extrae esos recursos a una carpeta TEMPORAL distinta en
    cada arranque (sys._MEIPASS); en modo normal (python servidor_web.py)
    es simplemente la carpeta donde está este archivo."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, nombre_carpeta)


app = Flask(
    __name__,
    template_folder=resource_path("templates"),
    static_folder=resource_path("static"),
)
app.secret_key = "cambia-esto-por-algo-secreto"  # cualquier texto, solo debe ser secreto
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

        def log_trabajo(mensaje, _t=trabajo):
            print(mensaje)
            with candado:
                _t["log"].append(mensaje)

        try:
            if trabajo["v"].get("TipoViaje") == "Cola":
                resultado = rndc_core.ejecutar_cola(
                    trabajo["v"]["_viajes_cola"], trabajo["usuario"], trabajo["password"], log_trabajo
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


if __name__ == "__main__":
    error_arranque_flask = []  # lista en vez de variable suelta: así el hilo puede "avisarle" al programa principal

    def iniciar_servidor_flask():
        try:
            app.run(host="127.0.0.1", port=5000, debug=False, threaded=True, use_reloader=False)
        except Exception as e:
            # Si esto falla (el caso típico: el puerto 5000 ya está ocupado
            # por otra copia del programa que quedó abierta de fondo), el
            # hilo se moriría en silencio y quien usa el programa solo
            # vería una pantalla de "no se puede conectar" sin ninguna
            # pista de qué pasó (más aún con --noconsole). Se guarda el
            # motivo aquí para poder mostrarlo de verdad más abajo.
            error_arranque_flask.append(str(e))

    hilo_flask = threading.Thread(target=iniciar_servidor_flask, daemon=True)
    hilo_flask.start()

    # Se espera de VERDAD a que el servidor responda antes de abrir la
    # ventana, en vez de una pausa fija de 1 segundo (que a veces no
    # alcanza — sobre todo la primera vez que corre en una computadora,
    # cuando el antivirus suele revisar más a fondo un programa nuevo y
    # todo tarda más en arrancar). Si abriéramos la ventana antes de
    # tiempo, se queda en negro porque intenta cargar una página que
    # todavía no existe.
    servidor_listo = False
    for intento in range(60):  # hasta 30 segundos de margen (60 x 0.5s)
        if error_arranque_flask:
            break  # no tiene sentido seguir esperando si el servidor ya truncó el arranque
        try:
            requests.get("http://127.0.0.1:5000/login", timeout=1)
            servidor_listo = True
            break
        except Exception:
            time.sleep(0.5)

    if not servidor_listo:
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
        else:
            mensaje_error += (
                "El servidor tardó más de 30 segundos en responder (posiblemente el "
                "antivirus lo está revisando a fondo, sobre todo la primera vez que "
                "se usa en esta computadora). Cierra este aviso, espera un momento y "
                "vuelve a abrir el programa."
            )
        print("⚠️ ", mensaje_error)
        try:
            # Con --noconsole no hay ventana de texto donde el usuario vea
            # este print, así que se muestra también como un cuadro de
            # diálogo nativo de Windows (no depende de que webview funcione).
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, mensaje_error, "Automatización RNDC - No se pudo iniciar", 0x10)
        except Exception:
            pass
        if error_arranque_flask:
            # El servidor de verdad no va a arrancar solo esperando más:
            # no tiene caso abrir una ventana que solo va a mostrar el
            # error de conexión de siempre.
            sys.exit(1)
        # Si fue solo lentitud (sin error real), se intenta abrir la
        # ventana de todas formas, por si el servidor arranca en los
        # próximos segundos mientras la ventana carga.

    try:
        import webview
        # Ventana de escritorio de verdad: barra de título propia,
        # minimizar/maximizar, y una X que cierra TODO el programa de
        # verdad (más abajo se fuerza el cierre completo del proceso,
        # para que el servidor no se quede vivo de fondo).
        webview.create_window(
            "Innova - Automatización RNDC",
            "http://localhost:5000",
            width=1250, height=850, min_size=(950, 650),
        )
        webview.start()

        # Al cerrar la ventana (la X, o Alt+F4), webview.start() ya
        # terminó de "bloquear" y el programa debería acabar solo aquí.
        # Pero a veces algo se queda vivo de fondo (el servidor Flask en
        # su hilo, o el propio proceso del .exe) y el puerto 5000 sigue
        # ocupado la próxima vez que se intenta abrir el programa —
        # dando justo el error de "no se puede conectar" que se vio antes.
        # Por eso, en vez de dejar que Python "intente" cerrar todo solo,
        # se fuerza el cierre completo del proceso aquí mismo.
        os._exit(0)
    except ImportError:
        # Si pywebview no está instalado (ej: corriendo con
        # "python servidor_web.py" sin haber instalado requirements.txt
        # completo), se cae de vuelta al navegador normal.
        import webbrowser
        webbrowser.open("http://localhost:5000")
        print("Servidor iniciado. Abre en tu navegador: http://localhost:5000")
        hilo_flask.join()
