"""
api_rndc.py -- Crea Remesas y Manifiestos usando el Web Service oficial del
RNDC (SOAP/XML) en vez de manejar un navegador con Selenium.

Es un módulo NUEVO y SEPARADO del resto de la automatización -- no
reemplaza core/automatizacion.py todavía. La idea es poder probarlo a
fondo por su cuenta antes de que la app lo use por defecto.

Requiere el paquete "zeep". Como el botón de "Actualizar" solo trae
archivos de código, no instala paquetes nuevos en el .exe ya armado, el
import se hace de forma opcional -- si zeep no está instalado (porque el
.exe no se ha reconstruido con este requisito todavía), el resto de la
app sigue funcionando normal, y solo la función de este módulo avisa
claro que hace falta reconstruir el .exe, en vez de tumbar la app entera.
"""
import os
import time
import xml.etree.ElementTree as ET

try:
    from zeep import Client
    from zeep.transports import Transport
    import requests
    _ZEEP_DISPONIBLE = True
except ImportError:
    _ZEEP_DISPONIBLE = False

from .config import FIJOS_REMESA, FIJOS_MANIFIESTO
from .utilidades import calcular_retencion_ica

CARPETA_WSDL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wsdl")

# "real" usa el RNDC de verdad. "pruebas" usa el ambiente de pruebas
# (rndcpruebas) -- pero para ESTA cuenta, "Grabar" ahí está bloqueado por
# el Ministerio por ahora; solo sirve para Consultar mientras se resuelve.
WSDL_POR_SERVIDOR = {
    "real_terceros": "rndc_real.wsdl",          # Tercero, Vehículo, consultas generales
    "real_remesas": "rndc_real_remesas.wsdl",   # Remesa, Manifiesto
    "pruebas": "rndc_pruebas.wsdl",
}

_clientes_cacheados = {}


class ErrorRNDC(Exception):
    """Se lanza cuando el RNDC responde con un <ErrorMSG> (o varios)."""
    def __init__(self, mensaje, xml_respuesta=None):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.xml_respuesta = xml_respuesta


def _cliente_para(servidor):
    if not _ZEEP_DISPONIBLE:
        raise ErrorRNDC(
            "El Web Service (experimental) todavía no está disponible en esta "
            "instalación -- hace falta reconstruir el .exe para incluir el "
            "paquete 'zeep'. Mientras tanto, desmarca la casilla de \"Usar Web "
            "Service\" y sigue usando el navegador normal."
        )
    if servidor not in _clientes_cacheados:
        ruta_wsdl = os.path.join(CARPETA_WSDL, WSDL_POR_SERVIDOR[servidor])
        sesion = requests.Session()
        sesion.timeout = 25
        transporte = Transport(session=sesion, timeout=25)
        _clientes_cacheados[servidor] = Client(wsdl=ruta_wsdl, transport=transporte)
    return _clientes_cacheados[servidor]


def _llamar(usuario, password, tipo, procesoid, variables_xml, documento_xml="",
            servidor="real_remesas"):
    """Arma el sobre <root> completo y lo manda. Devuelve el XML de texto
    que responde el RNDC, tal cual."""
    xml_pedido = (
        f"<?xml version='1.0' encoding='ISO-8859-1' ?>"
        f"<root><acceso><username>{usuario}</username>"
        f"<password>{password}</password></acceso>"
        f"<solicitud><tipo>{tipo}</tipo><procesoid>{procesoid}</procesoid></solicitud>"
        f"<variables>{variables_xml}</variables>"
        + (f"<documento>{documento_xml}</documento>" if documento_xml else "")
        + "</root>"
    )
    cliente = _cliente_para(servidor)
    return cliente.service.AtenderMensajeRNDC(Request=xml_pedido)


