"""
core/config.py — Constantes: URLs del RNDC, rutas de archivos y los
diccionarios de valores fijos (FIJOS_REMESA / FIJOS_MANIFIESTO).
No depende de ningún otro módulo del proyecto.
"""
import sys
import os

URL_LOGIN = "https://rndc2.mintransporte.gov.co/Ingresar/Iniciar-Sesión"


URL_LOGOUT = "https://rndc2.mintransporte.gov.co/Ingresar/Iniciar-Sesión/ctl/Logoff"


URL_REMESA = "https://rndc2.mintransporte.gov.co/logistica/ctl/Remesa/mid/394"


URL_REIMPRIMIR_REMESA = "https://rndc2.mintransporte.gov.co/logistica/ctl/ReimprimirRemesa/mid/394"


URL_REIMPRIMIR_MANIFIESTO = "https://rndc2.mintransporte.gov.co/logistica/ctl/ReimprimirManifiesto/mid/394"


URL_MANIFIESTO = "https://rndc2.mintransporte.gov.co/logistica/ctl/Manifiesto/mid/394"


URL_TERCERO = "https://rndc2.mintransporte.gov.co/logistica/ctl/tercero/mid/394"


URL_VEHICULO = "https://rndc2.mintransporte.gov.co/logistica/ctl/vehiculo/mid/394"


URL_MAESTRO = "https://rndc2.mintransporte.gov.co/logistica/ctl/Maestros/mid/394"


def obtener_carpeta_programa():
    """Carpeta donde vive el programa de verdad: al lado del .exe si está
    empaquetado, o al lado de este archivo .py si se corre normal. NO se
    usa __file__ directamente porque en un .exe de un solo archivo
    (--onefile), __file__ apunta a una carpeta TEMPORAL que se borra al
    cerrar el programa (por eso antes no se encontraban los PDFs)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def ruta_captura(nombre_archivo):
    """Ruta completa y persistente para guardar una captura de pantalla
    de diagnóstico (al lado del programa, no en una carpeta temporal)."""
    return os.path.join(obtener_carpeta_programa(), nombre_archivo)


CARPETA_DESCARGAS = os.path.join(os.path.expanduser("~"), "Downloads")


CARPETA_DESCARGAS_WINDOWS = CARPETA_DESCARGAS  # mismo lugar; se deja el nombre por compatibilidad


ARCHIVO_SEDES = os.path.join(obtener_carpeta_programa(), "sedes_empresa.json")


ARCHIVO_CONDUCTORES = os.path.join(obtener_carpeta_programa(), "conductores_empresa.json")


FIJOS_REMESA = {
    "TIPOIDPROPIETARIO": "N",
    "NUMIDPROPIETARIO": "9020412810",
    "SEDE_PROPIETARIO_CONTIENE": "BOGOTA",

    "OPERACIONTRANSPORTE": "P",          # Mercancía Consolidada
    "TIPOEMPAQUE_CONTIENE": "VARIOS",

    "TIPOIDREMITENTE": "N",
    "NUMIDREMITENTE": "9020412810",
    "HORACITAPACTADACARGUE": "08:00",
    "HORASPACTOCARGA": "1",
    "MINUTOSPACTOCARGA": "1",

    "TIPOIDDESTINATARIO": "N",
    "NUMIDDESTINATARIO": "9020412810",
    "HORACITAPACTADADESCARGUEREMESA": "08:00",
    "HORASPACTODESCARGUE": "1",
    "MINUTOSPACTODESCARGUE": "1",

    "NATURALEZACARGA": "1",              # Carga General
    "CODIGOPRODUCTO": "009800",

    "NOMUNIDADMEDIDAPRODUCTO": "KGM",
    "NOMUNIDADMEDIDACAPACIDAD": "1",

    "NOMDUENOPOLIZA": "N",               # No existe póliza
}


FIJOS_MANIFIESTO = {
    "TIPOMANIFIESTO": "G",
    "TIPOIDTITULAR": "C",
    "TIPOIDCONDUCTOR": "C",
    "RETENCIONICA": "9",
    "RESPONSABLEPAGOCARGUE": "R",
    "RESPONSABLEPAGODESCARGUE": "D",
    "RECOMENDACIONES": (
        "NO SE ASUME NINGUNA RESPONSABILIDAD SOBRE LA MERCANCIA "
        "TRANSPORTADA,POLIZA,PESO Y VALOR DE FLETE E IMPUESTOS LOS ASUME "
        "DIRECTAMENTE EL CONDUCTOR,EL VEHICULO LLEVA ELPESO PERMITIDO "
        "YLA MERCANCIA LICITA."
    ),
}


EXTENSIONES_TEMPORALES = (".crdownload", ".tmp", ".part", ".download")
