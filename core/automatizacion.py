"""
core/automatizacion.py — El motor principal: `ejecutar_automatizacion`
(un viaje completo: Remesa + Manifiesto) y `ejecutar_cola` (varios
viajes seguidos reutilizando el mismo navegador).
"""
import time
import os
from datetime import datetime, timedelta
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoAlertPresentException, StaleElementReferenceException,
    NoSuchElementException, UnexpectedAlertPresentException, WebDriverException,
)

from .config import (
    URL_LOGIN, URL_LOGOUT, URL_REMESA, URL_MANIFIESTO,
    URL_REIMPRIMIR_REMESA, URL_REIMPRIMIR_MANIFIESTO,
    CARPETA_DESCARGAS, FIJOS_REMESA, FIJOS_MANIFIESTO, ruta_captura,
)
from .navegador import (
    crear_opciones_chrome, obtener_chromedriver_path, registrar_navegador_abandonado,
    crear_driver_con_limite,
)
from .utilidades import (
    set_text, set_select, set_select_por_texto_parcial, set_autocomplete_municipio,
    traducir_error, limpiar_numero, normalizar_tipo_id,
    esperar_confirmacion_manifiesto, leer_radicado_y_aceptar_alerta, limpiar_capturas_viejas,
    calcular_retencion_ica, calcular_fecha_pago_por_defecto,
)
from .terceros import crear_tercero, crear_vehiculo, buscar_nombre_tercero_con_sesion_activa
from .documentos import descargar_pdf_documento