def _extraer_radicado_o_error(xml_respuesta):
    """Devuelve (radicado, None) si hubo éxito, o (None, mensaje_error) si
    el RNDC respondió con un ErrorMSG. Lanza ErrorRNDC si de plano no se
    puede sacar ni un radicado ni un mensaje de error."""
    texto = str(xml_respuesta)
    try:
        raiz = ET.fromstring(texto)
        ingresoid = raiz.find("ingresoid")
        if ingresoid is not None and ingresoid.text:
            return ingresoid.text.strip(), None
        error = raiz.find("ErrorMSG")
        if error is not None and error.text:
            return None, error.text.strip()
    except ET.ParseError:
        # A veces el mensaje de error del RNDC trae algún carácter que
        # rompe la lectura estricta de XML (una tilde o símbolo mal
        # codificado). En vez de perder el mensaje por completo, se busca
        # el contenido a mano entre las etiquetas, sin exigir que el resto
        # del documento sea perfecto.
        import re
        m = re.search(r"<ingresoid>(.*?)</ingresoid>", texto, re.DOTALL)
        if m and m.group(1).strip():
            return m.group(1).strip(), None
        m = re.search(r"<ErrorMSG>(.*?)</ErrorMSG>", texto, re.DOTALL)
        if m and m.group(1).strip():
            return None, m.group(1).strip()

    return None, f"Respuesta inesperada del RNDC: {xml_respuesta}"


_cache_sedes = {}  # (usuario, nit) -> lista de sedes, para no repetir la consulta gigante


def _obtener_sedes(usuario, password, nit_empresa, log=None):
    """Trae TODAS las sedes registradas para nit_empresa, una sola vez por
    corrida (se guarda en caché) -- antes se repetía esta consulta (que
    puede traer cientos de sedes) una vez por cada campo, inundando el
    log sin necesidad."""
    clave = (usuario, nit_empresa)
    if clave in _cache_sedes:
        return _cache_sedes[clave]

    variables = "CODSEDETERCERO,NOMSEDETERCERO,CODMUNICIPIORNDC"
    documento = (
        f"<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>"
        f"<CODTIPOIDTERCERO>'N'</CODTIPOIDTERCERO>"
        f"<NUMIDTERCERO>{nit_empresa}</NUMIDTERCERO>"
    )
    respuesta = _llamar(usuario, password, tipo=3, procesoid=11,
                         variables_xml=variables, documento_xml=documento,
                         servidor="real_terceros")
    try:
        raiz = ET.fromstring(str(respuesta))
    except ET.ParseError as e:
        if log:
            log(f"    ⚠️ No se pudo leer la lista de sedes: {e}")
        _cache_sedes[clave] = []
        return []

    sedes = []
    for doc in raiz.findall("documento"):
        sedes.append({
            "codigo_sede": doc.findtext("codsedetercero", default="").strip(),
            "nombre": doc.findtext("nomsedetercero", default="").strip(),
            "municipio": doc.findtext("codmunicipiorndc", default="").strip(),
        })
    if log:
        log(f"    ({len(sedes)} sedes encontradas para el NIT {nit_empresa})")
    _cache_sedes[clave] = sedes
    return sedes


def buscar_sede(usuario, password, nit_empresa, texto_buscar, log=None):
    """Busca, entre las sedes YA registradas para nit_empresa (usa la
    caché de _obtener_sedes, no repite la consulta gigante), la primera
    cuyo nombre contenga texto_buscar (sin importar mayúsculas) -- el
    mismo comportamiento que hoy tiene la lista desplegable de sedes en
    el sitio web. Devuelve un dict {'codigo_sede': ..., 'municipio': ...},
    o None si no encuentra ninguna que coincida."""
    sedes = _obtener_sedes(usuario, password, nit_empresa, log=log)
    texto_buscar_norm = texto_buscar.strip().upper()
    for sede in sedes:
        if texto_buscar_norm in sede["nombre"].upper():
            if log:
                log(f"    '{texto_buscar}' -> sede {sede['codigo_sede']} "
                    f"({sede['nombre']}, municipio {sede['municipio']})")
            return {"codigo_sede": sede["codigo_sede"], "municipio": sede["municipio"]}
    return None


