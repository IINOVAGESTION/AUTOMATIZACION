"""
lanzador.py — El punto de entrada de verdad del .exe.

Este archivo (y SOLO este) es lo que PyInstaller convierte en el .exe.
Su trabajo es simple a propósito:
  1. La primera vez que corre en una computadora, copia una "semilla"
     del código (servidor_web.py, core/, backend/, templates/, static/)
     a una carpeta externa y persistente (fuera del .exe).
  2. Carga servidor_web.py DESDE esa carpeta externa (no desde el
     interior sellado del .exe) y lo arranca.

¿Para qué? Para que el botón "Actualizar código" (dentro de la app)
pueda reemplazar el contenido de esa carpeta externa con lo último del
repositorio de GitHub, y que el cambio quede activo con solo reiniciar
el programa — sin tener que reconstruir ni volver a instalar el .exe
cada vez.

IMPORTANTE para quien construya el .exe: los imports de flask/selenium/
requests/webview de aquí abajo están puestos a propósito, aunque este
archivo no los use directamente — es la señal que necesita PyInstaller
para empacarlos DE TODAS FORMAS dentro del .exe (si no, al cargar
servidor_web.py desde la carpeta externa, esos import fallarían porque
PyInstaller nunca los habría incluido).
"""
import os
import sys
import shutil
import importlib

# Ver nota de arriba: estos imports son necesarios aunque no se usen
# directamente en este archivo.
import flask  # noqa: F401
import requests  # noqa: F401
import selenium  # noqa: F401
import webdriver_manager  # noqa: F401
try:
    import webview  # noqa: F401
except ImportError:
    pass


ELEMENTOS_SEMILLA = [
    "servidor_web.py", "rndc_core.py", "core", "backend", "templates", "static",
]


def ruta_semilla(nombre_carpeta):
    """Ruta a los archivos "semilla" que van empacados dentro del .exe,
    usados solo para el primer arranque en una computadora nueva."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, nombre_carpeta)


def carpeta_datos_appdata():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    carpeta = os.path.join(base, "AutomatizacionRNDC")
    os.makedirs(carpeta, exist_ok=True)
    return carpeta


def carpeta_codigo():
    c = os.path.join(carpeta_datos_appdata(), "app")
    os.makedirs(c, exist_ok=True)
    return c


def sembrar_si_hace_falta():
    """Si la carpeta de código externa está vacía (primer arranque en
    esta computadora, o alguien la borró), la llena con la copia que
    viene empacada dentro del .exe."""
    destino = carpeta_codigo()
    ya_sembrada = os.path.exists(os.path.join(destino, "servidor_web.py"))
    if ya_sembrada:
        return
    origen = ruta_semilla("semilla")
    for nombre in ELEMENTOS_SEMILLA:
        origen_item = os.path.join(origen, nombre)
        destino_item = os.path.join(destino, nombre)
        if not os.path.exists(origen_item):
            continue
        if os.path.isdir(origen_item):
            shutil.copytree(origen_item, destino_item, dirs_exist_ok=True)
        else:
            shutil.copy2(origen_item, destino_item)


def main():
    sembrar_si_hace_falta()
    destino = carpeta_codigo()

    # Se agrega la carpeta de código externa como el PRIMER lugar donde
    # Python busca módulos, para que "import servidor_web" cargue la
    # copia externa y actualizable (no una copia vieja empacada).
    sys.path.insert(0, destino)

    servidor_web = importlib.import_module("servidor_web")
    servidor_web.iniciar_app()


if __name__ == "__main__":
    main()
