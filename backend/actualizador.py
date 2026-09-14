"""
backend/actualizador.py — Descarga la última versión de la INTERFAZ
(templates/ y static/) desde GitHub y la deja guardada al lado del
.exe, en carpetas "templates_actualizado" y "static_actualizado".
resource_path() (en servidor_web.py) usa esas carpetas en vez de las
que vienen empacadas dentro del .exe, si existen.

Por qué solo la interfaz y no todo el código: el código Python
(servidor_web.py, core/, backend/) va compilado ADENTRO del propio
.exe — no se puede reemplazar en caliente sin arriesgar que el programa
no arranque (ya pasó, y quedaba como un proceso fantasma sin abrir
ninguna ventana). La interfaz sí se puede reemplazar de forma segura
porque Flask siempre la lee de una carpeta en disco en cada arranque,
sea cual sea esa carpeta.

Para cambios de LÓGICA (bugs, velocidad, reglas de negocio), sigue
haciendo falta generar un .exe nuevo con construir_exe.bat, como
siempre.
"""
import os
import io
import json
import shutil
import zipfile
import tempfile
import requests

import rndc_core

URL_REPO = "https://api.github.com/repos/IINOVAGESTION/AUTOMATIZACION/zipball/main"

# Solo estas dos carpetas se traen del repositorio. El resto del
# código (servidor_web.py, core/, backend/) NO se toca — eso solo
# cambia reconstruyendo el .exe.
ELEMENTOS_ACTUALIZABLES = ["templates", "static"]


def ruta_token():
    return os.path.join(rndc_core.obtener_carpeta_programa(), "token_actualizacion.txt")


def leer_token():
    ruta = ruta_token()
    if os.path.exists(ruta):
        with open(ruta, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def guardar_token(token):
    with open(ruta_token(), "w", encoding="utf-8") as f:
        f.write((token or "").strip())


def actualizar_interfaz_desde_github(log=print):
    """Descarga templates/ y static/ del repositorio y los deja listos
    para usarse (en "<carpeta>_actualizado", al lado del .exe). El
    cambio se aplica solo. Devuelve (ok, mensaje)."""
    token = leer_token()

    log("Descargando la última versión de la interfaz desde GitHub...")
    encabezados = {"Accept": "application/vnd.github+json"}
    if token:
        encabezados["Authorization"] = f"token {token}"
    try:
        resp = requests.get(URL_REPO, headers=encabezados, timeout=30)
    except Exception as e:
        return False, f"No se pudo conectar a GitHub: {e}"

    if resp.status_code == 401:
        return False, "El token de actualización no es válido o ya venció. Genera uno nuevo y guárdalo aquí."
    if resp.status_code == 404:
        if token:
            return False, "No se encontró el repositorio (o el token no tiene permiso sobre él)."
        return False, (
            "No se encontró el repositorio, o todavía es privado. Si sigue siendo "
            "privado, pega un token de solo lectura arriba y guárdalo."
        )
    if resp.status_code != 200:
        return False, f"GitHub respondió con un error ({resp.status_code}). Intenta de nuevo en un momento."

    log("Descarga lista. Extrayendo...")
    try:
        with tempfile.TemporaryDirectory() as carpeta_temporal:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                z.extractall(carpeta_temporal)

            subcarpetas = [d for d in os.listdir(carpeta_temporal)
                           if os.path.isdir(os.path.join(carpeta_temporal, d))]
            if not subcarpetas:
                return False, "El .zip descargado llegó vacío o dañado."
            raiz_descarga = os.path.join(carpeta_temporal, subcarpetas[0])

            log("Aplicando la actualización de la interfaz...")
            for nombre in ELEMENTOS_ACTUALIZABLES:
                origen_item = os.path.join(raiz_descarga, nombre)
                if not os.path.isdir(origen_item):
                    continue
                destino_item = os.path.join(
                    rndc_core.obtener_carpeta_programa(), f"{nombre}_actualizado"
                )
                # Se arma primero en una carpeta aparte y recién al final
                # se reemplaza de un solo golpe (os.replace), para que si
                # algo falla a la mitad no quede una carpeta a medio
                # escribir siendo usada por la app.
                destino_temporal = destino_item + "_nuevo"
                if os.path.exists(destino_temporal):
                    shutil.rmtree(destino_temporal)
                shutil.copytree(origen_item, destino_temporal)
                if os.path.exists(destino_item):
                    shutil.rmtree(destino_item)
                os.replace(destino_temporal, destino_item)

            with open(
                os.path.join(rndc_core.obtener_carpeta_programa(), "_version_interfaz.json"),
                "w", encoding="utf-8",
            ) as f:
                json.dump({"etag": resp.headers.get("ETag", "")}, f)

    except Exception as e:
        return False, f"Falló al aplicar la actualización: {e}"

    log("✅ Interfaz actualizada correctamente.")
    return True, "Listo. Reinicia la app para que se vea la interfaz nueva."