def crear_remesa_api(usuario, password, nit_empresa, consecutivo_remesa,
                      origen, destino, producto_codigo, descripcion_producto,
                      peso_kg, sede_propietario_contiene, log=None):
    """Crea una Remesa vía Web Service.
    Devuelve el radicado (texto) si funciona; lanza ErrorRNDC si no."""

    sede_remitente = buscar_sede(usuario, password, nit_empresa, origen, log=log)
    if not sede_remitente:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga '{origen}' "
                         f"para el NIT {nit_empresa}. Hay que crearla en el RNDC primero.")

    sede_destinatario = buscar_sede(usuario, password, nit_empresa, destino, log=log)
    if not sede_destinatario:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga '{destino}' "
                         f"para el NIT {nit_empresa}. Hay que crearla en el RNDC primero.")

    sede_propietario = buscar_sede(usuario, password, nit_empresa,
                                    sede_propietario_contiene, log=log)
    if not sede_propietario:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga "
                         f"'{sede_propietario_contiene}' (propietario) para el NIT {nit_empresa}.")

    if log:
        log(f"    Sede remitente ({origen}): {sede_remitente['codigo_sede']} | "
            f"Sede destinatario ({destino}): {sede_destinatario['codigo_sede']} | "
            f"Sede propietario: {sede_propietario['codigo_sede']}")

    ahora = time.strftime("%d/%m/%Y")
    variables = f"""
<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>
<CONSECUTIVOREMESA>{consecutivo_remesa}</CONSECUTIVOREMESA>
<CODOPERACIONTRANSPORTE>P</CODOPERACIONTRANSPORTE>
<CODNATURALEZACARGA>1</CODNATURALEZACARGA>
<CANTIDADCARGADA>{peso_kg}</CANTIDADCARGADA>
<UNIDADMEDIDACAPACIDAD>1</UNIDADMEDIDACAPACIDAD>
<CODTIPOEMPAQUE>0</CODTIPOEMPAQUE>
<MERCANCIAREMESA>{producto_codigo}</MERCANCIAREMESA>
<DESCRIPCIONCORTAPRODUCTO>{descripcion_producto}</DESCRIPCIONCORTAPRODUCTO>
<CODTIPOIDREMITENTE>N</CODTIPOIDREMITENTE>
<NUMIDREMITENTE>{nit_empresa}</NUMIDREMITENTE>
<CODSEDEREMITENTE>{sede_remitente['codigo_sede']}</CODSEDEREMITENTE>
<CODTIPOIDDESTINATARIO>N</CODTIPOIDDESTINATARIO>
<NUMIDDESTINATARIO>{nit_empresa}</NUMIDDESTINATARIO>
<CODSEDEDESTINATARIO>{sede_destinatario['codigo_sede']}</CODSEDEDESTINATARIO>
<HORASPACTOCARGA>1</HORASPACTOCARGA>
<MINUTOSPACTOCARGA>0</MINUTOSPACTOCARGA>
<HORASPACTODESCARGUE>1</HORASPACTODESCARGUE>
<MINUTOSPACTODESCARGUE>0</MINUTOSPACTODESCARGUE>
<CODTIPOIDPROPIETARIO>N</CODTIPOIDPROPIETARIO>
<NUMIDPROPIETARIO>{nit_empresa}</NUMIDPROPIETARIO>
<CODSEDEPROPIETARIO>{sede_propietario['codigo_sede']}</CODSEDEPROPIETARIO>
<DUENOPOLIZA>N</DUENOPOLIZA>
<FECHACITAPACTADACARGUE>{ahora}</FECHACITAPACTADACARGUE>
<HORACITAPACTADACARGUE>08:00</HORACITAPACTADACARGUE>
<FECHACITAPACTADADESCARGUE>{ahora}</FECHACITAPACTADADESCARGUE>
<HORACITAPACTADADESCARGUEREMESA>10:00</HORACITAPACTADADESCARGUEREMESA>
""".strip()

    respuesta = _llamar(usuario, password, tipo=1, procesoid=3,
                         variables_xml=variables, servidor="real_remesas")
    radicado, error = _extraer_radicado_o_error(respuesta)
    if error:
        raise ErrorRNDC(error, respuesta)
    return radicado


