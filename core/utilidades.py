"""
core/utilidades.py — Funciones de apoyo usadas en todo el proceso de
automatización: escribir/leer campos del formulario RNDC, traducir
errores, normalizar textos y números, manejar descargas, etc.
"""
import time
import os
import re
import unicodedata
from datetime import datetime, timedelta
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoAlertPresentException, StaleElementReferenceException,
    NoSuchElementException, UnexpectedAlertPresentException, WebDriverException,
)

from .config import CARPETA_DESCARGAS_WINDOWS, EXTENSIONES_TEMPORALES


def calcular_fecha_pago_por_defecto(fecha_expedicion, fecha_descargue_por_defecto):
    """La Fecha de Pago, cuando no se escribe a mano, siempre debe quedar
    5 días después de la Fecha de Expedición (que a su vez puede ser hoy,
    o una fecha distinta que el usuario haya elegido). 'fecha_expedicion'
    viene en formato 'DD/MM/AAAA'. Si por algún motivo no se puede leer,
    se usa 'fecha_descargue_por_defecto' (hoy + 5 días) como respaldo."""
    try:
        dia, mes, anio = fecha_expedicion.split("/")
        fecha = datetime(int(anio), int(mes), int(dia)) + timedelta(days=5)
        return fecha.strftime("%d/%m/%Y")
    except Exception:
        return fecha_descargue_por_defecto


def calcular_retencion_ica(ciudad_origen, ciudad_destino, valor_por_defecto):
    """Rionegro (origen o destino) -> 8. Itagüí (origen o destino) -> 10.
    Cualquier otro caso -> el valor fijo de siempre."""
    texto = quitar_tildes(f"{ciudad_origen or ''} {ciudad_destino or ''}".upper())
    if "RIONEGRO" in texto:
        return "8"
    if "ITAGUI" in texto:
        return "10"
    return valor_por_defecto


def traducir_error(e):
    """Traduce errores técnicos comunes a un mensaje más claro en español.
    Si no reconoce el tipo de error, devuelve el texto técnico original
    (para no ocultar información útil si hay que reportarlo)."""
    nombre_clase = type(e).__name__
    texto_original = repr(e)

    traducciones = {
        "TimeoutException": "El sitio del RNDC tardó demasiado en responder.",
        "NoSuchElementException": "Un campo esperado no apareció en la página "
                                    "(puede ser un cambio temporal del sitio del RNDC).",
        "StaleElementReferenceException": "La página cambió justo en el momento en que el "
                                            "programa intentaba usar un campo.",
        "UnexpectedAlertPresentException": "El sitio mostró un aviso inesperado en la pantalla.",
        "WebDriverException": "Hubo un problema de conexión con el navegador o con el sitio del RNDC.",
        "ConnectionError": "No se pudo conectar a internet o al sitio del RNDC.",
        "ConnectionResetError": "Se perdió la conexión a internet o con el sitio del RNDC a mitad de camino.",
        "JSONDecodeError": "El sitio del RNDC devolvió una respuesta inesperada (no en el formato normal).",
        "FileNotFoundError": "No se encontró un archivo que el programa esperaba usar.",
        "PermissionError": "El programa no tiene permiso para leer/escribir un archivo "
                             "(puede que esté abierto en otro programa).",
    }

    if nombre_clase in traducciones:
        return f"{traducciones[nombre_clase]} (detalle técnico: {texto_original})"
    if nombre_clase == "TimeoutError":
        # Estos los lanza el propio programa (ej. la descarga del
        # chromedriver) ya redactados en español para la persona, así
        # que se muestran tal cual, sin envolverlos en el formato
        # técnico de Python (que se vería como TimeoutError('...')).
        return str(e)
    return texto_original


def limpiar_numero(valor):
    """Deja SOLO los dígitos de un texto (quita puntos, comas, guiones,
    espacios, letras, o cualquier otra cosa) por si un dato numérico
    (cédula, NIT, flete, peso) llega con algo extra sin querer, ej:
    '1.234.567', '900-123456-1', 'C.C. 123456'."""
    if not valor:
        return valor
    return re.sub(r"[^\d]", "", str(valor))


def normalizar_tipo_id(valor):
    """Convierte a código válido (C, N, P, E, T...) aunque venga escrito
    como palabra completa (ej: 'Cedula Ciudadania' -> 'C'). Es una capa
    extra de seguridad por si queda algún dato viejo guardado así."""
    if not valor:
        return valor
    valor_limpio = valor.strip()
    if len(valor_limpio) == 1:
        return valor_limpio.upper()
    mapa = {
        "CEDULA CIUDADANIA": "C", "CÉDULA CIUDADANÍA": "C", "CEDULA": "C", "CÉDULA": "C",
        "NIT": "N",
        "PASAPORTE": "P",
        "CEDULA EXTRANJERIA": "E", "CÉDULA EXTRANJERÍA": "E",
        "TARJETA IDENTIDAD": "T",
    }
    return mapa.get(valor_limpio.upper(), valor_limpio)


