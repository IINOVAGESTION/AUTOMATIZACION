"""
backend/actualizador.py — Descarga la última versión del código desde
GitHub (TODO: lógica y también interfaz) y la aplica a la carpeta
externa desde donde corre la app de verdad (ver lanzador.py). Como la
ventana (pywebview) vive separada en lanzador.py y nunca se toca desde
aquí, esto ya no arriesga que la app deje de abrir.

CÓMO FUNCIONA POR DENTRO:
  - lanzador.py (el .exe de verdad) NO trae el código "sellado" adentro:
    en el primer arranque, copia una semilla inicial a una carpeta
    externa y persistente y desde ahí corre siempre servidor_web.py.
    Esa carpeta externa SÍ se puede reemplazar en caliente.
  - Este módulo descarga el .zip del repositorio y reemplaza el
    contenido de esa carpeta externa con lo que venga del repositorio.
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
URL_INFO_COMMIT = "https://api.github.com/repos/IINOVAGESTION/AUTOMATIZACION/commits/main"


def _log_por_defecto(mensaje):
    """El log() por defecto de esta función es print(), y esta función
    se llama desde la ruta /actualizar_codigo del servidor SIN pasarle
    un log propio. En el .exe empacado con --noconsole, print() con un
    emoji (como el ✅ de más abajo) revienta con UnicodeEncodeError
    porque la salida estándar ahí usa una codificación vieja (cp1252)
    que no entiende emojis — y como esto pasaba DESPUÉS de aplicar la
    actualización con éxito, el navegador recibía un error 500 en vez
    de la confirmación, aunque el código sí se hubiera actualizado bien.
    Por eso el log por defecto queda protegido aquí."""
    try:
        print(mensaje)
    except Exception:
        pass

# Todo lo que sí se trae del repositorio y reemplaza la copia local.
# lanzador.py se deja afuera a propósito: ese es el único archivo que
# de verdad va sellado dentro del .exe y solo cambia reconstruyéndolo.
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
    try:
        ruta = ruta_token()
        if os.path.exists(ruta):
            with open(ruta, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def guardar_token(token):
    with open(ruta_token(), "w", encoding="utf-8") as f:
        f.write((token or "").strip())


def actualizar_desde_github(log=_log_por_defecto):
    """Descarga el .zip del repositorio y reemplaza el código local.
    El token es opcional: si el repositorio es público no hace falta
    ninguno. Devuelve (ok, mensaje)."""
    token = leer_token()

    log("Descargando la última versión desde GitHub...")
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

            destino = carpeta_codigo()
            log("Aplicando la actualización...")
            for nombre in ELEMENTOS_ACTUALIZABLES:
                origen_item = os.path.join(raiz_descarga, nombre)
                if not os.path.exists(origen_item):
                    continue
                destino_item = os.path.join(destino, nombre)
                destino_temporal = destino_item + "_nuevo"
                if os.path.isdir(origen_item):
                    if os.path.exists(destino_temporal):
                        shutil.rmtree(destino_temporal)
                    shutil.copytree(origen_item, destino_temporal)
                else:
                    if os.path.exists(destino_temporal):
                        os.remove(destino_temporal)
                    shutil.copy2(origen_item, destino_temporal)
                if os.path.exists(destino_item):
                    if os.path.isdir(destino_item):
                        shutil.rmtree(destino_item)
                    else:
                        os.remove(destino_item)
                os.replace(destino_temporal, destino_item)

            with open(os.path.join(destino, "_version_actualizada.json"), "w", encoding="utf-8") as f:
                json.dump(obtener_info_commit_actual(encabezados), f)

    except Exception as e:
        return False, f"Falló al aplicar la actualización: {e}"

    log("✅ Código actualizado correctamente.")
    return True, "Listo."


def obtener_info_commit_actual(encabezados):
    """Le pregunta a GitHub cuál es el último cambio (commit) de la rama
    principal — un identificador corto, la fecha, y el resumen del
    cambio — para poder mostrarlo en la app y así confirmar de un
    vistazo si de verdad se trajo lo más reciente. Si esta consulta
    extra falla por cualquier motivo (ej. el límite de consultas de
    GitHub sin token), no es grave: la actualización de código ya se
    aplicó de todas formas, simplemente no se sabrá el detalle exacto."""
    try:
        resp = requests.get(URL_INFO_COMMIT, headers=encabezados, timeout=15)
        if resp.status_code == 200:
            datos = resp.json()
            return {
                "sha_corto": datos.get("sha", "")[:7],
                "fecha": datos.get("commit", {}).get("author", {}).get("date", ""),
                "resumen": datos.get("commit", {}).get("message", "").split("\n")[0][:100],
            }
    except Exception:
        pass
    return {}


def leer_version_actual():
    """Lee la info de la última actualización aplicada (si alguna vez
    se usó el botón "Actualizar" en esta computadora). Devuelve un
    diccionario vacío si nunca se ha actualizado — en ese caso, la app
    sigue corriendo con la versión "de fábrica" que traía el .exe."""
    ruta = os.path.join(carpeta_codigo(), "_version_actualizada.json")
    if os.path.exists(ruta):
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