def crear_manifiesto_api(usuario, password, nit_empresa, consecutivo_manifiesto,
                          consecutivo_remesa, origen, destino,
                          cedula_titular, placa, placa_remolque,
                          cedula_conductor, cedula_conductor2,
                          flete, retencion_ica, retencion_fuente,
                          fopat_aplica, log=None):
    """Crea un Manifiesto vía Web Service, uniéndolo a una remesa ya
    creada. Calcula el FOPAT automáticamente (0.1% del flete) si
    fopat_aplica es True. Devuelve el radicado; lanza ErrorRNDC si no."""

    sede_origen = buscar_sede(usuario, password, nit_empresa, origen, log=log)
    sede_destino = buscar_sede(usuario, password, nit_empresa, destino, log=log)
    if not sede_origen:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga '{origen}' "
                         f"para el NIT {nit_empresa}, no se puede saber el municipio de origen.")
    if not sede_destino:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga '{destino}' "
                         f"para el NIT {nit_empresa}, no se puede saber el municipio de destino.")
    municipio_origen = sede_origen["municipio"]
    municipio_destino = sede_destino["municipio"]

    ahora = time.strftime("%d/%m/%Y")
    valor_fopat = round(float(flete) * 0.001) if fopat_aplica else None

    variables = f"""
<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>
<NUMMANIFIESTOCARGA>{consecutivo_manifiesto}</NUMMANIFIESTOCARGA>
<CODOPERACIONTRANSPORTE>G</CODOPERACIONTRANSPORTE>
<FECHAEXPEDICIONMANIFIESTO>{ahora}</FECHAEXPEDICIONMANIFIESTO>
<CODMUNICIPIOORIGENMANIFIESTO>{municipio_origen}</CODMUNICIPIOORIGENMANIFIESTO>
<CODMUNICIPIODESTINOMANIFIESTO>{municipio_destino}</CODMUNICIPIODESTINOMANIFIESTO>
<CODIDTITULARMANIFIESTO>C</CODIDTITULARMANIFIESTO>
<NUMIDTITULARMANIFIESTO>{cedula_titular}</NUMIDTITULARMANIFIESTO>
<NUMPLACA>{placa}</NUMPLACA>
{f'<NUMPLACAREMOLQUE>{placa_remolque}</NUMPLACAREMOLQUE>' if placa_remolque else ''}
<CODIDCONDUCTOR>C</CODIDCONDUCTOR>
<NUMIDCONDUCTOR>{cedula_conductor}</NUMIDCONDUCTOR>
{f'<CODIDCONDUCTOR2>C</CODIDCONDUCTOR2><NUMIDCONDUCTOR2>{cedula_conductor2}</NUMIDCONDUCTOR2>' if cedula_conductor2 else ''}
<VALORFLETEPACTADOVIAJE>{flete}</VALORFLETEPACTADOVIAJE>
<RETENCIONFUENTEMANIFIESTO>{retencion_fuente}</RETENCIONFUENTEMANIFIESTO>
<RETENCIONICAMANIFIESTOCARGA>{retencion_ica}</RETENCIONICAMANIFIESTOCARGA>
{f'<RETENCIONFOPAT>{valor_fopat}</RETENCIONFOPAT>' if fopat_aplica else ''}
<CODMUNICIPIOPAGOSALDO>{municipio_destino}</CODMUNICIPIOPAGOSALDO>
<FECHAPAGOSALDOMANIFIESTO>{ahora}</FECHAPAGOSALDOMANIFIESTO>
<CODRESPONSABLEPAGOCARGUE>R</CODRESPONSABLEPAGOCARGUE>
<CODRESPONSABLEPAGODESCARGUE>D</CODRESPONSABLEPAGODESCARGUE>
<OBSERVACIONES>NO SE ASUME NINGUNA RESPONSABILIDAD SOBRE LA MERCANCIA TRANSPORTADA,POLIZA,PESO Y VALOR DE FLETE E IMPUESTOS LOS ASUME DIRECTAMENTE EL CONDUCTOR,EL VEHICULO LLEVA ELPESO PERMITIDO YLA MERCANCIA LICITA.</OBSERVACIONES>
<REMESASMAN procesoid="43">
<REMESA>
<CONSECUTIVOREMESA>{consecutivo_remesa}</CONSECUTIVOREMESA>
</REMESA>
</REMESASMAN>
""".strip()

    respuesta = _llamar(usuario, password, tipo=1, procesoid=4,
                         variables_xml=variables, servidor="real_remesas")
    radicado, error = _extraer_radicado_o_error(respuesta)
    if error:
        raise ErrorRNDC(error, respuesta)
    return radicado


