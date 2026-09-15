"""
core/terceros.py — Todo lo relacionado con "terceros" del RNDC
(conductores, titulares) y vehículos: crearlos, verificarlos, y
descargar el listado completo de la empresa (Maestros).
"""
import time
import re
import json
import os
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoAlertPresentException, StaleElementReferenceException,
    NoSuchElementException, UnexpectedAlertPresentException, WebDriverException,
)

from .config import (
    URL_LOGIN, URL_REMESA, URL_MANIFIESTO, URL_TERCERO, URL_VEHICULO,
    URL_MAESTRO, ARCHIVO_SEDES, ARCHIVO_CONDUCTORES, FIJOS_REMESA,
    ruta_captura,
)
from .navegador import crear_opciones_chrome, obtener_chromedriver_path, crear_driver_con_limite
from .utilidades import set_text, set_select, set_autocomplete_municipio, traducir_error, limpiar_numero

def crear_tercero(driver, wait, tipo_id, numero_id, nombre, apellido1, apellido2, municipio, log):
    log(f"    Registrando Tercero nuevo: {tipo_id} {numero_id} - {nombre} {apellido1}...")
    driver.get(URL_TERCERO)
    try:
        wait.until(EC.presence_of_element_located((By.ID, "dnn_ctr394_Tercero_TIPOIDTERCERO")))
    except TimeoutException:
        pass
    time.sleep(0.55)

    set_select(driver, "dnn_ctr394_Tercero_TIPOIDTERCERO", tipo_id)

    campo_num = driver.find_element(By.ID, "dnn_ctr394_Tercero_NUMIDTERCERO")
    campo_num.clear()
    campo_num.send_keys(numero_id)
    campo_num.send_keys(Keys.TAB)
    time.sleep(1.1)

    set_text(driver, "dnn_ctr394_Tercero_NOMIDTERCERO", nombre)
    set_text(driver, "dnn_ctr394_Tercero_PRIMERAPELLIDOIDTERCERO", apellido1)
    if apellido2:
        set_text(driver, "dnn_ctr394_Tercero_SEGUNDOAPELLIDOIDTERCERO", apellido2)

    set_autocomplete_municipio(driver, wait, "dnn_ctr394_Tercero_MUNICIPIORNDC", municipio, log, modo_flexible=True)
    set_text(driver, "dnn_ctr394_Tercero_NOMSEDETERCERO", municipio)
    time.sleep(0.55)

    driver.save_screenshot(ruta_captura(f"debug_tercero_{numero_id}_llenado.png"))
    driver.find_element(By.ID, "dnn_ctr394_Tercero_btGuardar").click()
    time.sleep(1.2)

    guardado = True
    try:
        alerta = driver.switch_to.alert
        texto_alerta = alerta.text
        log(f"    Mensaje al guardar Tercero: {texto_alerta}")
        alerta.accept()
    except NoAlertPresentException:
        try:
            texto_error = driver.find_element(By.ID, "dnn_ctr394_Tercero_lbMsgError").text.strip()
            if texto_error:
                log(f"    ⚠️  Mensaje de error al crear el Tercero: {texto_error}")
                guardado = False
        except Exception:
            pass

    if not guardado:
        driver.save_screenshot(ruta_captura(f"debug_tercero_{numero_id}_guardado.png"))
    return guardado


