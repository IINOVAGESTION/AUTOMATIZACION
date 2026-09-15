"""
core/navegador.py — Todo lo relacionado con abrir/configurar Chrome
(Selenium): opciones del navegador, caché del chromedriver, y el
control de ventanas "abandonadas" cuando un viaje falla.
"""
import concurrent.futures
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

_RUTA_CHROMEDRIVER_CACHE = None


_navegadores_abandonados_por_error = []


_MAX_NAVEGADORES_ABANDONADOS = 2


def registrar_navegador_abandonado(driver, log):
    """Cuando un viaje falla y se deja el navegador abierto para revisar,
    esto lleva la cuenta de cuántos hay abiertos a la vez. Si ya hay
    demasiados (por varios errores seguidos, ej. en la Cola de viajes),
    cierra el más viejo automáticamente para no acumular ventanas de
    Chrome sin control y terminar congelando la computadora."""
    global _navegadores_abandonados_por_error
    while len(_navegadores_abandonados_por_error) >= _MAX_NAVEGADORES_ABANDONADOS:
        viejo = _navegadores_abandonados_por_error.pop(0)
        try:
            viejo.quit()
            log("    🧹 Se cerró automáticamente una ventana de un error anterior "
                "(ya había demasiadas abiertas a la vez).")
        except Exception:
            pass
    _navegadores_abandonados_por_error.append(driver)


def obtener_chromedriver_path():
    """Busca la ruta del chromedriver UNA SOLA VEZ por sesión del programa
    (ChromeDriverManager().install() hace una consulta a internet para
    verificar la versión cada vez que se llama, lo que suma varios
    segundos innecesarios si se abren muchos navegadores seguidos, como
    en la Cola de viajes).

    Esa consulta a internet no tenía ningún límite de tiempo: si la
    conexión de la computadora estaba lenta, bloqueada por un firewall/
    antivirus corporativo, o simplemente caída, el programa se quedaba
    esperando ahí PARA SIEMPRE sin mostrar ningún error (quedaba
    "Ejecutando..." sin avanzar, y ni siquiera llegaba a abrir Chrome).
    Por eso ahora se le pone un límite de 45 segundos."""
    global _RUTA_CHROMEDRIVER_CACHE
    if _RUTA_CHROMEDRIVER_CACHE is None:
        ejecutor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        futuro = ejecutor.submit(lambda: ChromeDriverManager().install())
        try:
            _RUTA_CHROMEDRIVER_CACHE = futuro.result(timeout=45)
        except concurrent.futures.TimeoutError:
            # shutdown(wait=False): si se usara "with" aquí (o el wait=True
            # de por defecto), Python esperaría a que el hilo colgado
            # termine antes de dejar salir esta función — justo lo que se
            # quiere evitar. Se deja ese hilo de fondo, huérfano, y se
            # sigue de inmediato para no bloquear el resto del programa.
            ejecutor.shutdown(wait=False)
            raise TimeoutError(
                "No se pudo preparar Chrome: la descarga/verificación del "
                "'chromedriver' (necesaria la primera vez, o tras una "
                "actualización de Chrome) tardó más de 45 segundos. Revisa "
                "que esta computadora tenga internet, y que el antivirus o "
                "firewall no esté bloqueando la conexión."
            )
        ejecutor.shutdown(wait=False)
    return _RUTA_CHROMEDRIVER_CACHE


def crear_driver_con_limite(chrome_options, chromedriver_path, timeout=45):
    """Crea el objeto Selenium que controla Chrome (webdriver.Chrome),
    pero con un límite de tiempo real — esa llamada NO tenía ninguno
    por defecto, así que si Chrome no terminaba de arrancar bien (puerto
    de depuración bloqueado por un antivirus, un perfil de Chrome
    trabado, etc.) el programa se quedaba esperando PARA SIEMPRE, sin
    ningún error, y ni siquiera cerrar la ventana de Chrome a mano lo
    liberaba (el programa no estaba "mirando" la ventana, estaba
    esperando una respuesta interna que nunca iba a llegar).

    Devuelve el driver ya listo, o lanza TimeoutError con un mensaje
    claro si se pasó del límite."""
    ejecutor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    futuro = ejecutor.submit(
        lambda: webdriver.Chrome(service=Service(chromedriver_path), options=chrome_options)
    )
    try:
        driver = futuro.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        ejecutor.shutdown(wait=False)  # no esperar al hilo colgado (ver obtener_chromedriver_path)
        raise TimeoutError(
            "Chrome no terminó de abrirse en 45 segundos. Esto suele pasar por un "
            "antivirus bloqueando el navegador, un perfil de Chrome dañado, o muy "
            "poca memoria/CPU libre en la computadora en ese momento. Si vuelve a "
            "pasar, intenta reiniciar la computadora."
        )
    ejecutor.shutdown(wait=False)

    # Esto es la protección más importante de las dos: pone un límite de
    # tiempo a NIVEL DE CADA ORDEN que Selenium le manda a Chrome de aquí
    # en adelante (abrir una página, maximizar la ventana, buscar un
    # campo, etc.), no solo a la apertura inicial. Sin esto, cualquier
    # orden individual (ej. driver.maximize_window(), que es justo lo que
    # sigue después de esta función) puede colgarse para siempre sin
    # ningún aviso, exactamente como pasaba antes con la apertura misma
    # de Chrome — y cerrar la ventana a mano tampoco libera nada, porque
    # el programa no está "mirando" la ventana, está esperando una
    # respuesta a una orden que ya se envió y nunca contestó.
    try:
        driver.command_executor.set_timeout(40)
    except Exception:
        pass  # si esta versión de Selenium no lo soporta, se sigue igual (mejor sin esto que fallar aquí)

    return driver


def crear_opciones_chrome(carpeta_descargas=None, invisible=False):
    """Opciones de Chrome compartidas. Las imágenes quedan desactivadas
    (no hacen falta para nada de lo que hace el script, y cada página
    carga más rápido sin ellas).
    invisible=True: corre el navegador SIN mostrar ninguna ventana
    (modo 'headless'). Útil para no llenar la pantalla de ventanas,
    pero si algo falla no hay ventana que revisar a simple vista, solo
    las capturas debug_*.png que el script guarda automáticamente."""
    chrome_options = Options()
    prefs = {"profile.managed_default_content_settings.images": 2}
    if carpeta_descargas:
        prefs.update({
            "download.default_directory": carpeta_descargas,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True,
        })
    chrome_options.add_experimental_option("prefs", prefs)
    if invisible:
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--window-size=1600,1000")

    # Banderas que bajan el uso de memoria/CPU de cada Chrome, y evitan
    # procesos de fondo que no hacen falta para nada de lo que hace el
    # script. También ayudan a que no se acumule tanto consumo si quedan
    # varias ventanas abiertas a la vez (por ejemplo, tras un error).
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--disable-background-timer-throttling")
    chrome_options.add_argument("--disable-backgrounding-occluded-windows")
    chrome_options.add_argument("--disable-renderer-backgrounding")
    chrome_options.add_argument("--disable-features=TranslateUI,Translate")
    chrome_options.add_argument("--log-level=3")  # menos texto de consola de Chrome
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])

    return chrome_options
