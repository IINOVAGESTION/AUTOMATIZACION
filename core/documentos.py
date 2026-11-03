"""
core/documentos.py — Descarga de los PDF de remesa/manifiesto ya
generados en el RNDC.
"""
import os
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoAlertPresentException, NoSuchElementException,
    UnexpectedAlertPresentException,
)

from .utilidades import (
    renombrar_descarga, traducir_error, carpetas_de_descarga_posibles, foto_de_carpetas,
)
from .config import obtener_carpeta_programa, EXTENSIONES_TEMPORALES

# Cuánto se espera, como máximo, a que aparezca el PDF después de pulsar
# imprimir. El RNDC a veces tarda bastante en armarlo.
TIEMPO_MAXIMO_DESCARGA = 90


def forzar_carpeta_descargas(driver, carpeta, log=None):
    """Le ordena a Chrome, directamente (no solo con la preferencia al
    abrirlo), que guarde lo que descargue en 'carpeta'. Con el modo
    invisible y tras algunas actualizaciones de Chrome la preferencia
    sola a veces se ignora y el archivo termina en otro lugar o no baja."""
    ruta = os.path.abspath(carpeta)
    os.makedirs(ruta, exist_ok=True)
    for comando, params in (
        ("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": ruta}),
        ("Page.setDownloadBehavior", {"behavior": "allow", "downloadPath": ruta}),
    ):
        try:
            driver.execute_cdp_cmd(comando, params)
            return True
        except Exception:
            continue
    if log:
        log("    (no se pudo forzar la carpeta de descargas en Chrome; se usa la configuración normal)")
    return False


def _bytes_pdf_de_url(driver, url):
    """Descarga el contenido de 'url' con la misma sesión del navegador y
    lo devuelve solo si de verdad es un PDF; si no, None."""
    if not url:
        return None
    try:
        if url.startswith("blob:"):
            b64 = driver.execute_async_script(
                "var url=arguments[0], cb=arguments[arguments.length-1];"
                "fetch(url).then(r=>r.blob()).then(b=>{var f=new FileReader();"
                "f.onloadend=function(){cb(f.result.split(',')[1]||'')};f.readAsDataURL(b);})"
                ".catch(function(){cb('')});", url)
            import base64
            datos = base64.b64decode(b64) if b64 else b""
        elif url.startswith("http"):
            import requests
            sesion = requests.Session()
            for c in driver.get_cookies():
                sesion.cookies.set(c["name"], c["value"], domain=c.get("domain"))
            agente = driver.execute_script("return navigator.userAgent")
            r = sesion.get(url, headers={"User-Agent": agente}, timeout=30)
            datos = r.content if r.status_code == 200 else b""
        else:
            return None
        return datos if datos[:5] == b"%PDF-" else None
    except Exception:
        return None


class _VentanaCargando(Exception):
    pass


def _rescatar_pdf_abierto_en_pantalla(driver, ventana_original, url_reimprimir, carpeta, nombre, log):
    """Si el RNDC, en vez de descargar el PDF, lo abrió en una pestaña
    nueva (o en la misma), lo guarda directamente desde ahí. Devuelve la
    ruta del archivo o None."""
    candidatas = []
    try:
        for h in driver.window_handles:
            if h != ventana_original:
                candidatas.append((h, True))
    except Exception:
        pass
    candidatas.append((ventana_original, False))
    resultado = None
    for handle, es_extra in candidatas:
        try:
            driver.switch_to.window(handle)
            url = driver.current_url
            if not es_extra and url.rstrip("/") == (url_reimprimir or "").rstrip("/"):
                continue
            if es_extra and url in ("about:blank", ""):
                # La ventana emergente todavía está cargando: no se cierra,
                # se vuelve a mirar en la próxima ronda.
                raise _VentanaCargando()
            datos = _bytes_pdf_de_url(driver, url)
            if datos:
                destino = os.path.join(carpeta, nombre + ".pdf")
                if os.path.exists(destino):
                    os.remove(destino)
                with open(destino, "wb") as f:
                    f.write(datos)
                log(f"✅ El RNDC abrió el PDF en pantalla en vez de descargarlo; se guardó directo en: {destino}")
                resultado = destino
        except _VentanaCargando:
            continue   # sin cerrarla
        except Exception:
            pass
        if es_extra:
            try:
                driver.close()
            except Exception:
                pass
        if resultado:
            break
    try:
        driver.switch_to.window(ventana_original)
    except Exception:
        pass
    return resultado