def crear_vehiculo(driver, wait, placa, log):
    """Registra una placa nueva (vehículo O remolque, es la misma página)
    usando el botón 'CREAR +' (Vincular Rápidamente). Después lee el campo
    'Clase' que el propio sitio detecta para decidir el Peso Vacío
    correcto: Camión = 1200 kg, Tractocamión = 3000 kg,
    Semirremolque/Remolque = 6000 kg. Devuelve True si quedó vinculado."""
    log(f"    Registrando vehículo/remolque nuevo: placa {placa}...")
    driver.get(URL_VEHICULO)
    try:
        wait.until(EC.presence_of_element_located((By.ID, "dnn_ctr394_Vehiculo_NUMPLACA")))
    except TimeoutException:
        pass
    time.sleep(0.55)

    campo_placa = driver.find_element(By.ID, "dnn_ctr394_Vehiculo_NUMPLACA")
    campo_placa.clear()
    campo_placa.send_keys(placa)
    campo_placa.send_keys(Keys.TAB)
    time.sleep(1.1)

    driver.find_element(By.ID, "btnVincularFast").click()
    time.sleep(2.5)  # dar tiempo de sobra a que el sitio consulte y llene la Clase

    try:
        texto_msg = driver.find_element(By.ID, "dnn_ctr394_Vehiculo_LBMSGERROR").text.strip()
        log(f"    Mensaje al vincular: {texto_msg}")
    except Exception:
        texto_msg = ""

    # No confiamos solo en el texto del mensaje (puede tardar en aparecer o
    # variar). Se considera vinculado si además el campo "Clase" ya quedó
    # con algún valor (eso confirma que el sitio sí encontró y cargó el
    # vehículo/remolque).
    clase = ""
    try:
        clase = driver.find_element(By.ID, "dnn_ctr394_Vehiculo_CLASE").get_attribute("value").strip().upper()
    except Exception:
        pass

    vinculado = ("vinculado" in texto_msg.lower()) or bool(clase)
    log(f"    Clase detectada por el sitio: '{clase}' (vinculado={vinculado})")

    if vinculado:
        if "SEMIRREMOLQUE" in clase or "REMOLQUE" in clase:
            peso_vacio = "6000"
        elif "TRACTOCAMION" in clase or "TRACTOMULA" in clase or "TRACTO" in clase:
            peso_vacio = "3000"
        else:
            peso_vacio = "1200"  # Camión u otra clase no prevista/no detectada, se usa el valor de camión por defecto

        log(f"    Ajustando Peso Vacío a {peso_vacio} kg...")
        try:
            campo_peso = wait.until(EC.element_to_be_clickable((By.ID, "dnn_ctr394_Vehiculo_PESOVEHICULOVACIO")))
            campo_peso.clear()
            campo_peso.send_keys(peso_vacio)
            campo_peso.send_keys(Keys.TAB)
            time.sleep(0.5)
            valor_quedo = campo_peso.get_attribute("value")
            log(f"    Peso Vacío en el campo tras escribirlo: '{valor_quedo}'")
            driver.find_element(By.ID, "dnn_ctr394_Vehiculo_btGuardar").click()
            time.sleep(1)
        except (NoSuchElementException, TimeoutException) as e:
            log(f"    ⚠️  No se pudo ajustar el Peso Vacío (campo no encontrado o no disponible: {e}).")
        except Exception as e:
            log(f"    ⚠️  Error inesperado ajustando el Peso Vacío: {traducir_error(e)}")

    driver.save_screenshot(ruta_captura(f"debug_vehiculo_{placa}_guardado.png"))
    return vinculado


def verificar_y_crear_conductor_si_falta(driver, wait, tipo_id, numero_id, nombre, apellido1, apellido2, municipio, log):
    log(f"Verificando si el conductor {numero_id} ya está registrado...")
    driver.get(URL_MANIFIESTO)
    time.sleep(1.2)

    campo = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_NUMIDCONDUCTOR")
    campo.clear()
    campo.send_keys(numero_id)
    campo.send_keys(Keys.TAB)
    time.sleep(1.1)

    try:
        alerta = driver.switch_to.alert
        texto_alerta = alerta.text
        alerta.accept()
        log(f"    El conductor no existe todavía: {texto_alerta}")
        if not nombre or not apellido1 or not municipio:
            return False, (
                "El conductor no está registrado en el sistema, pero faltan "
                "Nombre / Apellido / Municipio del conductor en el formulario para poder crearlo."
            )
        creado = crear_tercero(driver, wait, tipo_id, numero_id, nombre, apellido1, apellido2, municipio, log)
        if not creado:
            return False, f"No se pudo registrar al conductor {numero_id} como Tercero."
        log(f"    ✅ Conductor {numero_id} registrado.")
        return True, None
    except NoAlertPresentException:
        log("    El conductor ya estaba registrado.")
        return True, None