def set_text(driver, field_id, value, blur=True):
    el = driver.find_element(By.ID, field_id)
    el.clear()
    el.send_keys(value)
    if blur:
        el.send_keys(Keys.TAB)
        time.sleep(0.4)
    return el


def set_select(driver, field_id, value):
    campo = driver.find_element(By.ID, field_id)
    sel = Select(campo)
    sel.select_by_value(value)
    campo.send_keys(Keys.TAB)
    time.sleep(0.4)


def quitar_tildes(texto):
    """Quita tildes/diacríticos para comparar sin importar mayúsculas ni
    tildes (ej: 'NARIÑO' y 'NARINO' se tratan igual). El dato del RNDC a
    veces tiene inconsistencias de tildes dentro de la misma opción."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", texto)
        if not unicodedata.combining(c)
    )


def set_select_por_texto_parcial(driver, field_id, texto_parcial, log, espera_previa=1):
    time.sleep(espera_previa)
    sel = Select(driver.find_element(By.ID, field_id))
    objetivo = quitar_tildes(texto_parcial.strip().upper())

    for opcion in sel.options:
        if quitar_tildes(opcion.text.strip().upper()) == objetivo:
            sel.select_by_visible_text(opcion.text)
            time.sleep(0.4)
            return True

    coincidencias = [o for o in sel.options if objetivo in quitar_tildes(o.text.upper())]
    if not coincidencias:
        log(f"⚠️  No se encontró ninguna opción que contenga '{texto_parcial}' en {field_id}.")
        return False

    if len(coincidencias) > 1:
        opciones_texto = [o.text for o in coincidencias]
        log(f"⚠️  '{texto_parcial}' coincide con VARIAS opciones en {field_id}: {opciones_texto}")
        log(f"    Se eligió la primera por defecto: '{opciones_texto[0]}'.")

    sel.select_by_visible_text(coincidencias[0].text)
    time.sleep(0.4)
    return True


def _buscar_sugerencias_municipio(driver, wait, el, termino_busqueda, log, field_id):
    """Escribe un término de búsqueda y devuelve la lista de sugerencias
    visibles que aparecen (o [] si no aparece ninguna). Nunca debería
    quedarse "colgada" en silencio: siempre avisa qué está haciendo."""
    log(f"    Buscando municipio '{termino_busqueda}' en {field_id}...")
    try:
        el.click()
        el.clear()
        # Se escribe con JavaScript (en vez de send_keys) porque en Windows,
        # Selenium a veces no logra escribir bien tildes o la letra 'ñ'. Se
        # disparan los mismos eventos que el navegador dispara al escribir a
        # mano, para que el autocompletado del sitio reaccione igual.
        driver.execute_script("""
            var el = arguments[0];
            var texto = arguments[1];
            el.focus();
            el.value = texto;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('keyup', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        """, el, termino_busqueda)
        time.sleep(0.45)

        def hay_sugerencias_visibles(d):
            try:
                return any(
                    s.is_displayed()
                    for s in d.find_elements(By.CSS_SELECTOR, "ul.ui-autocomplete li.ui-menu-item")
                )
            except StaleElementReferenceException:
                return False

        # Antes esto reutilizaba el "wait" global de 20 segundos — pero
        # esta espera es solo para ver si el sitio SUGIERE algo (nada
        # que ver con confirmar un envío ni con evitar duplicados, así
        # que no hay ningún riesgo real en acortarla). Si el término no
        # trae sugerencias, el código YA prueba otra combinación de
        # palabras por su cuenta — con 20s por intento fallido, un
        # nombre de ciudad compuesto (corregimientos, veredas) podía
        # sumar 40-60+ segundos solo en intentos que no iban a servir.
        # 7 segundos sigue siendo bastante margen para una respuesta
        # normal del sitio, y corta mucho más rápido los intentos que
        # de verdad no tienen sugerencias.
        WebDriverWait(driver, 7).until(hay_sugerencias_visibles, message="timeout esperando sugerencias visibles")
        todas = driver.find_elements(By.CSS_SELECTOR, "ul.ui-autocomplete li.ui-menu-item")
        encontradas = [s for s in todas if s.is_displayed()]
        log(f"    -> {len(encontradas)} sugerencia(s) encontradas para '{termino_busqueda}'.")
        return encontradas
    except (TimeoutException, StaleElementReferenceException):
        log(f"    -> Sin sugerencias para '{termino_busqueda}' (tiempo agotado).")
        return []
    except Exception as e:
        log(f"    ⚠️  Error inesperado buscando '{termino_busqueda}': {traducir_error(e)}")
        return []


def set_autocomplete_municipio(driver, wait, field_id, texto_completo, log, indice=0, modo_flexible=False):
    el = driver.find_element(By.ID, field_id)
    palabras = texto_completo.strip().split()

    objetivo_norm = quitar_tildes(" ".join(texto_completo.strip().upper().split()))
    sugerencias = []
    termino_usado = palabras[0] if palabras else ""

    # El caso más común es que el departamento sea 1 sola palabra al final
    # (ej: "SAN ANTONIO DE PRADO MEDELLIN ANTIOQUIA" -> departamento
    # "ANTIOQUIA"). Se prueba primero con TODAS las palabras menos la
    # última, así casi siempre acierta en 1 sola búsqueda en vez de ir
    # probando de a una palabra (lo que sumaba varios segundos en nombres
    # de ciudad compuestos, como corregimientos).
    ordenes_de_busqueda = []
    if len(palabras) >= 2:
        ordenes_de_busqueda.append(len(palabras) - 1)
    ordenes_de_busqueda += [n for n in range(1, len(palabras)) if n not in ordenes_de_busqueda]

    for n_palabras in ordenes_de_busqueda:
        termino_usado = " ".join(palabras[:n_palabras])
        sugerencias = _buscar_sugerencias_municipio(driver, wait, el, termino_usado, log, field_id)
        if not sugerencias:
            continue
        if any(quitar_tildes(" ".join(s.text.strip().upper().split())) == objetivo_norm for s in sugerencias):
            break  # ya encontramos la búsqueda que trae la coincidencia exacta

    if not sugerencias:
        log(f"⚠️  No aparecieron sugerencias para '{termino_usado}' en {field_id}.")
        return False

    coincidencias_exactas = [
        s for s in sugerencias if quitar_tildes(" ".join(s.text.strip().upper().split())) == objetivo_norm
    ]

    if not coincidencias_exactas and not modo_flexible:
        opciones_texto = [s.text for s in sugerencias]
        log(f"⚠️  '{texto_completo}' no coincide EXACTO con ninguna opción en {field_id}.")
        log(f"    Opciones disponibles eran: {opciones_texto}")
        return False

    if not coincidencias_exactas and modo_flexible:
        opciones_texto = [s.text for s in sugerencias]
        elegida = sugerencias[indice % len(sugerencias)]
        log(f"⚠️  '{texto_completo}' no coincide exacto en {field_id}. "
            f"Opciones vistas: {opciones_texto}. Probando con: '{elegida.text}'...")
        elegida.click()
        time.sleep(0.35)
        el.send_keys(Keys.TAB)
        time.sleep(0.4)
        return True

    if indice >= len(coincidencias_exactas):
        if modo_flexible:
            indice = indice % len(coincidencias_exactas)
        else:
            log(f"⚠️  Solo hay {len(coincidencias_exactas)} opción(es) exacta(s) para "
                f"'{texto_completo}' en {field_id}.")
            return False

    elegida = coincidencias_exactas[indice]
    elegida.click()
    time.sleep(0.35)
    el.send_keys(Keys.TAB)
    time.sleep(0.4)
    return True


def esperar_nueva_descarga_en(carpeta, archivos_antes, timeout):
    fin = time.time() + timeout
    while time.time() < fin:
        try:
            archivos_ahora = set(os.listdir(carpeta))
        except FileNotFoundError:
            archivos_ahora = set()
        hay_descargas_en_progreso = any(f.endswith(EXTENSIONES_TEMPORALES) for f in archivos_ahora)
        nuevos = [f for f in (archivos_ahora - archivos_antes) if not f.endswith(EXTENSIONES_TEMPORALES)]
        # Se prioriza un archivo que ya sea .pdf de verdad; si no hay
        # ninguno todavía, se sigue esperando en vez de tomar cualquier
        # cosa a medias.
        nuevos_pdf = [f for f in nuevos if f.lower().endswith(".pdf")]
        if nuevos_pdf and not hay_descargas_en_progreso:
            return nuevos_pdf[0]
        if nuevos and not hay_descargas_en_progreso and time.time() > fin - 2:
            # Últimos 2 segundos del plazo: si apareció algo (aunque no
            # sea .pdf reconocible), se toma como respaldo en vez de
            # devolver nada.
            return nuevos[0]
        time.sleep(0.35)
    return None


def renombrar_descarga(carpeta, nombre_nuevo_sin_extension, archivos_antes, log, timeout=30):
    archivo = esperar_nueva_descarga_en(carpeta, archivos_antes, timeout=timeout)
    carpeta_encontrada = carpeta

    if not archivo:
        try:
            archivos_antes_win = set(os.listdir(CARPETA_DESCARGAS_WINDOWS))
        except FileNotFoundError:
            archivos_antes_win = set()
        archivo = esperar_nueva_descarga_en(CARPETA_DESCARGAS_WINDOWS, archivos_antes_win, timeout=5)
        carpeta_encontrada = CARPETA_DESCARGAS_WINDOWS

    if not archivo:
        log(f"⚠️  No se detectó ninguna descarga nueva para renombrar a '{nombre_nuevo_sin_extension}'.")
        return None

    extension = os.path.splitext(archivo)[1]
    origen = os.path.join(carpeta_encontrada, archivo)
    destino = os.path.join(carpeta, nombre_nuevo_sin_extension + extension)

    if os.path.exists(destino):
        os.remove(destino)

    os.rename(origen, destino)
    log(f"✅ Archivo descargado y renombrado a: {destino}")
    return destino


def esperar_confirmacion_manifiesto(driver, timeout=35):
    """timeout subido de 20s a 35s: con 20s, si el sitio tardaba en mostrar
    la confirmación, el programa asumía que el guardado había fallado y
    reiniciaba TODO el manifiesto desde cero -> terminaba creando un
    manifiesto duplicado aunque el primero sí se hubiera guardado bien."""
    fin = time.time() + timeout
    while time.time() < fin:
        try:
            alerta = driver.switch_to.alert
            texto_alerta = alerta.text
            alerta.accept()
            return ("error", texto_alerta)
        except NoAlertPresentException:
            pass

        elementos = driver.find_elements(By.ID, "dnn_ctr394_ManifiestoNew_lbIngreso")
        if elementos:
            return ("exito", elementos[0].text.strip())

        # Red de seguridad: a veces el sitio SÍ muestra el aviso de éxito
        # ("Manifiesto Creado") pero, por lo que sea (la página tardó en
        # terminar de cargar del todo, un cambio del sitio, etc.), la
        # casilla específica de donde normalmente se lee el radicado no
        # aparece. Antes, esto se trataba como "no se sabe qué pasó" y el
        # programa reiniciaba y reintentaba todo el manifiesto desde
        # cero — arriesgándose a crear un SEGUNDO manifiesto duplicado
        # sobre uno que en realidad ya se había guardado bien. Ahora, si
        # se ve ese aviso de éxito (aunque sea sin el radicado), se avisa
        # de inmediato y se detiene en vez de reintentar a ciegas.
        try:
            texto_pagina = driver.find_element(By.TAG_NAME, "body").text
            if "Manifiesto Creado" in texto_pagina:
                return ("exito_sin_radicado", texto_pagina[:400])
        except Exception:
            pass

        time.sleep(0.35)
    return (None, None)


def leer_radicado_y_aceptar_alerta(driver, wait, nombre_documento, log):
    radicado = None
    try:
        wait.until(EC.alert_is_present())
        alerta = driver.switch_to.alert
        texto_alerta = alerta.text
        log(f"Mensaje del sitio ({nombre_documento}): {texto_alerta}")
        coincidencia = re.search(r"Radicado:\s*(\d+)", texto_alerta)
        if coincidencia:
            radicado = coincidencia.group(1)
            log(f"✅ Radicado de {nombre_documento}: {radicado}")
        else:
            log(f"⚠️  No se pudo leer el radicado del {nombre_documento}.")
        alerta.accept()
    except (TimeoutException, NoAlertPresentException):
        log(f"⚠️  No apareció confirmación para {nombre_documento}. Revisa el navegador.")
    return radicado


def limpiar_capturas_viejas(dias=7, log=print):
    """Borra las capturas debug_*.png de más de 'dias' días, para que no
    se vayan acumulando con el tiempo."""
    carpeta = os.path.dirname(os.path.abspath(__file__))
    limite = time.time() - dias * 86400
    borradas = 0
    try:
        for nombre in os.listdir(carpeta):
            if nombre.startswith("debug_") and nombre.endswith(".png"):
                ruta = os.path.join(carpeta, nombre)
                try:
                    if os.path.getmtime(ruta) < limite:
                        os.remove(ruta)
                        borradas += 1
                except OSError:
                    pass
    except OSError:
        pass
    if borradas:
        log(f"🧹 Se borraron {borradas} captura(s) de diagnóstico de más de {dias} días.")
