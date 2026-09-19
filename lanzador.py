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
import threading
import time

# Ver nota de arriba: necesarios aunque no se usen todos directamente.
import flask  # noqa: F401
import requests  # noqa: F401
import selenium  # noqa: F401
import webdriver_manager  # noqa: F401
import psutil
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
    if not ya_sembrada:
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

    # El token "de fábrica" (solo lectura, sube el límite de consultas
    # a GitHub de 60 a 5.000/hora) NO puede vivir en el código que se
    # sube a GitHub — el propio GitHub bloquea cualquier intento de
    # subir un token real a un repositorio, como medida de seguridad.
    # Por eso viaja empacado DENTRO del .exe (fuera de git por
    # completo) y se copia aquí a una carpeta aparte, por fuera de la
    # carpeta de código que el botón "Actualizar" reemplaza — así
    # sobrevive a las actualizaciones. Solo se copia si todavía no
    # existe, para no pisar un token más nuevo si algún día se
    # reconstruye el .exe con uno distinto.
    origen_token = os.path.join(ruta_semilla("semilla"), "token_por_defecto.txt")
    destino_token = os.path.join(carpeta_datos_appdata(), "token_por_defecto.txt")
    if os.path.exists(origen_token) and not os.path.exists(destino_token):
        shutil.copy2(origen_token, destino_token)


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


def limpiar_chromedriver_huerfano_de_antes():
    """Al ABRIR la app (antes de arrancar nada más): busca procesos de
    "chromedriver" que hayan quedado vivos de una sesión ANTERIOR de
    este mismo programa que se cerró mal (ej. la ventana se cerró
    mientras un viaje seguía corriendo, y el cierre forzado no le dio
    tiempo a Selenium de cerrar el navegador). Esos procesos quedan
    "huérfanos" — no pertenecen a ningún proceso vivo — y a veces
    bastan para que la próxima vez que se abre el programa, algo no
    arranque bien, obligando a cerrarlos a mano desde el Administrador
    de tareas antes de poder abrir la app.

    Solo mata "chromedriver" específicamente (nunca "chrome" a secas):
    un chromedriver.exe corriendo por su cuenta SIEMPRE es sobrante de
    algún programa de automatización (nadie lo abre para navegar), así
    que es seguro cerrarlo. Un chrome.exe normal, en cambio, podría ser
    una ventana de navegación real de la persona — ese nunca se toca
    aquí, para no cerrarle algo que esté usando de verdad."""
    try:
        for proceso in psutil.process_iter(["pid", "name"]):
            try:
                nombre = (proceso.info.get("name") or "").lower()
                if "chromedriver" in nombre:
                    proceso.terminate()
            except Exception:
                pass  # ese proceso en particular no se pudo cerrar (permisos, ya se cerró solo, etc.) — se sigue con los demás
    except Exception:
        pass  # si psutil falla por completo, no vale la pena bloquear el arranque por esto


def matar_procesos_hijos_huerfanos():
    """Al CERRAR la app (justo antes de terminar el proceso del todo):
    mata cualquier proceso que ESTE programa haya abierto y que siga
    vivo — típicamente chromedriver y el/los Chrome que haya lanzado —
    para no dejar nada huérfano que la próxima apertura tenga que
    limpiar. Es el complemento de limpiar_chromedriver_huerfano_de_antes():
    esa limpia lo que quedó de sesiones viejas: esta evita que la
    sesión ACTUAL deje algo nuevo regado."""
    try:
        proceso_actual = psutil.Process()
        hijos = proceso_actual.children(recursive=True)
        for hijo in hijos:
            try:
                hijo.terminate()
            except Exception:
                pass
        _, vivos = psutil.wait_procs(hijos, timeout=2)
        for hijo in vivos:
            try:
                hijo.kill()
            except Exception:
                pass
    except Exception:
        pass


def main():
    try:
        limpiar_chromedriver_huerfano_de_antes()
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
    # sin cambios, que siempre funcionó. Este bloque también va dentro
    # de su propio try/except: antes, si algo fallaba justo aquí (crear
    # o arrancar la ventana), el programa se quedaba como proceso vivo
    # en el Administrador de tareas SIN ninguna ventana y SIN ningún
    # aviso — exactamente el error que no se podía diagnosticar.
    #
    # Además de eso, hay un segundo caso que un try/except NO agarra:
    # que la ventana simplemente se CUELGUE al arrancar (sin lanzar
    # ningún error, solo sin terminar nunca) — pasa a veces con el
    # componente nativo de la ventana (WebView2/.NET) si algo lo
    # interrumpe en el momento justo (ej. el antivirus revisándolo).
    # Por eso hay un "vigilante" en un hilo aparte: si a los 20
    # segundos la ventana todavía no terminó de cargar, avisa con un
    # mensaje claro en vez de quedarse en silencio para siempre.
    ventana_cargo = {"ok": False}

    def vigilar_arranque_ventana():
        time.sleep(20)
        if not ventana_cargo["ok"]:
            mostrar_error_nativo(
                "Automatización RNDC - La ventana está tardando mucho",
                "Ya pasaron 20 segundos y la ventana todavía no termina de abrir. "
                "El programa sigue 'corriendo' (por eso aparece en el "
                "Administrador de tareas) pero algo está bloqueando la ventana "
                "misma — el sospechoso más común es el antivirus revisando un "
                "componente de la ventana justo en ese momento.\n\n"
                "Si el programa se queda así, ciérralo desde el Administrador "
                "de tareas (busca 'AutomatizacionRNDC') y ábrelo de nuevo.",
            )

    threading.Thread(target=vigilar_arranque_ventana, daemon=True).start()

    def marcar_ventana_cargada():
        ventana_cargo["ok"] = True

    try:
        webview.create_window(
            "Innova - Automatización RNDC",
            "http://localhost:5000",
            width=1250, height=850, min_size=(950, 650),
        )
        webview.start(marcar_ventana_cargada)
    except Exception as e:
        import traceback
        detalle = traceback.format_exc()
        mostrar_error_nativo(
            "Automatización RNDC - No se pudo abrir la ventana",
            f"El servidor interno sí arrancó bien, pero la ventana no pudo abrirse.\n\n"
            f"Error: {e}\n\nDetalle técnico:\n{detalle[-1200:]}\n\n"
            f"Si esto se repite seguido, puede ser el antivirus bloqueando un "
            f"componente de la ventana (WebView2/.NET) en el momento justo de "
            f"abrir. Intenta abrir el programa de nuevo.",
        )
        sys.exit(1)

    # Antes de cerrar del todo, se mata cualquier chromedriver/Chrome
    # que ESTA sesión haya dejado vivo (ver matar_procesos_hijos_huerfanos),
    # para que la próxima apertura no se tope con nada atascado.
    matar_procesos_hijos_huerfanos()

    # Al cerrar la ventana (la X, o Alt+F4), se fuerza el cierre
    # completo del proceso, para que el servidor no se quede vivo de
    # fondo ocupando el puerto la próxima vez que se abra el programa.
    os._exit(0)


if __name__ == "__main__":
    main()