def buscar_nombre_tercero_con_sesion_activa(driver, wait, tipo_id, numero_id, log=print):
    """Igual que verificar_tercero, pero REUTILIZA un navegador que ya
    está abierto y con sesión iniciada (en vez de abrir uno nuevo y
    volver a hacer login) — se usa para completar el nombre real del
    conductor/titular justo antes de registrar un viaje, sin gastar
    tiempo extra abriendo otra sesión completa.
    Devuelve el nombre (str) si lo encuentra, o None si no."""
    if not numero_id:
        return None
    try:
        driver.get(URL_MANIFIESTO)
        time.sleep(1.2)

        campo_tipo = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_TIPOIDCONDUCTOR")
        Select(campo_tipo).select_by_value(tipo_id or "C")
        campo_tipo.send_keys(Keys.TAB)
        time.sleep(0.5)

        campo = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_NUMIDCONDUCTOR")
        campo.clear()
        campo.send_keys(limpiar_numero(numero_id))
        campo.send_keys(Keys.TAB)
        time.sleep(1.3)

        try:
            alerta = driver.switch_to.alert
            alerta.accept()
            return None  # no existe como tercero
        except NoAlertPresentException:
            pass

        try:
            valor = driver.find_element(
                By.ID, "dnn_ctr394_Manifiesto_MANNOMBRECONDUCTOR"
            ).get_attribute("value").strip()
            return valor or None
        except Exception:
            return None
    except Exception as e:
        log(f"    ⚠️  No se pudo confirmar el nombre para {numero_id}: {traducir_error(e)}")
        return None


def verificar_tercero(tipo_id, numero_id, usuario, password, log):
    """Consulta rápida: selecciona el Tipo de Identificación correcto,
    escribe la cédula/NIT en el campo de conductor del Manifiesto, y
    revisa si el sitio avisa que 'no existe como tercero'.
    Si existe, intenta leer el nombre asociado (puede no lograrlo, según
    el campo exacto que use el sitio para mostrarlo).
    Devuelve: {"existe": bool, "nombre": str|None}"""
    chrome_options = crear_opciones_chrome()
    driver = crear_driver_con_limite(chrome_options, obtener_chromedriver_path())
    driver.set_page_load_timeout(25)
    wait = WebDriverWait(driver, 20)
    try:
        driver.get(URL_LOGIN)
        time.sleep(1)
        wait.until(EC.visibility_of_element_located(
            (By.ID, "dnn_ctr390_FormLogIn_edUsername"))).send_keys(usuario)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_edPassword").send_keys(password)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_btIngresar").click()
        time.sleep(2)

        driver.get(URL_MANIFIESTO)
        time.sleep(1.5)

        # Primero se selecciona el Tipo de Identificación correcto (Cédula,
        # Nit, etc.) y se sale del campo, para que la validación compare
        # contra el tipo correcto, no el que haya quedado por defecto.
        campo_tipo = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_TIPOIDCONDUCTOR")
        Select(campo_tipo).select_by_value(tipo_id or "C")
        campo_tipo.send_keys(Keys.TAB)
        time.sleep(0.5)

        campo = driver.find_element(By.ID, "dnn_ctr394_Manifiesto_NUMIDCONDUCTOR")
        campo.clear()
        campo.send_keys(limpiar_numero(numero_id))
        campo.send_keys(Keys.TAB)
        time.sleep(1.5)

        try:
            alerta = driver.switch_to.alert
            alerta.accept()
            return {"existe": False, "nombre": None}
        except NoAlertPresentException:
            pass

        # Intenta leer el nombre del conductor, si el sitio lo muestra en
        # algún campo de solo lectura cercano (varía según el sitio; si no
        # se logra, igual se confirma que SÍ existe).
        nombre = None
        for posible_id in ("dnn_ctr394_Manifiesto_MANNOMBRECONDUCTOR",):
            try:
                valor = driver.find_element(By.ID, posible_id).get_attribute("value").strip()
                if valor:
                    nombre = valor
                    break
            except Exception:
                continue

        return {"existe": True, "nombre": nombre}
    finally:
        driver.quit()