def ejecutar_automatizacion(v, usuario, password, log, driver_compartido=None, wait_compartido=None,
                             ya_logueado=False, cerrar_al_terminar=True, sufijo_archivo_viaje=""):
    """
    v: diccionario con los datos del viaje (Consecutivo, Origen, Destino,
       Producto, Peso, Multiparada, Paradas, Cedula_Titular, Placa,
       Placa_Remolque, Cedula_Conductor, Nombre_Conductor,
       Apellido1_Conductor, Apellido2_Conductor, Municipio_Conductor,
       Flete, TipoID_Remitente_Cliente, NIT_Remitente_Cliente,
       TipoID_Destinatario_Cliente, NIT_Destinatario_Cliente)
    usuario, password: credenciales del RNDC
    log: función que recibe un mensaje de texto (para mostrar progreso)

    driver_compartido/wait_compartido: si se pasan (ej. desde la Cola de
        viajes, para reutilizar el mismo navegador entre varios viajes
        seguidos en vez de abrir uno nuevo cada vez), se usan en lugar de
        crear un navegador nuevo. ya_logueado=True evita repetir el login
        inicial (se asume que quien llama ya inició sesión). cerrar_al_
        terminar=False deja el navegador abierto al terminar (para que la
        Cola lo seguga usando en el siguiente viaje) en vez de cerrarlo.

    Devuelve: {"ok": bool, "error": str|None, "resumen": [str, ...]}
    """
    limpiar_capturas_viejas(dias=7, log=log)

    # Limpia puntos/comas/espacios de los campos numéricos (por si llegan
    # con puntuación, ej: cédula "1.234.567" o flete "1.500.000").
    campos_numericos = [
        "Peso", "Flete", "Anticipo", "Cedula_Titular", "Cedula_Conductor",
        "NIT_Remitente_Cliente", "NIT_Destinatario_Cliente",
    ]
    for campo in campos_numericos:
        if v.get(campo):
            v[campo] = limpiar_numero(v[campo])
    for parada in v.get("Paradas", []):
        for campo in ("Peso", "NIT_Remitente", "NIT_Destinatario"):
            if parada.get(campo):
                parada[campo] = limpiar_numero(parada[campo])

    fr = FIJOS_REMESA
    fm = FIJOS_MANIFIESTO
    invisible = bool(v.get("Invisible"))

    if driver_compartido is not None:
        driver = driver_compartido
        wait = wait_compartido
        log("♻️  Reutilizando el navegador y la sesión del viaje anterior de la cola (más rápido).")
    else:
        os.makedirs(CARPETA_DESCARGAS, exist_ok=True)
        chrome_options = crear_opciones_chrome(carpeta_descargas=CARPETA_DESCARGAS, invisible=invisible)
        log(f"Abriendo navegador Chrome ({'invisible' if invisible else 'visible'}) y arrancando el proceso completo...")
        driver = crear_driver_con_limite(chrome_options, obtener_chromedriver_path())
        driver.set_page_load_timeout(25)  # si el sitio no responde, no se cuelga para siempre
        if not invisible:
            driver.maximize_window()
        wait = WebDriverWait(driver, 20)

    def detener_por_error(mensaje):
        log(f"❌ {mensaje}")
        driver.save_screenshot(ruta_captura("debug_detenido_por_error.png"))
        if not cerrar_al_terminar:
            log("El proceso se DETUVO aquí. (El navegador se deja tal cual para que la Cola siga "
                "con el siguiente viaje.)")
        elif invisible:
            log("El proceso se DETUVO aquí. Como el navegador está en modo invisible, revisa la "
                "captura 'debug_detenido_por_error.png' guardada en la carpeta del programa.")
            driver.quit()
        else:
            log("El proceso se DETUVO aquí. El navegador se deja abierto para que revises qué pasó.")
            registrar_navegador_abandonado(driver, log)

    try:
        fecha_cargue = datetime.now().strftime("%d/%m/%Y")
        fecha_descargue = (datetime.now() + timedelta(days=5)).strftime("%d/%m/%Y")

        # ======================= LOGIN =======================
        def hacer_login():
            for intento in range(1, 4):
                try:
                    # Se cierra sesión explícitamente primero (clic en "Salir"),
                    # no solo se navega directo al login mientras la sesión
                    # anterior sigue medio activa. Esto evita el error interno
                    # del sitio que aparecía al crear varias remesas seguidas.
                    try:
                        driver.get(URL_LOGOUT)
                        time.sleep(0.35)
                    except Exception:
                        pass
                    driver.get(URL_LOGIN)
                    time.sleep(0.3)
                    campo_usuario = wait.until(EC.visibility_of_element_located(
                        (By.ID, "dnn_ctr390_FormLogIn_edUsername")))
                    campo_usuario.clear()
                    campo_usuario.send_keys(usuario)
                    driver.find_element(By.ID, "dnn_ctr390_FormLogIn_edPassword").send_keys(password)
                    driver.find_element(By.ID, "dnn_ctr390_FormLogIn_btIngresar").click()
                    time.sleep(0.35)
                    log("✅ Login realizado.")
                    return True
                except (TimeoutException, NoSuchElementException):
                    log(f"⚠️  La página de login tardó en cargar (intento {intento}/3). Reintentando...")
                    time.sleep(0.45)
                except WebDriverException as e:
                    texto_error = str(e).lower()
                    palabras_sitio_caido = ("err_connection", "err_name_not_resolved", "err_internet",
                                             "err_timed_out", "net::", "dns", "no se puede conectar")
                    if any(p in texto_error for p in palabras_sitio_caido):
                        log(f"❌ El sitio del RNDC parece estar CAÍDO o sin conexión (intento {intento}/3): {e}")
                    else:
                        log(f"⚠️  Error del navegador al intentar entrar (intento {intento}/3): {e}")
                    time.sleep(1.5)
            log("❌ No se pudo iniciar sesión tras varios intentos. Es posible que el sitio del RNDC "
                "esté caído en este momento, o que no tengas conexión a internet. Intenta de nuevo "
                "en unos minutos.")
            return False

        def sesion_expirada():
            """Detecta si el sitio nos devolvió a la pantalla de login sin
            avisar (sesión cerrada a mitad de proceso)."""
            try:
                return driver.find_element(By.ID, "dnn_ctr390_FormLogIn_edUsername").is_displayed()
            except Exception:
                return False

        if ya_logueado:
            log("♻️  Sesión ya activa (reutilizada), no hace falta volver a iniciar sesión.")
        elif not hacer_login():
            detener_por_error("No se pudo iniciar sesión en el RNDC tras varios intentos.")
            return {"ok": False, "error": "SITIO_CAIDO: no se pudo iniciar sesión", "resumen": []}

        # Guarda la ruta de cada PDF descargado con éxito (remesas y
        # manifiesto), para poder ofrecer la vista previa al terminar.
        archivos_generados = []

        def crear_remesa(consecutivo, origen, destino, producto, peso, sufijo_archivo="", intentos_reinicio=0,
                          tipoid_remitente_tramo=None, numid_remitente_tramo=None,
                          tipoid_destinatario_tramo=None, numid_destinatario_tramo=None):
          try:
            log(f"--- Llenando REMESA {consecutivo} ({origen} → {destino}) ---")
            driver.get(URL_REMESA)
            try:
                wait.until(EC.presence_of_element_located((By.ID, "dnn_ctr394_Remesa_TIPOIDPROPIETARIO")))
            except TimeoutException:
                pass  # seguimos igual; el resto del código ya maneja los reintentos si algo falta
            time.sleep(0.25)

            # A veces el sitio del RNDC muestra un error interno propio
            # (sesión corrupta) y la página queda en blanco/rota. Se
            # detecta por el texto típico de esos errores .NET y, si
            # aparece, se reinicia sesión y se vuelve a intentar desde cero
            # ANTES de llenar nada con datos vacíos.
            texto_pagina = driver.find_element(By.TAG_NAME, "body").text
            errores_conocidos_sitio = ["Object reference not set", "DataBinding:", "A critical error has occurred"]
            if any(err in texto_pagina for err in errores_conocidos_sitio) and intentos_reinicio < 15:
                espera = 2  # antes eran 8s, luego 3s; se agilizó más el reingreso
                log(f"    ⚠️  El sitio mostró un error interno propio al cargar la página de Remesa. "
                    f"Esperando {espera}s antes de reiniciar sesión y volver a intentar "
                    f"(intento {intentos_reinicio + 1}/15)...")
                time.sleep(espera)
                hacer_login()
                return crear_remesa(
                    consecutivo, origen, destino, producto, peso,
                    sufijo_archivo=sufijo_archivo, intentos_reinicio=intentos_reinicio + 1,
                    tipoid_remitente_tramo=tipoid_remitente_tramo, numid_remitente_tramo=numid_remitente_tramo,
                    tipoid_destinatario_tramo=tipoid_destinatario_tramo, numid_destinatario_tramo=numid_destinatario_tramo
                )

            set_select(driver, "dnn_ctr394_Remesa_TIPOIDPROPIETARIO", fr["TIPOIDPROPIETARIO"])
            set_text(driver, "dnn_ctr394_Remesa_NUMIDPROPIETARIO", fr["NUMIDPROPIETARIO"])
            set_select_por_texto_parcial(
                driver, "dnn_ctr394_Remesa_SEDEPROPIETARIOLISTA",
                fr["SEDE_PROPIETARIO_CONTIENE"], log, espera_previa=0.5
            )

            set_text(driver, "dnn_ctr394_Remesa_CONSECUTIVOREMESA", consecutivo)

            set_select(driver, "dnn_ctr394_Remesa_OPERACIONTRANSPORTE", fr["OPERACIONTRANSPORTE"])
            set_select_por_texto_parcial(
                driver, "dnn_ctr394_Remesa_TIPOEMPAQUE",
                fr["TIPOEMPAQUE_CONTIENE"], log, espera_previa=0.5
            )

            tipoid_remitente = normalizar_tipo_id(tipoid_remitente_tramo or v["TipoID_Remitente_Cliente"] or fr["TIPOIDREMITENTE"])
            numid_remitente = numid_remitente_tramo or v["NIT_Remitente_Cliente"] or fr["NUMIDREMITENTE"]
            set_select(driver, "dnn_ctr394_Remesa_TIPOIDREMITENTE", tipoid_remitente)
            set_text(driver, "dnn_ctr394_Remesa_NUMIDREMITENTE", numid_remitente)
            set_select_por_texto_parcial(
                driver, "dnn_ctr394_Remesa_SEDEREMITENTELISTA", origen, log, espera_previa=1.5
            )
            ciudad_cargue_real = driver.find_element(By.ID, "dnn_ctr394_Remesa_REM_ORIG").get_attribute("value").strip()
            log(f"Municipio de cargue registrado en la remesa: '{ciudad_cargue_real}'")
            set_text(driver, "dnn_ctr394_Remesa_FECHACITAPACTADACARGUE", fecha_cargue, blur=False)
            set_text(driver, "dnn_ctr394_Remesa_HORACITAPACTADACARGUE", fr["HORACITAPACTADACARGUE"], blur=False)
            set_text(driver, "dnn_ctr394_Remesa_HORASPACTOCARGA", fr["HORASPACTOCARGA"], blur=False)
            set_text(driver, "dnn_ctr394_Remesa_MINUTOSPACTOCARGA", fr["MINUTOSPACTOCARGA"], blur=False)

            tipoid_destinatario = normalizar_tipo_id(tipoid_destinatario_tramo or v["TipoID_Destinatario_Cliente"] or fr["TIPOIDDESTINATARIO"])
            numid_destinatario = numid_destinatario_tramo or v["NIT_Destinatario_Cliente"] or fr["NUMIDDESTINATARIO"]
            set_select(driver, "dnn_ctr394_Remesa_TIPOIDDESTINATARIO", tipoid_destinatario)
            set_text(driver, "dnn_ctr394_Remesa_NUMIDDESTINATARIO", numid_destinatario)
            set_select_por_texto_parcial(
                driver, "dnn_ctr394_Remesa_SEDEDESTINATARIOLISTA", destino, log, espera_previa=1.5
            )
            ciudad_descargue_real = driver.find_element(By.ID, "dnn_ctr394_Remesa_REM_DESTI").get_attribute("value").strip()

            mismas_ciudades_esperadas = (origen.strip().upper() == destino.strip().upper())
            if not mismas_ciudades_esperadas and ciudad_descargue_real == ciudad_cargue_real:
                log("⚠️  Cargue y descargue quedaron con la misma ciudad, reintentando destinatario...")
                set_text(driver, "dnn_ctr394_Remesa_NUMIDDESTINATARIO", numid_destinatario)
                time.sleep(1.2)
                set_select_por_texto_parcial(
                    driver, "dnn_ctr394_Remesa_SEDEDESTINATARIOLISTA", destino, log, espera_previa=3
                )
                ciudad_descargue_real = driver.find_element(By.ID, "dnn_ctr394_Remesa_REM_DESTI").get_attribute("value").strip()

            log(f"Municipio de descargue registrado en la remesa: '{ciudad_descargue_real}'")
            set_text(driver, "dnn_ctr394_Remesa_FECHACITAPACTADADESCARGUE", fecha_descargue, blur=False)
            set_text(driver, "dnn_ctr394_Remesa_HORACITAPACTADADESCARGUEREMESA", fr["HORACITAPACTADADESCARGUEREMESA"], blur=False)
            set_text(driver, "dnn_ctr394_Remesa_HORASPACTODESCARGUE", fr["HORASPACTODESCARGUE"], blur=False)
            set_text(driver, "dnn_ctr394_Remesa_MINUTOSPACTODESCARGUE", fr["MINUTOSPACTODESCARGUE"], blur=False)

            set_select(driver, "dnn_ctr394_Remesa_NATURALEZACARGA", fr["NATURALEZACARGA"])
            set_text(driver, "dnn_ctr394_Remesa_MERCANCIAREMESA", fr["CODIGOPRODUCTO"])
            set_text(driver, "dnn_ctr394_Remesa_DESCRIPCIONCORTAPRODUCTO", producto, blur=False)

            set_select(driver, "dnn_ctr394_Remesa_NOMUNIDADMEDIDAPRODUCTO", fr["NOMUNIDADMEDIDAPRODUCTO"])
            set_text(driver, "dnn_ctr394_Remesa_CANTIDADPRODUCTO", peso, blur=False)
            set_select(driver, "dnn_ctr394_Remesa_NOMUNIDADMEDIDACAPACIDAD", fr["NOMUNIDADMEDIDACAPACIDAD"])
            set_text(driver, "dnn_ctr394_Remesa_CANTIDADCARGADA", peso, blur=False)

            set_select(driver, "dnn_ctr394_Remesa_NOMDUENOPOLIZA", fr["NOMDUENOPOLIZA"])

            driver.save_screenshot(ruta_captura(f"debug_remesa_{consecutivo}_llenada.png"))

            if v.get("ModoPractica"):
                log(f"🎓 MODO PRÁCTICA: aquí se habría guardado la remesa {consecutivo}. "
                    f"No se creó nada real, no se descarga ningún PDF.")
                radicado_practica = f"PRACTICA-{int(time.time())}"
                return radicado_practica, ciudad_cargue_real, ciudad_descargue_real

            radicado = None
            max_intentos_remesa = 5  # los reintentos "de verdad" ahora recargan la página completa
            for intento in range(1, max_intentos_remesa + 1):
                log(f"Guardando la remesa {consecutivo} (intento {intento})...")
                try:
                    driver.find_element(By.ID, "dnn_ctr394_Remesa_btEnviar").click()
                except NoSuchElementException:
                    if intentos_reinicio < 15:
                        log("    La página cambió de estado (probablemente el intento anterior sí se "
                            "estaba procesando). Reiniciando sesión y volviendo a intentar esta remesa desde cero...")
                        hacer_login()
                        return crear_remesa(
                    consecutivo, origen, destino, producto, peso,
                    sufijo_archivo=sufijo_archivo, intentos_reinicio=intentos_reinicio + 1,
                    tipoid_remitente_tramo=tipoid_remitente_tramo, numid_remitente_tramo=numid_remitente_tramo,
                    tipoid_destinatario_tramo=tipoid_destinatario_tramo, numid_destinatario_tramo=numid_destinatario_tramo
                )
                    else:
                        log("    La página sigue cambiando de estado tras varios reinicios. Se detiene aquí.")
                        break

                radicado = leer_radicado_y_aceptar_alerta(driver, wait, f"remesa {consecutivo}", log)
                time.sleep(0.7)

                if radicado:
                    break

                texto_msg_encontrado = ""
                for id_msg in ("dnn_ctr394_Remesa_LBMSGERROR", "dnn_ctr394_Remesa_LBMSGERROR2"):
                    try:
                        texto_msg = driver.find_element(By.ID, id_msg).text.strip()
                        if texto_msg:
                            log(f"    Mensaje visible en la página: {texto_msg}")
                            texto_msg_encontrado = texto_msg
                    except Exception:
                        pass

                # Si el sitio dejó un mensaje de validación (ej: "Falta el
                # Consecutivo", "Falta el Tipo de Empaque"), NO conviene
                # reintentar sobre la misma página: algunos campos que se
                # llenaron por JavaScript se van perdiendo entre intento e
                # intento y cada vez falla por un motivo distinto. Se cierra
                # sesión, se vuelve a entrar, y se llena todo de nuevo desde cero.
                if texto_msg_encontrado and intentos_reinicio < 15:
                    log("    Reiniciando sesión y volviendo a llenar la remesa desde cero...")
                    hacer_login()
                    return crear_remesa(
                    consecutivo, origen, destino, producto, peso,
                    sufijo_archivo=sufijo_archivo, intentos_reinicio=intentos_reinicio + 1,
                    tipoid_remitente_tramo=tipoid_remitente_tramo, numid_remitente_tramo=numid_remitente_tramo,
                    tipoid_destinatario_tramo=tipoid_destinatario_tramo, numid_destinatario_tramo=numid_destinatario_tramo
                )

                if intento < max_intentos_remesa:
                    log("    No se confirmó la creación de la remesa. Reintentando...")
                    time.sleep(1.1)

            driver.save_screenshot(ruta_captura(f"debug_remesa_{consecutivo}_guardada.png"))

            if not radicado:
                return None, None, None

            log(f"Descargando el PDF de la remesa {consecutivo}...")
            nombre_archivo = f"remesa{v['Placa']}{sufijo_archivo}"
            archivo_remesa = descargar_pdf_documento(
                driver, wait, URL_REIMPRIMIR_REMESA, radicado, nombre_archivo, CARPETA_DESCARGAS,
                "dnn_ctr394_ReimprimirRemesa_RADICADO", "dnn_ctr394_ReimprimirRemesa_btImprimir", log
            )
            if archivo_remesa:
                archivos_generados.append(os.path.basename(archivo_remesa))

            return radicado, ciudad_cargue_real, ciudad_descargue_real

          except (NoSuchElementException, UnexpectedAlertPresentException) as e:
            # Si quedó una alerta nativa sin atender, la aceptamos para
            # destrabar el navegador antes de reintentar.
            try:
                alerta_pendiente = driver.switch_to.alert
                log(f"    (Había una alerta pendiente: {alerta_pendiente.text})")
                alerta_pendiente.accept()
            except NoAlertPresentException:
                pass

            log("    Reiniciando sesión por seguridad antes de reintentar...")
            hacer_login()

            if intentos_reinicio < 15:
                log(f"    ⚠️  Un campo esperado no apareció en la página ({e}). "
                    f"Reiniciando el llenado de esta remesa desde cero...")
                return crear_remesa(
                    consecutivo, origen, destino, producto, peso,
                    sufijo_archivo=sufijo_archivo, intentos_reinicio=intentos_reinicio + 1,
                    tipoid_remitente_tramo=tipoid_remitente_tramo, numid_remitente_tramo=numid_remitente_tramo,
                    tipoid_destinatario_tramo=tipoid_destinatario_tramo, numid_destinatario_tramo=numid_destinatario_tramo
                )
            else:
                log(f"    ⚠️  Un campo esperado sigue sin aparecer tras varios reinicios ({e}). Se detiene aquí.")
                driver.save_screenshot(ruta_captura(f"debug_remesa_{consecutivo}_error.png"))
                return None, None, None

        # ======================= CREAR REMESA(S) =======================
        remesas_creadas = []

        if v["Multiparada"] or v["IdaYRegreso"]:
            letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            for i, parada in enumerate(v["Paradas"]):
                if i > 0:
                    # El sitio del RNDC a veces da un error interno (DataBinding)
                    # al crear una segunda remesa seguida en la misma sesión.
                    # Cerrando y volviendo a iniciar sesión se evita.
                    log("Reiniciando sesión antes de la siguiente remesa (evita un error conocido del sitio)...")
                    hacer_login()
                consecutivo_tramo = f"{v['Consecutivo']}{letras[i]}"
                radicado, origen_real, destino_real = crear_remesa(
                    consecutivo_tramo, parada["Origen"], parada["Destino"],
                    parada["Producto"], parada["Peso"], sufijo_archivo=f"{sufijo_archivo_viaje}_{letras[i]}",
                    tipoid_remitente_tramo=parada.get("TipoID_Remitente") or None,
                    numid_remitente_tramo=parada.get("NIT_Remitente") or None,
                    tipoid_destinatario_tramo=parada.get("TipoID_Destinatario") or None,
                    numid_destinatario_tramo=parada.get("NIT_Destinatario") or None,
                )
                if not radicado:
                    detener_por_error(
                        f"No se pudo confirmar la creación de la REMESA {consecutivo_tramo} "
                        f"(tramo {i + 1} de {len(v['Paradas'])})."
                    )
                    return {"ok": False, "error": "remesa multi-tramo fallida", "resumen": []}
                remesas_creadas.append({
                    "consecutivo": consecutivo_tramo, "radicado": radicado,
                    "origen_real": origen_real, "destino_real": destino_real,
                })
        else:
            radicado, origen_real, destino_real = crear_remesa(
                v["Consecutivo"], v["Origen"], v["Destino"], v["Producto"], v["Peso"],
                sufijo_archivo=sufijo_archivo_viaje,
            )
            if not radicado:
                detener_por_error("No se pudo confirmar la creación de la REMESA tras varios intentos.")
                return {"ok": False, "error": "remesa fallida", "resumen": []}
            remesas_creadas.append({
                "consecutivo": v["Consecutivo"], "radicado": radicado,
                "origen_real": origen_real, "destino_real": destino_real,
                "peso": float(v["Peso"]),
            })

        ciudad_cargue_real = remesas_creadas[0]["origen_real"]
        ciudad_descargue_real = remesas_creadas[-1]["destino_real"]
        # Para Ida y Regreso (ej: Bogotá-Buenaventura-Bogotá), el municipio
        # intermedio es el punto de retorno: el destino del primer tramo.
        ciudad_intermedia_real = (
            remesas_creadas[0]["destino_real"]
            if v["IdaYRegreso"] and len(remesas_creadas) >= 2
            else None
        )

        # ======================= MANIFIESTO =======================
        manifiesto_descargado_por_consecutivo = [False]  # lista para poder modificarla desde adentro (closure)

        def intentar_llenar_manifiesto():
          try:
            """Llena y guarda el Manifiesto una vez. Si en el camino el
            sitio avisa que el conductor 'no existe como tercero', lo
            registra y devuelve una señal para reintentar TODO el llenado
            desde cero (porque al ir a crear el Tercero se pierde lo ya
            escrito en esta página).
            Devuelve: (radicado_o_None, mensaje_o_None, reintentar_bool)"""
            log("--- Llenando MANIFIESTO ---")
            driver.get(URL_MANIFIESTO)
            try:
                wait.until(EC.presence_of_element_located((By.ID, "dnn_ctr394_Manifiesto_NUMMANIFIESTOCARGA")))
            except TimeoutException:
                pass
            time.sleep(0.25)

            texto_pagina = driver.find_element(By.TAG_NAME, "body").text
            errores_conocidos_sitio = ["Object reference not set", "DataBinding:", "A critical error has occurred"]
            if any(err in texto_pagina for err in errores_conocidos_sitio):
                log("    ⚠️  El sitio mostró un error interno propio al cargar la página de Manifiesto. "
                    "Esperando 2s antes de reiniciar sesión y volver a intentar...")
                time.sleep(2)
                hacer_login()
                return None, None, "pagina"

            set_text(driver, "dnn_ctr394_Manifiesto_NUMMANIFIESTOCARGA", v["Consecutivo"])

            if v["Multiparada"]:
                tipo_manifiesto = "M"
            elif v["IdaYRegreso"]:
                tipo_manifiesto = "I"
            else:
                tipo_manifiesto = fm["TIPOMANIFIESTO"]
            set_select(driver, "dnn_ctr394_Manifiesto_NOMOPERACIONTRANSPORTE", tipo_manifiesto)
            fecha_expedicion = v.get("FechaExpedicion") or fecha_cargue
            set_text(driver, "dnn_ctr394_Manifiesto_FECHAEXPEDICIONMANIFIESTO", fecha_expedicion, blur=False)
            set_text(driver, "dnn_ctr394_Manifiesto_VIAJESDIA", "1", blur=False)

            indice_origen = 0
            indice_destino = 0
            set_autocomplete_municipio(
                driver, wait, "dnn_ctr394_Manifiesto_MANORIGEN", ciudad_cargue_real, log,
                indice=indice_origen, modo_flexible=True
            )
            set_autocomplete_municipio(
                driver, wait, "dnn_ctr394_Manifiesto_MANDESTINO", ciudad_descargue_real, log,
                indice=indice_destino, modo_flexible=True
            )
            if v["IdaYRegreso"] and ciudad_intermedia_real:
                set_autocomplete_municipio(
                    driver, wait, "dnn_ctr394_Manifiesto_MANINTERMEDIO", ciudad_intermedia_real, log,
                    modo_flexible=True
                )

            set_select(driver, "dnn_ctr394_Manifiesto_TIPOIDTITULARMANIFIESTO", fm["TIPOIDTITULAR"])

            # --- Titular: manejo REACTIVO del aviso "no existe como tercero" ---
            campo_titular = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_NUMIDTITULARMANIFIESTO")
            campo_titular.clear()
            campo_titular.send_keys(v["Cedula_Titular"])
            campo_titular.send_keys(Keys.TAB)
            time.sleep(1.1)
            try:
                alerta = driver.switch_to.alert
                texto_alerta = alerta.text
                alerta.accept()
                log(f"El titular no existe todavía: {texto_alerta}")
                if not v.get("Nombre_Titular") or not v.get("Apellido1_Titular") or not v.get("Municipio_Titular"):
                    return None, (
                        "El titular del manifiesto no está registrado en el sistema, pero faltan "
                        "Nombre / Apellido 1 / Municipio del titular en el formulario para poder crearlo."
                    ), None
                creado = crear_tercero(
                    driver, wait, fm["TIPOIDTITULAR"], v["Cedula_Titular"],
                    v["Nombre_Titular"], v["Apellido1_Titular"],
                    v.get("Apellido2_Titular", ""), v["Municipio_Titular"], log
                )
                if not creado:
                    return None, f"No se pudo registrar al titular {v['Cedula_Titular']} como Tercero.", None
                log("✅ Titular registrado. Reiniciando el llenado del manifiesto...")
                return None, None, "conductor"  # reutiliza la misma señal de "reiniciar todo"
            except NoAlertPresentException:
                pass  # el titular ya existía, seguir normal

            # --- Placa: manejo REACTIVO del aviso "no está registrado en el Maestro de Vehículos" ---
            campo_placa = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_NUMPLACA")
            campo_placa.clear()
            campo_placa.send_keys(v["Placa"])
            campo_placa.send_keys(Keys.TAB)
            time.sleep(1.1)
            try:
                alerta = driver.switch_to.alert
                texto_alerta = alerta.text
                alerta.accept()
                log(f"La placa no existe todavía: {texto_alerta}")
                creado = crear_vehiculo(driver, wait, v["Placa"], log)
                if not creado:
                    return None, f"No se pudo registrar la placa {v['Placa']} en el Maestro de Vehículos.", None
                log("✅ Vehículo registrado. Reiniciando el llenado del manifiesto...")
                return None, None, "conductor"  # reutiliza la misma señal de "reiniciar todo"
            except NoAlertPresentException:
                pass  # la placa ya existía, seguir normal

            # --- Placa Remolque: mismo manejo reactivo, si aplica ---
            if v["Placa_Remolque"]:
                campo_remolque = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_NUMPLACAREMOLQUE")
                campo_remolque.clear()
                campo_remolque.send_keys(v["Placa_Remolque"])
                campo_remolque.send_keys(Keys.TAB)
                time.sleep(1.1)
                try:
                    alerta = driver.switch_to.alert
                    texto_alerta = alerta.text
                    alerta.accept()
                    log(f"La placa del remolque no existe todavía: {texto_alerta}")
                    creado = crear_vehiculo(driver, wait, v["Placa_Remolque"], log)
                    if not creado:
                        return None, f"No se pudo registrar el remolque {v['Placa_Remolque']} en el Maestro de Vehículos.", None
                    log("✅ Remolque registrado. Reiniciando el llenado del manifiesto...")
                    return None, None, "conductor"
                except NoAlertPresentException:
                    pass  # el remolque ya existía, seguir normal

            set_select(driver, "dnn_ctr394_Manifiesto_TIPOIDCONDUCTOR", fm["TIPOIDCONDUCTOR"])

            # --- Conductor: manejo REACTIVO del aviso "no existe como tercero" ---
            campo_conductor = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_NUMIDCONDUCTOR")
            campo_conductor.clear()
            campo_conductor.send_keys(v["Cedula_Conductor"])
            campo_conductor.send_keys(Keys.TAB)
            time.sleep(1.1)
            try:
                alerta = driver.switch_to.alert
                texto_alerta = alerta.text
                alerta.accept()
                log(f"El conductor no existe todavía: {texto_alerta}")
                if not v["Nombre_Conductor"] or not v["Apellido1_Conductor"] or not v["Municipio_Conductor"]:
                    return None, (
                        "El conductor no está registrado en el sistema, pero faltan "
                        "Nombre / Apellido 1 / Municipio del conductor en el formulario "
                        "para poder crearlo."
                    ), None
                creado = crear_tercero(
                    driver, wait, fm["TIPOIDCONDUCTOR"], v["Cedula_Conductor"],
                    v["Nombre_Conductor"], v["Apellido1_Conductor"],
                    v["Apellido2_Conductor"], v["Municipio_Conductor"], log
                )
                if not creado:
                    return None, f"No se pudo registrar al conductor {v['Cedula_Conductor']} como Tercero.", None
                log("✅ Conductor registrado. Reiniciando el llenado del manifiesto...")
                return None, None, "conductor"  # reintentar TODO el manifiesto desde cero
            except NoAlertPresentException:
                pass  # el conductor ya existía, seguir normal

            # --- Segundo conductor (opcional, solo si el cliente lo pide) ---
            # Casilla confirmada en vivo por el usuario:
            # dnn_ctr394_Manifiesto_NUMIDCONDUCTOR2 / TIPOIDCONDUCTOR2.
            # set_select() ya selecciona el tipo de identificación Y tabula
            # después (es justo lo que hace esa función), que es lo que el
            # sitio necesita antes de dejar escribir en la casilla del
            # número de identificación.
            if v.get("Cedula_Conductor2"):
                try:
                    set_select(driver, "dnn_ctr394_Manifiesto_TIPOIDCONDUCTOR2", fm["TIPOIDCONDUCTOR"])
                    campo_conductor2 = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_NUMIDCONDUCTOR2")
                    campo_conductor2.clear()
                    campo_conductor2.send_keys(v["Cedula_Conductor2"])
                    campo_conductor2.send_keys(Keys.TAB)
                    time.sleep(1.1)
                    try:
                        alerta2 = driver.switch_to.alert
                        texto_alerta2 = alerta2.text
                        alerta2.accept()
                        log(f"⚠️  El segundo conductor no existe como Tercero todavía: {texto_alerta2}. "
                            f"Regístralo primero (pestaña de verificar) y vuelve a intentar — "
                            f"el manifiesto sigue sin el segundo conductor por ahora.")
                    except NoAlertPresentException:
                        log("✅ Segundo conductor diligenciado.")
                except NoSuchElementException:
                    log("⚠️  No se encontró la casilla del segundo conductor en el sitio con el nombre "
                        "esperado (dnn_ctr394_Manifiesto_NUMIDCONDUCTOR2) — puede que el RNDC use otro "
                        "nombre para esa casilla. El manifiesto sigue sin el segundo conductor; avísale "
                        "a soporte para ajustar esto.")
                except Exception as e:
                    log(f"⚠️  No se pudo diligenciar el segundo conductor: {traducir_error(e)}. "
                        f"El manifiesto sigue sin él.")

            valor_flete_actual = float(v["Flete"])
            fopat_aplica = bool(v["Placa_Remolque"])

            def actualizar_flete(nuevo_valor):
                campo_valor_flete = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_VALORFLETEPACTADOVIAJE")
                campo_valor_flete.clear()
                campo_valor_flete.send_keys(str(int(nuevo_valor)))
                driver.execute_script("VALORFLETEPACTADOVIAJE_onexit();")
                time.sleep(1.1)

            actualizar_flete(valor_flete_actual)

            campo_ica = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_RETENCIONICAMANIFIESTOCARGA")
            campo_ica.clear()
            valor_ica = calcular_retencion_ica(ciudad_cargue_real, ciudad_descargue_real, fm["RETENCIONICA"])
            campo_ica.send_keys(valor_ica)
            driver.execute_script("RETENCIONICA_onexit();")
            time.sleep(0.4)

            if v.get("Anticipo"):
                set_text(driver, "dnn_ctr394_Manifiesto_VALORANTICIPOMANIFIESTO", v["Anticipo"])

            def llenar_fopat():
                valor_fopat = round(valor_flete_actual * 0.001)
                campo_fopat = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_RETENCIONFOPAT")
                campo_fopat.clear()
                campo_fopat.send_keys(str(valor_fopat))
                driver.execute_script("RETENCIONFOPAT_onexit();")
                time.sleep(0.4)
                log(f"Retención FOPAT calculada: {valor_fopat}")

            if fopat_aplica:
                llenar_fopat()

            set_select(driver, "dnn_ctr394_Manifiesto_RESPONSABLEPAGOCARGUE", fm["RESPONSABLEPAGOCARGUE"])
            set_select(driver, "dnn_ctr394_Manifiesto_RESPONSABLEPAGODESCARGUE", fm["RESPONSABLEPAGODESCARGUE"])
            set_autocomplete_municipio(
                driver, wait, "dnn_ctr394_Manifiesto_MUNICIPIOPAGOSALDO", ciudad_descargue_real, log,
                indice=indice_destino, modo_flexible=True
            )
            fecha_pago = v.get("FechaPago") or calcular_fecha_pago_por_defecto(fecha_expedicion, fecha_descargue)
            set_text(driver, "dnn_ctr394_Manifiesto_FECHAPAGOSALDOMANIFIESTO", fecha_pago, blur=False)

            recomendaciones = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_OBSERVACIONES")
            recomendaciones.clear()
            recomendaciones.send_keys(v.get("Observaciones") or fm["RECOMENDACIONES"])

            for remesa_info in remesas_creadas:
                set_text(driver, "dnn_ctr394_Manifiesto_REMESA", remesa_info["consecutivo"], blur=False)
                driver.find_element(By.ID, "dnn_ctr394_Manifiesto_BTAGREGAR").click()
                time.sleep(1.1)

            driver.save_screenshot(ruta_captura("debug_manifiesto_llenado.png"))

            if v.get("ModoPractica"):
                log("🎓 MODO PRÁCTICA: aquí se habría guardado el manifiesto. "
                    "No se creó nada real, no se descarga ningún PDF.")
                radicado_practica = f"PRACTICA-{int(time.time())}"
                return radicado_practica, None, None

            radicado_manifiesto = None
            mensaje = None
            max_intentos = 20
            for intento in range(1, max_intentos + 1):
                log(f"Guardando el manifiesto (intento {intento})...")
                driver.find_element(By.ID, "dnn_ctr394_Manifiesto_btGuardar").click()

                resultado, mensaje = esperar_confirmacion_manifiesto(driver)

                if resultado == "exito":
                    radicado_manifiesto = mensaje
                    log(f"✅ Radicado del manifiesto: {radicado_manifiesto}")
                    break

                elif resultado == "exito_sin_radicado":
                    # El sitio SÍ dijo "Manifiesto Creado", pero no se pudo
                    # leer el número de radicado en pantalla. En vez de
                    # reintentar a ciegas (riesgo de duplicado), se intenta
                    # encontrar el mismo manifiesto por su CONSECUTIVO en la
                    # página de Reimprimir — tiene una casilla aparte para
                    # eso (NUMMANIFIESTOCARGA), separada de la de radicado.
                    driver.save_screenshot(ruta_captura("debug_manifiesto_sin_radicado.png"))
                    log("⚠️  El sitio confirmó \"Manifiesto Creado\", pero no se pudo leer el número "
                        "de radicado en la página. Buscándolo por el consecutivo en vez de reintentar "
                        "(así no se arriesga a crear uno duplicado)...")
                    archivo_por_consecutivo = descargar_pdf_documento(
                        driver, wait, URL_REIMPRIMIR_MANIFIESTO, v["Consecutivo"],
                        f"{v['Placa']}{sufijo_archivo_viaje}", CARPETA_DESCARGAS,
                        "dnn_ctr394_ReimprimirManifiesto_NUMMANIFIESTOCARGA",
                        "dnn_ctr394_ReimprimirManifiesto_btImprimir", log,
                        id_boton_consultar="dnn_ctr394_ReimprimirManifiesto_btConsultar",
                    )
                    if archivo_por_consecutivo:
                        archivos_generados.append(os.path.basename(archivo_por_consecutivo))
                        manifiesto_descargado_por_consecutivo[0] = True
                        log(f"✅ Manifiesto encontrado y descargado por el consecutivo {v['Consecutivo']} "
                            f"(el radicado exacto no se pudo leer, pero el PDF sí quedó guardado en Descargas).")
                        return v["Consecutivo"], "Encontrado por consecutivo", None

                    # Ni siquiera por consecutivo se pudo encontrar/descargar
                    # — ahí sí no queda más que detenerse y avisar para que
                    # se revise a mano, en vez de seguir intentando algo que
                    # ya demostró no funcionar por dos caminos distintos.
                    log("🛑 Tampoco se pudo encontrar el manifiesto por el consecutivo. "
                        "NO se va a reintentar crear uno nuevo, para no arriesgarse a un duplicado. "
                        "Entra al RNDC y busca este manifiesto a mano (Consultas) para confirmar el "
                        f"radicado real. Consecutivo: {v['Consecutivo']}.")
                    return None, "Manifiesto creado pero sin radicado legible — revisar a mano en el RNDC.", None

                elif resultado == "error":
                    log(f"⚠️  El sitio mostró un mensaje de error al guardar el manifiesto: {mensaje}")

                    # Se amplían las palabras que se reconocen para estos dos
                    # casos (FOPAT y flete insuficiente), porque el mensaje
                    # exacto que muestra el sitio puede variar y no siempre
                    # contiene literalmente "FOPAT" o el código "MAN045" —
                    # si no se reconoce, el programa caía al caso genérico
                    # (reiniciar sesión completa) en vez de solo ajustar el
                    # dato puntual y seguir, lo cual tarda mucho más de lo
                    # necesario.
                    if ("FOPAT" in mensaje.upper() or "PEAJE" in mensaje.upper()) and intento < max_intentos:
                        if not fopat_aplica:
                            log("    Parece que falta la Retención FOPAT. Calculándola y reintentando...")
                            fopat_aplica = True
                            llenar_fopat()
                        else:
                            log("    El sitio sigue rechazando por FOPAT aunque ya estaba puesto. "
                                "Quitándolo y reintentando...")
                            try:
                                campo_fopat = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_RETENCIONFOPAT")
                                campo_fopat.clear()
                                campo_fopat.send_keys(Keys.TAB)
                                driver.execute_script("RETENCIONFOPAT_onexit();")
                                time.sleep(0.35)
                            except NoSuchElementException:
                                pass
                            fopat_aplica = False
                        continue

                    elif (
                        "MAN045" in mensaje.upper()
                        or ("FLETE" in mensaje.upper() and any(
                            palabra in mensaje.upper()
                            for palabra in ("INSUFICIENTE", "BAJO", "MINIMO", "MÍNIMO", "SICETAC", "INFERIOR")
                        ))
                    ) and intento < max_intentos:
                        valor_flete_actual += 200000
                        log(f"    El valor del flete es muy bajo para SiceTac. "
                            f"Subiendo a {valor_flete_actual:,.0f} y reintentando...")
                        actualizar_flete(valor_flete_actual)
                        driver.execute_script("RETENCIONICA_onexit();")
                        time.sleep(0.4)
                        if fopat_aplica:
                            llenar_fopat()  # recalcular FOPAT con el nuevo valor del flete
                        continue

                    elif ("MAN220" in mensaje.upper() and not v["Multiparada"] and not v["IdaYRegreso"]
                          and intento < max_intentos):
                        log("    El peso excede la capacidad del vehículo. Se creará una remesa "
                            "nueva con 500 kilos menos y se reintentará...")
                        driver.save_screenshot(ruta_captura("debug_manifiesto_guardado.png"))
                        return None, mensaje, "peso"

                    elif ("MUNICIPIO" in mensaje.upper() or "MAN354" in mensaje.upper()
                          or "MAN353" in mensaje.upper()) and intento < max_intentos:
                        log("    Las ciudades del manifiesto no coinciden con las de la remesa. Reintentando...")
                        if "DESTINO" in mensaje.upper():
                            indice_destino += 1
                            set_autocomplete_municipio(
                                driver, wait, "dnn_ctr394_Manifiesto_MANDESTINO", ciudad_descargue_real, log,
                                indice=indice_destino, modo_flexible=True
                            )
                            set_autocomplete_municipio(
                                driver, wait, "dnn_ctr394_Manifiesto_MUNICIPIOPAGOSALDO", ciudad_descargue_real, log,
                                indice=indice_destino, modo_flexible=True
                            )
                        else:
                            indice_origen += 1
                            set_autocomplete_municipio(
                                driver, wait, "dnn_ctr394_Manifiesto_MANORIGEN", ciudad_cargue_real, log,
                                indice=indice_origen, modo_flexible=True
                            )
                        continue

                    else:
                        log(f"    Error no reconocido específicamente: \"{mensaje}\" — si esto se repite "
                            f"seguido, copia este mensaje tal cual para ajustar el programa. "
                            f"Reiniciando sesión y recargando el manifiesto completo desde cero...")
                        driver.save_screenshot(ruta_captura("debug_manifiesto_guardado.png"))
                        hacer_login()
                        return None, mensaje, "pagina"
                else:
                    log("⚠️  No apareció ni confirmación ni alerta de error para el manifiesto "
                        "(posible lentitud del sitio). Reiniciando sesión y recargando desde cero...")
                    driver.save_screenshot(ruta_captura("debug_manifiesto_guardado.png"))
                    hacer_login()
                    return None, mensaje, "pagina"

            driver.save_screenshot(ruta_captura("debug_manifiesto_guardado.png"))
            return radicado_manifiesto, mensaje, None

          except (NoSuchElementException, UnexpectedAlertPresentException) as e:
            try:
                alerta_pendiente = driver.switch_to.alert
                log(f"    (Había una alerta pendiente: {alerta_pendiente.text})")
                alerta_pendiente.accept()
            except NoAlertPresentException:
                pass
            log("    Reiniciando sesión por seguridad antes de reintentar...")
            hacer_login()
            log(f"    ⚠️  Un campo esperado no apareció en el manifiesto ({e}). Reintentando desde cero...")
            driver.save_screenshot(ruta_captura("debug_manifiesto_error.png"))
            return None, str(e), "pagina"

        radicado_manifiesto = None
        mensaje = None
        letras_peso = "BCDEFGHIJKLMNOPQRSTUVWXYZ"  # A ya se usa si hay multiparada; aquí para reducción de peso
        indice_letra_peso = 0
        max_intentos_totales = 15  # 1 normal + margen para conductor, peso, y reinicios por página

        for intento_general in range(max_intentos_totales):
            radicado_manifiesto, mensaje, senal = intentar_llenar_manifiesto()

            if senal == "conductor":
                continue

            elif senal == "pagina":
                time.sleep(0.7)
                continue

            elif senal == "peso":
                if len(remesas_creadas) != 1:
                    log("    No se puede ajustar el peso automáticamente en este tipo de viaje "
                        "(multiparada o varias remesas). Se detiene aquí.")
                    break
                peso_anterior = float(remesas_creadas[0].get("peso", v["Peso"]))
                nuevo_peso = peso_anterior - 500
                if nuevo_peso <= 0 or indice_letra_peso >= len(letras_peso):
                    log("    Ya no se puede reducir más el peso. Se detiene aquí.")
                    break
                nuevo_consecutivo = f"{v['Consecutivo']}{letras_peso[indice_letra_peso]}"
                sufijo_archivo = f"_{letras_peso[indice_letra_peso]}"
                indice_letra_peso += 1
                radicado_nuevo, origen_nuevo, destino_nuevo = crear_remesa(
                    nuevo_consecutivo, v["Origen"], v["Destino"], v["Producto"],
                    str(int(nuevo_peso)), sufijo_archivo=sufijo_archivo
                )
                if not radicado_nuevo:
                    log(f"    No se pudo crear la remesa {nuevo_consecutivo} con menos peso. Se detiene aquí.")
                    break
                remesas_creadas.clear()
                remesas_creadas.append({
                    "consecutivo": nuevo_consecutivo, "radicado": radicado_nuevo,
                    "origen_real": origen_nuevo, "destino_real": destino_nuevo,
                    "peso": nuevo_peso,
                })
                continue

            else:
                break

        if not radicado_manifiesto:
            detalle = f" Mensaje del sitio: {mensaje}" if mensaje else ""
            detener_por_error("No se pudo confirmar la creación del MANIFIESTO (no se obtuvo radicado)." + detalle)
            return {"ok": False, "error": mensaje or "manifiesto fallido", "resumen": [], "archivos": archivos_generados}

        if v.get("ModoPractica"):
            log("🎓 MODO PRÁCTICA: no se descarga ningún PDF (no se creó nada real).")
        elif manifiesto_descargado_por_consecutivo[0]:
            log("    (El PDF del manifiesto ya se descargó por el consecutivo más arriba, no hace falta de nuevo.)")
        else:
            log("Descargando el PDF del manifiesto (vía Reimprimir Manifiesto)...")
            archivo_manifiesto = descargar_pdf_documento(
                driver, wait, URL_REIMPRIMIR_MANIFIESTO, radicado_manifiesto,
                f"{v['Placa']}{sufijo_archivo_viaje}", CARPETA_DESCARGAS,
                "dnn_ctr394_ReimprimirManifiesto_RADICADO", "dnn_ctr394_ReimprimirManifiesto_btImprimir", log,
                id_boton_consultar="dnn_ctr394_ReimprimirManifiesto_btConsultar"
            )
            if archivo_manifiesto:
                archivos_generados.append(os.path.basename(archivo_manifiesto))

        resumen = [f"Remesa {r['consecutivo']} -> radicado: {r['radicado']}" for r in remesas_creadas]
        resumen.append(f"Manifiesto consecutivo {v['Consecutivo']} -> radicado: {radicado_manifiesto}")
        resumen.append(f"Documentos descargados en: {CARPETA_DESCARGAS}")

        log("================ RESUMEN ================")
        for linea in resumen:
            log(linea)
        log("===========================================")

        # Se confirma el nombre REAL del conductor y del titular (aunque
        # el usuario solo haya escrito la cédula), reutilizando la misma
        # sesión ya iniciada — para que el registro que se guarda en la
        # tabla de Drive siempre tenga el nombre completo.
        nombre_conductor_real = v.get("Nombre_Conductor") or None
        nombre_titular_real = v.get("Nombre_Titular") or None
        try:
            if not nombre_conductor_real and v.get("Cedula_Conductor"):
                nombre_conductor_real = buscar_nombre_tercero_con_sesion_activa(
                    driver, wait, fm.get("TIPOIDCONDUCTOR", "C"), v.get("Cedula_Conductor"), log
                )
            if not nombre_titular_real and v.get("Cedula_Titular"):
                nombre_titular_real = buscar_nombre_tercero_con_sesion_activa(
                    driver, wait, fm.get("TIPOIDTITULAR", "C"), v.get("Cedula_Titular"), log
                )
        except Exception as e:
            log(f"    ⚠️  No se pudieron confirmar los nombres para el registro: {traducir_error(e)}")

        if cerrar_al_terminar:
            driver.quit()  # todo salió bien, se cierra el navegador solo
        return {
            "ok": True, "error": None, "resumen": resumen, "archivos": archivos_generados,
            "nombre_conductor_real": nombre_conductor_real,
            "nombre_titular_real": nombre_titular_real,
        }

    except Exception as e:
        log("❌ Ocurrió un error inesperado:")
        log(traducir_error(e))
        try:
            driver.save_screenshot(ruta_captura("debug_error.png"))
        except Exception:
            pass
        if not cerrar_al_terminar:
            log("(El navegador se deja tal cual para que la Cola siga con el siguiente viaje.)")
        elif invisible:
            log("Como el navegador está en modo invisible, revisa la captura 'debug_error.png' "
                "guardada en la carpeta del programa.")
            try:
                driver.quit()
            except Exception:
                pass
        else:
            log("El navegador se deja abierto para que revises qué pasó.")
            registrar_navegador_abandonado(driver, log)
        return {"ok": False, "error": repr(e), "resumen": []}


