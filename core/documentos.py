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

from .utilidades import renombrar_descarga, traducir_error

def descargar_pdf_documento(driver, wait, url_reimprimir, radicado, nombre_archivo,
                             carpeta_descargas, id_campo_radicado, id_boton_imprimir, log,
                             id_boton_consultar=None, intentos=1):
    """Va a la página de 'Reimprimir', busca por el radicado, y descarga el
    PDF. Por defecto intenta UNA sola vez: cuando el RNDC no responde,
    reintentar solo hacía esperar mucho más sin resultado, y el PDF se
    puede volver a pedir cuando se quiera desde Reimprimir (el radicado ya
    existe y es válido)."""
    for intento in range(1, intentos + 1):
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
            campo.clear()
            campo.send_keys(radicado)
            campo.send_keys(Keys.TAB)
            time.sleep(1.1)

            if id_boton_consultar:
                driver.find_element(By.ID, id_boton_consultar).click()
                time.sleep(1.2)

            archivos_antes = set(os.listdir(carpeta_descargas))
            driver.find_element(By.ID, id_boton_imprimir).click()
            resultado = renombrar_descarga(carpeta_descargas, nombre_archivo, archivos_antes, log)
            if resultado:
                return resultado
            log(f"    No se detectó la descarga (intento {intento}/{intentos})." + (" Reintentando..." if intento < intentos else ""))
        except (NoSuchElementException, UnexpectedAlertPresentException, TimeoutException) as e:
            log(f"    ⚠️  Error al descargar el PDF (intento {intento}/{intentos}): {traducir_error(e)}")
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
