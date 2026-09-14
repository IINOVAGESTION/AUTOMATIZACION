"""
backend/actualizador.py — Descarga la última versión del código desde
GitHub y la aplica a la carpeta externa desde donde corre la app de
verdad (ver lanzador.py). Así, con un botón desde la propia app, se
puede traer lo último del repositorio sin tener que reconstruir ni
reinstalar el .exe cada vez.

CÓMO FUNCIONA POR DENTRO:
  - lanzador.py (el .exe de verdad) NO trae el código "sellado" adentro:
    en el primer arranque, copia una semilla inicial a una carpeta
    externa (ver carpeta_codigo() más abajo) y desde ahí corre siempre
    servidor_web.py. Esa carpeta externa SÍ se puede reemplazar en
    caliente.
  - Este módulo descarga el .zip del repositorio (privado, por eso hace
    falta un token de solo lectura) y reemplaza el contenido de esa
    carpeta externa con lo que venga del repositorio.
  - El cambio se aplica de verdad recién en el PRÓXIMO arranque del
    programa (por eso el botón ofrece "Reiniciar ahora" después).
"""
import os
import io
import json
import shutil
import zipfile
import tempfile
import requests

URL_REPO = "https://api.github.com/repos/IINOVAGESTION/AUTOMATIZACION/zipball/main"

# Carpetas/archivos que SÍ se traen del repositorio y reemplazan la copia
# local. Todo lo demás que haya en la carpeta de código (datos guardados,
# capturas de pantalla, etc.) se deja intacto.
ELEMENTOS_ACTUALIZABLES = [
    "servidor_web.py", "rndc_core.py", "core", "backend", "templates", "static",
]


def carpeta_datos_appdata():
    """Carpeta persistente por fuera de la carpeta de código, para que
    sobreviva a las actualizaciones (el token de acceso vive aquí, no
    dentro de la carpeta de código que se reemplaza por completo)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    carpeta = os.path.join(base, "AutomatizacionRNDC")
    os.makedirs(carpeta, exist_ok=True)
    return carpeta


def carpeta_codigo():
    """La carpeta externa y actualizable desde donde corre la app de
    verdad. lanzador.py la crea/siembra en el primer arranque."""
    c = os.path.join(carpeta_datos_appdata(), "app")
    os.makedirs(c, exist_ok=True)
    return c


def ruta_token():
    return os.path.join(carpeta_datos_appdata(), "token_actualizacion.txt")


def leer_token():
    ruta = ruta_token()
    if os.path.exists(ruta):
        with open(ruta, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def guardar_token(token):
    with open(ruta_token(), "w", encoding="utf-8") as f:
        f.write((token or "").strip())


def actualizar_desde_github(log=print):
    """Descarga el .zip del repositorio y reemplaza el código local.
    Devuelve (ok, mensaje)."""
    token = leer_token()
    if not token:
        return False, (
            "Todavía no se ha configurado el token de actualización en esta "
            "computadora. Pégalo en el campo de arriba y guárdalo primero."
        )

    log("Descargando la última versión del código desde GitHub...")
    try:
        resp = requests.get(
            URL_REPO,
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )
    except Exception as e:
        return False, f"No se pudo conectar a GitHub: {e}"

    if resp.status_code == 401:
        return False, "El token de actualización no es válido o ya venció. Genera uno nuevo y guárdalo aquí."
    if resp.status_code == 404:
        return False, "No se encontró el repositorio (o el token no tiene permiso sobre él)."
    if resp.status_code != 200:
        return False, f"GitHub respondió con un error ({resp.status_code}). Intenta de nuevo en un momento."

    log("Descarga lista. Extrayendo...")
    try:
        with tempfile.TemporaryDirectory() as carpeta_temporal:
            with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                z.extractall(carpeta_temporal)

            # GitHub empaqueta todo dentro de UNA sola subcarpeta con un
            # nombre variable (algo como "IINOVAGESTION-AUTOMATIZACION-<hash>").
            subcarpetas = [d for d in os.listdir(carpeta_temporal)
                           if os.path.isdir(os.path.join(carpeta_temporal, d))]
            if not subcarpetas:
                return False, "El .zip descargado llegó vacío o dañado."
            raiz_descarga = os.path.join(carpeta_temporal, subcarpetas[0])

            destino = carpeta_codigo()
            log("Aplicando la actualización...")
            for nombre in ELEMENTOS_ACTUALIZABLES:
                origen_item = os.path.join(raiz_descarga, nombre)
                destino_item = os.path.join(destino, nombre)
                if not os.path.exists(origen_item):
                    continue  # ese archivo/carpeta no vino en esta versión del repo, se deja como está
                if os.path.isdir(origen_item):
                    if os.path.exists(destino_item):
                        shutil.rmtree(destino_item)
                    shutil.copytree(origen_item, destino_item)
                else:
                    shutil.copy2(origen_item, destino_item)

            # Se guarda qué tan reciente quedó, solo para mostrarlo en la app.
            with open(os.path.join(destino, "_version_actualizada.json"), "w", encoding="utf-8") as f:
                json.dump({"commit_sha": resp.headers.get("ETag", "")}, f)

    except Exception as e:
        return False, f"Falló al aplicar la actualización: {e}"

    log("✅ Código actualizado correctamente.")
    return True, "Listo. Reinicia la app para que los cambios queden activos."