def obtener_lista_sedes_empresa(usuario, password, log):
    """Inicia sesión, va a Remesa, escribe el NIT fijo de la empresa, y lee
    la lista REAL de Sedes que el propio RNDC devuelve para ese NIT
    (la misma que se usa como Origen/Destino). La guarda en un archivo
    para que la página web la use como sugerencias reales, en vez de una
    lista genérica de ciudades de Colombia."""
    fr = FIJOS_REMESA
    os.makedirs(os.path.dirname(ARCHIVO_SEDES) or ".", exist_ok=True)

    chrome_options = crear_opciones_chrome()
    driver = crear_driver_con_limite(chrome_options, obtener_chromedriver_path())
    driver.set_page_load_timeout(25)  # si el sitio no responde, no se cuelga para siempre
    wait = WebDriverWait(driver, 20)
    try:
        driver.get(URL_LOGIN)
        time.sleep(1)
        wait.until(EC.visibility_of_element_located(
            (By.ID, "dnn_ctr390_FormLogIn_edUsername"))).send_keys(usuario)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_edPassword").send_keys(password)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_btIngresar").click()
        time.sleep(2)
        log("Login realizado. Consultando la lista real de Sedes...")

        driver.get(URL_REMESA)
        time.sleep(1.5)

        set_select(driver, "dnn_ctr394_Remesa_TIPOIDREMITENTE", fr["TIPOIDREMITENTE"])
        set_text(driver, "dnn_ctr394_Remesa_NUMIDREMITENTE", fr["NUMIDREMITENTE"])
        time.sleep(1.5)  # dar tiempo de sobra a que la lista de sedes se llene

        sel = Select(driver.find_element(By.ID, "dnn_ctr394_Remesa_SEDEREMITENTELISTA"))
        opciones = sorted(set(o.text.strip() for o in sel.options if o.text.strip()))

        with open(ARCHIVO_SEDES, "w", encoding="utf-8") as f:
            json.dump(opciones, f, ensure_ascii=False, indent=2)

        log(f"✅ Se guardaron {len(opciones)} sedes reales en {ARCHIVO_SEDES}.")
        return opciones
    finally:
        driver.quit()