def _diagnostico_descarga_fallida(driver, carpetas_antes, nombre, log, estado_pagina=None):
    """Deja en el log lo necesario para entender POR QUÉ no bajó el
    archivo (versión de Chrome, dónde estaba el navegador, qué mensaje
    mostraba el sitio, qué había en las carpetas) y guarda una captura."""
    try:
        log(f"    🔎 Diagnóstico: Chrome {driver.capabilities.get('browserVersion', '?')} | "
            f"página actual: {driver.current_url} | ventanas abiertas: {len(driver.window_handles)}")
    except Exception:
        pass
    try:
        cuerpo = driver.find_element(By.TAG_NAME, "body").text
        mensajes = [l.strip() for l in cuerpo.splitlines()
                    if any(p in l.lower() for p in ("error", "no se", "no existe", "inválid", "invalid", "fall"))]
        if mensajes:
            log("    🔎 Mensajes del sitio: " + " | ".join(mensajes[:3])[:300])
    except Exception:
        pass
    if estado_pagina:
        log(f"    🔎 Estado de la página: {estado_pagina}")
    _actividad_de_red_reciente(driver, log)
    for carpeta, antes in carpetas_antes.items():
        try:
            nuevos = sorted(set(os.listdir(carpeta)) - antes)
        except Exception:
            nuevos = []
        if nuevos:
            log(f"    🔎 Archivos nuevos en '{carpeta}': {', '.join(nuevos[:5])}")
    log(f"    🔎 Carpetas vigiladas: {', '.join(carpetas_antes)}")
    try:
        ruta = os.path.join(obtener_carpeta_programa(), f"debug_pdf_{nombre}.png")
        driver.save_screenshot(ruta)
        log(f"    🔎 Captura guardada en: {ruta}")
    except Exception:
        pass



def _esperar_pagina_quieta(driver, timeout=10):
    """Espera a que la página termine de cargar y de hacer sus consultas
    internas (ASP.NET 'postback') antes de pulsar un botón. Si se pulsa
    'Imprimir' mientras la página todavía está recargándose por haber
    escrito el radicado, el clic se pierde y nunca baja el PDF -- parece
    que 'a veces baja y a veces no'. Devuelve True si se estabilizó."""
    js = ("return document.readyState==='complete' && !(window.Sys && Sys.WebForms && "
          "Sys.WebForms.PageRequestManager && Sys.WebForms.PageRequestManager.getInstance()"
          ".get_isInAsyncPostBack());")
    fin = time.time() + timeout
    seguidas = 0
    while time.time() < fin:
        try:
            quieta = bool(driver.execute_script(js))
        except Exception:
            quieta = True   # si no se puede preguntar, no se bloquea nada
        seguidas = seguidas + 1 if quieta else 0
        if seguidas >= 3:   # tranquila durante ~0.6 s seguidos
            return True
        time.sleep(0.2)
    return False