def ejecutar_cola(lista_de_viajes, usuario, password, log):
    """Ejecuta varios viajes NORMALES (no Multiparada/Ida y Regreso) uno
    detrás de otro, REUTILIZANDO el mismo navegador y la misma sesión
    para todos (más rápido: solo se abre Chrome y se hace login una vez,
    no en cada viaje). Si alguno falla, lo anota y sigue con el siguiente
    en vez de detener toda la cola. Si el navegador compartido se rompe
    a mitad de camino, se recupera abriendo uno nuevo automáticamente.

    lista_de_viajes: lista de diccionarios 'v' (uno por viaje).
    Devuelve: {"ok": bool, "resultados": [ {...} , ... ]}
    """
    resultados = []
    total = len(lista_de_viajes)
    invisible = bool(lista_de_viajes[0].get("Invisible")) if lista_de_viajes else False

    driver = None
    wait = None

    def abrir_navegador_compartido():
        os.makedirs(CARPETA_DESCARGAS, exist_ok=True)
        chrome_options = crear_opciones_chrome(carpeta_descargas=CARPETA_DESCARGAS, invisible=invisible)
        log(f"Abriendo UN SOLO navegador Chrome ({'invisible' if invisible else 'visible'}) "
            f"para toda la cola de {total} viaje(s)...")
        d = crear_driver_con_limite(chrome_options, obtener_chromedriver_path())
        d.set_page_load_timeout(25)
        if not invisible:
            d.maximize_window()
        w = WebDriverWait(d, 20)
        return d, w

    try:
        driver, wait = abrir_navegador_compartido()
    except Exception as e:
        log(f"❌ No se pudo abrir el navegador compartido: {traducir_error(e)}")
        return {"ok": False, "resultados": [], "archivos": []}

    for i, v in enumerate(lista_de_viajes, start=1):
        consecutivo = v.get("Consecutivo", "?")
        log(f"\n=========================================")
        log(f"===  VIAJE {i}/{total} - Consecutivo {consecutivo}  ===")
        log(f"=========================================")
        try:
            resultado = ejecutar_automatizacion(
                v, usuario, password, log,
                driver_compartido=driver, wait_compartido=wait,
                ya_logueado=(i > 1),  # el primer viaje sí hace login; los demás reutilizan la sesión
                cerrar_al_terminar=False,
                sufijo_archivo_viaje=f"_viaje{i}",  # para que no se reemplacen entre sí en Descargas (misma placa)
            )
        except WebDriverException as e:
            # El navegador compartido se rompió (se cerró solo, se colgó,
            # etc.) - se abre uno nuevo y se reintenta ESTE MISMO viaje
            # una vez antes de darlo por fallido.
            log(f"⚠️  El navegador compartido tuvo un problema: {traducir_error(e)}. "
                f"Abriendo uno nuevo y reintentando este viaje...")
            try:
                driver.quit()
            except Exception:
                pass
            try:
                driver, wait = abrir_navegador_compartido()
                resultado = ejecutar_automatizacion(
                    v, usuario, password, log,
                    driver_compartido=driver, wait_compartido=wait,
                    ya_logueado=False, cerrar_al_terminar=False,
                    sufijo_archivo_viaje=f"_viaje{i}",
                )
            except Exception as e2:
                log(f"❌ No se pudo recuperar el navegador: {traducir_error(e2)}")
                resultado = {"ok": False, "error": repr(e2), "resumen": []}
        except Exception as e:
            log(f"❌ Error inesperado en el viaje {i}/{total} ({consecutivo}): {traducir_error(e)}")
            resultado = {"ok": False, "error": repr(e), "resumen": []}
        resultados.append({"consecutivo": consecutivo, **resultado})

        if not resultado.get("ok") and "SITIO_CAIDO" in str(resultado.get("error", "")):
            pendientes = total - i
            log(f"\n❌ El sitio del RNDC parece estar CAÍDO. Se detiene la cola aquí para no "
                f"perder tiempo intentando los {pendientes} viaje(s) restantes en vano.")
            log("    Cuando el sitio vuelva a funcionar, puedes correr de nuevo solo los "
                "viajes que faltaron.")
            break

    # Ya se procesaron todos los viajes (o se detuvo por sitio caído):
    # ahora sí se cierra el navegador compartido.
    try:
        driver.quit()
    except Exception:
        pass

    log("\n================ RESUMEN DE LA COLA ================")
    exitosos = [r for r in resultados if r["ok"]]
    fallidos = [r for r in resultados if not r["ok"]]
    log(f"✅ Viajes completados: {len(exitosos)}/{total}")
    for r in exitosos:
        for linea in r.get("resumen", []):
            log(f"    {linea}")
    if fallidos:
        log(f"❌ Viajes con error: {len(fallidos)}/{total}")
        for r in fallidos:
            log(f"    Consecutivo {r['consecutivo']}: {r.get('error', 'error desconocido')}")
    log("======================================================")

    archivos_totales = []
    for r in resultados:
        archivos_totales.extend(r.get("archivos", []))

    return {"ok": len(fallidos) == 0, "resultados": resultados, "archivos": archivos_totales}