def obtener_lista_conductores_empresa(usuario, password, log):
    """Inicia sesión, va a Consultas Maestros, elige 'Tercero' en el
    combo, y lee la tabla de resultados (Identificación + Nombre +
    Apellidos) de TODOS los terceros ya registrados de la empresa,
    recorriendo TODAS las páginas de resultados (una empresa grande
    puede tener miles de terceros repartidos en cientos de páginas). La
    guarda en un archivo para sugerir el nombre mientras se escribe una
    cédula de conductor. Con muchas páginas esto puede tardar varios
    minutos."""
    os.makedirs(os.path.dirname(ARCHIVO_CONDUCTORES) or ".", exist_ok=True)

    # Modo invisible por defecto: nadie necesita ver esta pantalla
    # pasando de página en página, y así es más rápido (el navegador no
    # gasta tiempo dibujando en pantalla lo que nadie está mirando).
    chrome_options = crear_opciones_chrome(invisible=True)
    driver = crear_driver_con_limite(chrome_options, obtener_chromedriver_path())
    driver.set_page_load_timeout(25)
    wait = WebDriverWait(driver, 20)
    try:
        driver.get(URL_LOGIN)
        time.sleep(1)
        wait.until(EC.visibility_of_element_located(
            (By.ID, "dnn_ctr390_FormLogIn_edUsername"))).send_keys(usuario)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_edPassword").send_keys(password)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_btIngresar").click()
        time.sleep(2)
        log("Login realizado. Consultando la lista real de conductores/terceros...")

        driver.get(URL_MAESTRO)

        # Abrir el combo "Maestro" (es un control DevExpress, no un
        # <select> normal) y elegir "Tercero" por su TEXTO, no por un
        # número de posición (que podría cambiar). Se espera de verdad a
        # que aparezca (hasta 20s), en vez de una pausa fija que a veces
        # no alcanza si la página tarda un poco más de lo normal.
        campo_combo = wait.until(EC.element_to_be_clickable(
            (By.ID, "dnn_ctr394_Maestros_cbProceso_I")))
        campo_combo.click()
        time.sleep(0.6)
        opciones_combo = wait.until(EC.presence_of_all_elements_located(
            (By.CSS_SELECTOR, "td.dxeListBoxItem")))
        encontrado = False
        for opcion in opciones_combo:
            if opcion.text.strip() == "Tercero":
                opcion.click()
                encontrado = True
                break
        if not encontrado:
            log("❌ No se encontró la opción 'Tercero' en el combo de Maestros. "
                "Puede que el sitio haya cambiado.")
            return {}
        time.sleep(0.5)

        wait.until(EC.element_to_be_clickable(
            (By.ID, "dnn_ctr394_Maestros_btConsultar"))).click()
        time.sleep(2.5)  # la tabla puede tardar un poco en cargar

        wait.until(EC.presence_of_element_located(
            (By.ID, "dnn_ctr394_Maestro_tvDatos_DXMainTable")))

        # Se lee el encabezado para saber en qué columna está cada dato.
        # OJO: se descubrió que la fila de encabezados puede tener MÁS
        # columnas escondidas que la fila de datos real (no coinciden
        # 1 a 1), así que se valida contra una fila de datos real antes
        # de confiar en los índices del encabezado.
        fila_encabezado = driver.find_element(By.ID, "dnn_ctr394_Maestro_tvDatos_DXHeadersRow")
        encabezados = [c.text.strip() for c in fila_encabezado.find_elements(By.TAG_NAME, "td")]

        def indice_de(nombre_columna):
            for i, texto in enumerate(encabezados):
                if texto.lower() == nombre_columna.lower():
                    return i
            return None

        idx_identificacion = indice_de("Identificación")
        idx_nombre = indice_de("Nombre")
        idx_apellido1 = indice_de("Primer Apellido")
        idx_apellido2 = indice_de("Segundo Apellido")

        # Posiciones fijas ya confirmadas manualmente contra una fila de
        # datos real (el encabezado y los datos NO tienen la misma
        # cantidad de columnas en esta pantalla del RNDC).
        IDX_IDENTIFICACION_FIJO = 7
        IDX_NOMBRE_FIJO = 10
        IDX_APELLIDO1_FIJO = 11
        IDX_APELLIDO2_FIJO = 12

        filas_de_prueba = driver.find_elements(
            By.CSS_SELECTOR, "table#dnn_ctr394_Maestro_tvDatos_DXMainTable tr[id*='DXDataRow']"
        )
        columnas_reales = len(filas_de_prueba[0].find_elements(By.TAG_NAME, "td")) if filas_de_prueba else 0

        indices_del_encabezado_validos = (
            idx_identificacion is not None and idx_nombre is not None
            and columnas_reales > 0
            and idx_identificacion < columnas_reales and idx_nombre < columnas_reales
            and (idx_apellido1 is None or idx_apellido1 < columnas_reales)
            and (idx_apellido2 is None or idx_apellido2 < columnas_reales)
        )

        if indices_del_encabezado_validos:
            log(f"    (Columnas detectadas por encabezado -> Identificación: col {idx_identificacion}, "
                f"Nombre: col {idx_nombre}, Primer Apellido: col {idx_apellido1}, "
                f"Segundo Apellido: col {idx_apellido2})")
        else:
            log(f"    ⚠️  Los índices del encabezado no coinciden con las {columnas_reales} columnas "
                f"reales de los datos. Se usan las posiciones fijas ya confirmadas en su lugar "
                f"(Identificación: col {IDX_IDENTIFICACION_FIJO}, Nombre: col {IDX_NOMBRE_FIJO}, "
                f"Primer Apellido: col {IDX_APELLIDO1_FIJO}, Segundo Apellido: col {IDX_APELLIDO2_FIJO}).")
            idx_identificacion = IDX_IDENTIFICACION_FIJO
            idx_nombre = IDX_NOMBRE_FIJO
            idx_apellido1 = IDX_APELLIDO1_FIJO
            idx_apellido2 = IDX_APELLIDO2_FIJO

        def leer_pagina_actual(mostrar_diagnostico=False):
            filas = driver.find_elements(
                By.CSS_SELECTOR, "table#dnn_ctr394_Maestro_tvDatos_DXMainTable tr[id*='DXDataRow']"
            )
            if mostrar_diagnostico:
                log(f"    (Se encontraron {len(filas)} fila(s) crudas en esta página con el selector actual)")
                if filas:
                    primera = [c.text.strip() for c in filas[0].find_elements(By.TAG_NAME, "td")]
                    log(f"    (Ejemplo, primera fila completa: {primera})")
            encontrados = {}
            for fila in filas:
                celdas = fila.find_elements(By.TAG_NAME, "td")
                if len(celdas) <= max(idx_identificacion, idx_nombre):
                    continue
                cedula = celdas[idx_identificacion].text.strip()
                nombre = celdas[idx_nombre].text.strip()
                apellido1 = celdas[idx_apellido1].text.strip() if idx_apellido1 is not None and len(celdas) > idx_apellido1 else ""
                apellido2 = celdas[idx_apellido2].text.strip() if idx_apellido2 is not None and len(celdas) > idx_apellido2 else ""
                nombre_completo = " ".join(p for p in [nombre, apellido1, apellido2] if p)
                if cedula and nombre_completo:
                    encontrados[cedula] = nombre_completo
            return encontrados

        # Averiguar cuántas páginas hay en total, leyendo el resumen
        # "Page 1 of 134 (6693 items)" que muestra el propio sitio.
        total_paginas = 1
        try:
            texto_resumen = driver.find_element(By.CSS_SELECTOR, "td.dxpSummary_Aqua").text
            coincidencia = re.search(r"of\s+(\d+)", texto_resumen)
            if coincidencia:
                total_paginas = int(coincidencia.group(1))
        except Exception:
            pass  # si no se encuentra el resumen, se asume que es 1 sola página

        if total_paginas > 1:
            log(f"Se encontraron {total_paginas} páginas de resultados. Esto puede tardar varios "
                f"minutos (se recorre página por página) — no cierres esta ventana.")

        conductores = {}
        def esperar_a_que_cambie_de_pagina(numero_esperado, tiempo_max=8):
            """En vez de esperar un tiempo fijo (que a veces es de más y
            a veces de menos), revisa cada poquito si la página ya
            cambió de verdad, y sigue apenas esté lista. Si tarda más de
            lo normal, no se cuelga para siempre — sigue de todas formas
            tras el tiempo máximo."""
            objetivo = f"[{numero_esperado}]"
            inicio = time.time()
            while time.time() - inicio < tiempo_max:
                try:
                    indicador = driver.find_element(
                        By.CSS_SELECTOR, "td.dxpPageNumber_Aqua.dxpCurrentPageNumber_Aqua"
                    )
                    if indicador.text.strip() == objetivo:
                        return True
                except Exception:
                    pass
                time.sleep(0.15)
            return False

        conductores.update(leer_pagina_actual(mostrar_diagnostico=True))

        for pagina in range(2, total_paginas + 1):
            try:
                # Esta es la MISMA función que usa el propio sitio cuando
                # le das clic a un número de página — se llama
                # directamente, así funciona sin importar si el número
                # de página está visible o escondido detrás de "...".
                driver.execute_script(
                    f"aspxGVPagerOnClick('dnn_ctr394_Maestro_tvDatos', 'PN{pagina - 1}');"
                )
                esperar_a_que_cambie_de_pagina(pagina)
                conductores.update(leer_pagina_actual())
                if pagina % 20 == 0 or pagina == total_paginas:
                    log(f"    ...llevamos {pagina}/{total_paginas} páginas "
                        f"({len(conductores)} conductores hasta ahora).")
            except Exception as e:
                log(f"    ⚠️  No se pudo leer la página {pagina}/{total_paginas}: "
                    f"{traducir_error(e)}. Se sigue con la siguiente.")
                continue

        with open(ARCHIVO_CONDUCTORES, "w", encoding="utf-8") as f:
            json.dump(conductores, f, ensure_ascii=False, indent=2)

        log(f"✅ Se guardaron {len(conductores)} conductores/terceros en {ARCHIVO_CONDUCTORES} "
            f"(de {total_paginas} página(s) revisadas).")
        return conductores
    except Exception as e:
        log(f"❌ Error al consultar la lista de conductores: {traducir_error(e)}")
        try:
            driver.save_screenshot(ruta_captura("debug_conductores_error.png"))
            log("    Se guardó una captura 'debug_conductores_error.png' en la carpeta del "
                "programa, para ayudar a diagnosticar qué pasó.")
        except Exception:
            pass
        raise
    finally:
        driver.quit()