def _avisar_archivos_raros(carpeta, archivos_antes, vistos, log):
    """Si aparece un archivo nuevo que NO es PDF (ej. un .htm), lo deja en
    el log con un pedazo de su contenido: suele ser la respuesta de error
    del RNDC guardada como si fuera la descarga."""
    import re as _re
    try:
        nuevos = set(os.listdir(carpeta)) - archivos_antes
    except Exception:
        return
    for f in sorted(nuevos - vistos):
        vistos.add(f)
        if f.lower().endswith((".pdf",) + EXTENSIONES_TEMPORALES):
            continue
        try:
            ruta = os.path.join(carpeta, f)
            tamano = os.path.getsize(ruta)
            with open(ruta, "rb") as fh:
                crudo = fh.read(4000).decode("utf-8", errors="ignore")
            texto = _re.sub(r"<[^>]+>", " ", crudo)
            texto = _re.sub(r"\s+", " ", texto).strip()[:300]
            log(f"    🔎 Apareció un archivo que no es PDF: '{f}' ({tamano} bytes). Contenido: {texto or '(vacío)'}")
        except Exception:
            log(f"    🔎 Apareció un archivo que no es PDF: '{f}' (ya no se pudo leer).")


def _esperar_pdf(driver, ventana_original, url_reimprimir, carpeta, nombre, archivos_antes,
                 otras_carpetas_antes, log, maximo):
    """Espera el PDF hasta 'maximo' segundos mirando las dos formas en que
    puede llegar: como archivo descargado, o abierto en una ventana nueva
    (popup) de la que se guarda el contenido directamente."""
    fin = time.time() + maximo
    vistos = set()
    while time.time() < fin:
        resultado = renombrar_descarga(carpeta, nombre, archivos_antes, log, timeout=0.6,
                                        otras_carpetas_antes=otras_carpetas_antes, silencioso=True,
                                        solo_pdf=True)
        if resultado:
            return resultado
        _avisar_archivos_raros(carpeta, archivos_antes, vistos, log)
        try:
            hay_ventana_nueva = len(driver.window_handles) > 1
        except Exception:
            hay_ventana_nueva = False
        if hay_ventana_nueva:
            resultado = _rescatar_pdf_abierto_en_pantalla(
                driver, ventana_original, url_reimprimir, carpeta, nombre, log)
            if resultado:
                return resultado
    log(f"⚠️  No se detectó ninguna descarga nueva para renombrar a '{nombre}'.")
    return None


def _actividad_de_red_reciente(driver, log, cuantas=8):
    """Para el diagnóstico: las últimas peticiones de red que hizo la
    página (dirección, tipo de contenido y código de respuesta)."""
    import json
    try:
        eventos = driver.get_log("performance")
    except Exception:
        return
    filas = []
    for e in eventos:
        try:
            m = json.loads(e["message"])["message"]
            if m.get("method") == "Network.responseReceived":
                r = m["params"]["response"]
                filas.append(f"{r.get('status')} {r.get('mimeType', '?')} {r.get('url', '')[:110]}")
        except Exception:
            continue
    if filas:
        log("    🔎 Últimas respuestas de red: " + " || ".join(filas[-cuantas:]))



