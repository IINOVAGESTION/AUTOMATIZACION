"""
lanzador.py — El punto de entrada de verdad del .exe.

Este archivo (y SOLO este) es lo que PyInstaller convierte en el .exe,
y es DELIBERADAMENTE chico y estable: casi nunca debería hacer falta
tocarlo de nuevo.

CÓMO FUNCIONA:
  1. La primera vez que corre en una computadora (o si la carpeta de
     código externa no existe todavía), copia una "semilla" del código
     — servidor_web.py, rndc_core.py, core/, backend/, templates/,
     static/ — a una carpeta externa y persistente (fuera del .exe).
  2. Carga servidor_web.py DESDE esa carpeta externa (no desde el
     interior sellado del .exe) y arranca el servidor Flask en un hilo.
  3. Crea la ventana de escritorio (pywebview) — esta parte SIEMPRE
     corre con el "import webview" normal de aquí abajo, nunca desde el
     código cargado dinámicamente. Es la lección de la vuelta anterior:
     la ventana usa componentes nativos de Windows (.NET/pythonnet) que
     se confunden si el código que los llama no viene exactamente de
     donde el empaquetador (PyInstaller) espera. Separando esto, el
     resto del programa (rutas, lógica de negocio, lo que se le vaya
     agregando) sí se puede actualizar desde la app sin ese riesgo.

El botón "Actualizar" (dentro de la app) reemplaza el contenido de la
carpeta externa con lo último del repositorio de GitHub. El cambio
queda activo la próxima vez que se abre el programa (o de una vez, si
se usa el botón "Reiniciar ahora" que aparece después de actualizar).

IMPORTANTE para quien construya el .exe: los imports de flask/selenium/
requests/webdriver_manager/webview de aquí abajo están puestos a
propósito, aunque este archivo casi no los use directamente — es la
señal que necesita PyInstaller para empacarlos DE TODAS FORMAS dentro
del .exe (si no, al cargar servidor_web.py desde la carpeta externa,
esos imports fallarían porque PyInstaller nunca los habría incluido).
"""
import os
import sys
import shutil
import importlib

# Ver nota de arriba: necesarios aunque no se usen todos directamente.
import flask  # noqa: F401
import requests  # noqa: F401
import selenium  # noqa: F401
import webdriver_manager  # noqa: F401
import webview


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


def mostrar_error_nativo(titulo, mensaje):
    # El print() es solo un extra para cuando SÍ hay consola (ej. corriendo
    # "python lanzador.py" en desarrollo); en el .exe empacado con
    # --noconsole a veces la salida estándar usa una codificación vieja
    # (cp1252) que no entiende emojis, y sin este try/except ese error
    # tapaba el mensaje real que sí debía verse en el cuadro de diálogo.
    try:
        print("AVISO:", mensaje)
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, mensaje, titulo, 0x10)
    except Exception:
        pass


def main():
    try:
        sembrar_si_hace_falta()
        destino = carpeta_codigo()

        # Se agrega la carpeta de código externa como el PRIMER lugar
        # donde Python busca módulos, para que "import servidor_web"
        # cargue la copia externa y actualizable (no una copia vieja
        # empacada). El código que se carga así es PURO PYTHON — la
        # ventana no se toca desde aquí, ver la nota al inicio.
        sys.path.insert(0, destino)
        servidor_web = importlib.import_module("servidor_web")

        servidor_listo, mensaje_error = servidor_web.iniciar_servidor_flask_en_hilo()

        if servidor_listo is False:
            mostrar_error_nativo("Automatización RNDC - No se pudo iniciar", mensaje_error)
            sys.exit(1)
        elif servidor_listo is None:
            # Solo lentitud, no un error real: se avisa pero se sigue
            # intentando abrir la ventana de todas formas.
            try:
                print("AVISO:", mensaje_error)
            except Exception:
                pass

    except Exception as e:
        import traceback
        detalle = traceback.format_exc()
        mostrar_error_nativo(
            "Automatización RNDC - Error al iniciar",
            f"El programa no pudo arrancar.\n\nError: {e}\n\nDetalle técnico:\n{detalle[-1200:]}",
        )
        sys.exit(1)

    # A partir de aquí, TODO lo relacionado con la ventana usa el
    # "import webview" normal de arriba del archivo — el mismo código,
    # sin cambios, que siempre funcionó.
    webview.create_window(
        "Innova - Automatización RNDC",
        "http://localhost:5000",
        width=1250, height=850, min_size=(950, 650),
    )
    webview.start()

    # Al cerrar la ventana (la X, o Alt+F4), se fuerza el cierre
    # completo del proceso, para que el servidor no se quede vivo de
    # fondo ocupando el puerto la próxima vez que se abra el programa.
    os._exit(0)


if __name__ == "__main__":
    main()
