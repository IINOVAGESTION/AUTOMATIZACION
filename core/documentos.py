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
                             id_boton_consultar=None):
    """Va a la página de 'Reimprimir', busca por el radicado, y descarga el
    PDF. Tiene su PROPIO reintento (hasta 3 veces) sin necesidad de volver
    a crear el documento, ya que el radicado ya existe y es válido."""
    for intento in range(1, 4):
        try:
            driver.get(url_reimprimir)
            try:
                wait.until(EC.presence_of_element_located((By.ID, id_campo_radicado)))
            except TimeoutException:
                pass
            time.sleep(0.6)

            campo = driver.find_element(By.ID, id_campo_radicado)
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
            log(f"    No se detectó la descarga (intento {intento}/3). Reintentando...")
        except (NoSuchElementException, UnexpectedAlertPresentException, TimeoutException) as e:
            log(f"    ⚠️  Error al descargar el PDF (intento {intento}/3): {traducir_error(e)}")
            try:
                alerta_pendiente = driver.switch_to.alert
                alerta_pendiente.accept()
            except NoAlertPresentException:
                pass
        time.sleep(1.5)

    log(f"⚠️  No se pudo descargar el PDF tras varios intentos. Puedes buscarlo manualmente "
        f"con el radicado {radicado} en el RNDC.")
    return None