def ejecutar_viaje_api(v, usuario, password, log):
    """Crea la Remesa y el Manifiesto de un viaje "Normal" (sin
    Multiparada/Récord todavía) usando el Web Service, en vez de manejar
    un navegador. Recibe 'v' con la misma forma que ya usa
    ejecutar_automatizacion, y devuelve un diccionario compatible con lo
    que ese devuelve, para que el resto de la app no tenga que cambiar.

    NOTA: por ahora solo cubre el viaje "Normal" -- Multiparada, Récord
    (cola) y segundo conductor todavía no están conectados aquí; para
    esos, seguir usando ejecutar_automatizacion (Selenium) mientras se
    completa esta parte.
    """
    nit_empresa = FIJOS_REMESA["NUMIDPROPIETARIO"]
    resumen = []

    try:
        log(f"Creando remesa {v['Consecutivo']} vía Web Service...")
        radicado_remesa = crear_remesa_api(
            usuario, password, nit_empresa, v["Consecutivo"],
            origen=v["Origen"], destino=v["Destino"],
            producto_codigo=FIJOS_REMESA["CODIGOPRODUCTO"],
            descripcion_producto=v["Producto"],
            peso_kg=v["Peso"],
            sede_propietario_contiene=FIJOS_REMESA["SEDE_PROPIETARIO_CONTIENE"],
            log=log,
        )
        log(f"✅ Radicado de la remesa: {radicado_remesa}")
        resumen.append(f"Remesa {v['Consecutivo']} -> radicado: {radicado_remesa}")

        fopat_aplica = bool(v.get("Placa_Remolque"))
        retencion_ica = calcular_retencion_ica(
            v["Origen"], v["Destino"], FIJOS_MANIFIESTO["RETENCIONICA"]
        )

        log(f"Creando manifiesto {v['Consecutivo']} vía Web Service...")
        radicado_manifiesto = crear_manifiesto_api(
            usuario, password, nit_empresa, v["Consecutivo"], v["Consecutivo"],
            origen=v["Origen"], destino=v["Destino"],
            cedula_titular=v["Cedula_Titular"], placa=v["Placa"],
            placa_remolque=v.get("Placa_Remolque"),
            cedula_conductor=v["Cedula_Conductor"],
            cedula_conductor2=v.get("Cedula_Conductor2"),
            flete=v["Flete"], retencion_ica=retencion_ica,
            retencion_fuente="1",
            fopat_aplica=fopat_aplica,
            log=log,
        )
        log(f"✅ Radicado del manifiesto: {radicado_manifiesto}")
        resumen.append(f"Manifiesto {v['Consecutivo']} -> radicado: {radicado_manifiesto}")

    except ErrorRNDC as e:
        log(f"❌ El RNDC rechazó la solicitud: {e.mensaje}")
        return {"ok": False, "error": e.mensaje, "resumen": resumen, "archivos": []}

    return {
        "ok": True, "error": None, "resumen": resumen, "archivos": [],
        "radicado_remesa": radicado_remesa,
        "radicado_manifiesto": radicado_manifiesto,
    }