def descargar_pdf_documento(driver, wait, url_reimprimir, radicado, nombre_archivo,
                             carpeta_descargas, id_campo_radicado, id_boton_imprimir, log,
                             id_boton_consultar=None, intentos=1):
    """Va a la página de 'Reimprimir', busca por el radicado, y descarga el
    PDF. Por defecto intenta UNA sola vez: cuando el RNDC no responde,
    reintentar solo hacía esperar mucho más sin resultado, y el PDF se
    puede volver a pedir cuando se quiera desde Reimprimir (el radicado ya
    existe y es válido)."""
    for intento in range(1, intentos + 1):
        etapa = "abrir la página de Reimprimir"
        try:
            driver.get(url_reimprimir)
            # id_campo_radicado puede ser un solo id o una lista de ids
            # candidatos (se usa la primera casilla que exista en la página).
            candidatos = [id_campo_radicado] if isinstance(id_campo_radicado, str) else list(id_campo_radicado)
            try:
                wait.until(lambda d: any(d.find_elements(By.ID, c) for c in candidatos))
            except TimeoutException:
                pass
            time.sleep(0.6)

            campo = None
            for candidato in candidatos:
                encontrados = driver.find_elements(By.ID, candidato)
                if encontrados:
                    campo = encontrados[0]
                    break
            if campo is None:
                raise NoSuchElementException(
                    "No se encontró la casilla de búsqueda en la página de Reimprimir "
                    f"(se buscó: {', '.join(candidatos)})."
                )
            etapa = "escribir el radicado"
            campo.clear()
            campo.send_keys(radicado)
            campo.send_keys(Keys.TAB)
            time.sleep(1.1)
            _esperar_pagina_quieta(driver)

            if id_boton_consultar:
                driver.find_element(By.ID, id_boton_consultar).click()
                time.sleep(1.2)
                _esperar_pagina_quieta(driver)

            forzar_carpeta_descargas(driver, carpeta_descargas)
            archivos_antes = set(os.listdir(carpeta_descargas))
            otras_carpetas_antes = foto_de_carpetas(carpetas_de_descarga_posibles(carpeta_descargas))
            ventana_original = driver.current_window_handle
            inicio_espera = time.time()
            etapa = "pulsar el botón de imprimir"
            try:
                # Mientras el RNDC arma el PDF, el clic puede quedarse
                # "pensando" (Selenium espera a que la página termine). Se
                # le da más margen y, si igual se pasa, se sigue esperando
                # el archivo en vez de darlo por perdido.
                driver.set_page_load_timeout(45)
                driver.find_element(By.ID, id_boton_imprimir).click()
            except TimeoutException:
                log("    ℹ️ El RNDC está tardando en armar el PDF; se sigue esperando la descarga...")
                try:
                    driver.execute_script("window.stop()")
                except Exception:
                    pass
            finally:
                try:
                    driver.set_page_load_timeout(25)
                except Exception:
                    pass
            etapa = "esperar la descarga"
            log(f"    Esperando que el RNDC arme el PDF (hasta {TIEMPO_MAXIMO_DESCARGA} segundos)...")
            resultado = _esperar_pdf(driver, ventana_original, url_reimprimir, carpeta_descargas,
                                      nombre_archivo, archivos_antes, otras_carpetas_antes, log,
                                      TIEMPO_MAXIMO_DESCARGA)
            if resultado:
                log(f"    (el PDF tardó {time.time() - inicio_espera:.0f} segundos en aparecer)")
                return resultado
            estado = None
            try:
                campo_ahora = driver.find_element(By.ID, candidatos[0]) if len(candidatos) == 1 else None
                if campo_ahora is None:
                    for cand in candidatos:
                        hallados = driver.find_elements(By.ID, cand)
                        if hallados:
                            campo_ahora = hallados[0]
                            break
                boton_ahora = driver.find_element(By.ID, id_boton_imprimir)
                estado = (f"radicado escrito='{campo_ahora.get_attribute('value') if campo_ahora else '?'}', "
                          f"botón imprimir habilitado={boton_ahora.is_enabled()}, visible={boton_ahora.is_displayed()}")
            except Exception:
                pass
            _diagnostico_descarga_fallida(driver, otras_carpetas_antes, nombre_archivo, log, estado)
            log(f"    No se detectó la descarga (intento {intento}/{intentos})." + (" Reintentando..." if intento < intentos else ""))
        except (NoSuchElementException, UnexpectedAlertPresentException, TimeoutException) as e:
            log(f"    ⚠️  Error al descargar el PDF (intento {intento}/{intentos}) al {etapa}: {traducir_error(e)}")
            try:
                alerta_pendiente = driver.switch_to.alert
                alerta_pendiente.accept()
            except NoAlertPresentException:
                pass
        if intento < intentos:
            time.sleep(1.5)

    log(f"⚠️  No se pudo descargar el PDF (el RNDC puede estar lento o caído). Puedes volver a "
        f"pedirlo luego desde Herramientas → Reimprimir con el radicado {radicado}.")
    return None
