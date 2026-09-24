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

from .config import FIJOS_REMESA, FIJOS_MANIFIESTO, CARPETA_DESCARGAS, URL_REIMPRIMIR_REMESA, URL_REIMPRIMIR_MANIFIESTO
from .utilidades import calcular_retencion_ica
from .navegador import crear_opciones_chrome, crear_driver_con_limite, obtener_chromedriver_path
from .documentos import descargar_pdf_documento

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
    caché de _obtener_sedes, no repite la consulta gigante), la que
    coincida con texto_buscar (sin importar mayúsculas) -- el mismo
    comportamiento que hoy tiene la lista desplegable de sedes en el
    sitio web. Devuelve un dict {'codigo_sede': ..., 'municipio': ...},
    o None si no encuentra ninguna que coincida.

    Cuando hay varias sedes con el mismo nombre (algunas empresas tienen
    entradas repetidas o antiguas), se prefiere la que tenga un código
    "limpio" (sin un '+' adelante) -- los códigos con '+' parecen venir
    de una carga antigua de datos, y hay indicios de que el RNDC a veces
    los interpreta mal (quitándoles el '+' y leyéndolos como un código
    totalmente distinto), lo que puede causar que el municipio del
    Manifiesto no coincida con el de la Remesa aunque ambos hayan
    buscado el mismo nombre de ciudad."""
    sedes = _obtener_sedes(usuario, password, nit_empresa, log=log)
    texto_buscar_norm = texto_buscar.strip().upper()
    coincidencias = [s for s in sedes if texto_buscar_norm in s["nombre"].upper()]
    if not coincidencias:
        return None

    limpias = [s for s in coincidencias if not s["codigo_sede"].startswith("+")]
    elegida = limpias[0] if limpias else coincidencias[0]

    if log:
        log(f"    '{texto_buscar}' -> sede {elegida['codigo_sede']} "
            f"({elegida['nombre']}, municipio {elegida['municipio']})"
            + (f" [de {len(coincidencias)} coincidencias, se evitaron las "
               f"que empiezan con '+']" if len(coincidencias) > 1 else ""))
    return {"codigo_sede": elegida["codigo_sede"], "municipio": elegida["municipio"]}


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
                          consecutivos_remesa, origen, destino,
                          cedula_titular, placa, placa_remolque,
                          cedula_conductor, cedula_conductor2,
                          flete, retencion_ica, retencion_fuente,
                          fopat_aplica, log=None,
                          municipio_intermedio_contiene=None, tipo_operacion="G"):
    """Crea un Manifiesto vía Web Service, uniéndolo a una o varias
    remesas ya creadas (consecutivos_remesa puede ser un texto -- un solo
    consecutivo -- o una lista, para Multiparada/Ida y Regreso). Calcula
    el FOPAT automáticamente (0.1% del flete) si fopat_aplica es True.
    'municipio_intermedio_contiene' es el texto de ciudad del punto de
    regreso (solo aplica a Ida y Regreso); 'tipo_operacion' es el código
    que espera el RNDC ("G" normal, "I" Ida y Regreso, "M" Multiparada).
    Devuelve el radicado; lanza ErrorRNDC si no."""
    if isinstance(consecutivos_remesa, str):
        consecutivos_remesa = [consecutivos_remesa]

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

    municipio_intermedio = None
    if municipio_intermedio_contiene:
        sede_intermedia = buscar_sede(usuario, password, nit_empresa,
                                       municipio_intermedio_contiene, log=log)
        if not sede_intermedia:
            raise ErrorRNDC(f"No se encontró ninguna sede que contenga "
                             f"'{municipio_intermedio_contiene}' para el punto intermedio "
                             f"(Ida y Regreso).")
        municipio_intermedio = sede_intermedia["municipio"]

    ahora = time.strftime("%d/%m/%Y")
    valor_fopat = round(float(flete) * 0.001) if fopat_aplica else None
    remesas_xml = "".join(
        f"<REMESA><CONSECUTIVOREMESA>{c}</CONSECUTIVOREMESA></REMESA>"
        for c in consecutivos_remesa
    )

    variables = f"""
<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>
<NUMMANIFIESTOCARGA>{consecutivo_manifiesto}</NUMMANIFIESTOCARGA>
<CODOPERACIONTRANSPORTE>{tipo_operacion}</CODOPERACIONTRANSPORTE>
<FECHAEXPEDICIONMANIFIESTO>{ahora}</FECHAEXPEDICIONMANIFIESTO>
<CODMUNICIPIOORIGENMANIFIESTO>{municipio_origen}</CODMUNICIPIOORIGENMANIFIESTO>
<CODMUNICIPIODESTINOMANIFIESTO>{municipio_destino}</CODMUNICIPIODESTINOMANIFIESTO>
{f'<CODMUNICIPIOINTERMEDIOMANIFIESTO>{municipio_intermedio}</CODMUNICIPIOINTERMEDIOMANIFIESTO>' if municipio_intermedio else ''}
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
{remesas_xml}
</REMESASMAN>
""".strip()

    respuesta = _llamar(usuario, password, tipo=1, procesoid=4,
                         variables_xml=variables, servidor="real_remesas")
    radicado, error = _extraer_radicado_o_error(respuesta)
    if error:
        raise ErrorRNDC(error, respuesta)
    return radicado


def _limpiar_numero(texto):
    """Deja solo los dígitos de un texto -- para números como Peso o
    Flete que la gente suele escribir con puntos o comas de miles
    ("3.000" o "3,000"). Sin esto, ese separador se manda tal cual al
    RNDC, que lo lee como un punto decimal -- "3.000" termina
    guardándose como el número 3 (tres), no tres mil."""
    return "".join(c for c in str(texto) if c.isdigit())


def ejecutar_viaje_api(v, usuario, password, log):
    """Crea la Remesa (o remesas, para Ida y Regreso) y el Manifiesto de
    un viaje usando el Web Service, en vez de manejar un navegador.
    Recibe 'v' con la misma forma que ya usa ejecutar_automatizacion, y
    devuelve un diccionario compatible con lo que ese devuelve, para que
    el resto de la app no tenga que cambiar.

    NOTA: por ahora cubre "Normal" e "Ida y Regreso" -- Multiparada,
    Récord (cola) y segundo conductor todavía no están conectados aquí;
    para esos, seguir usando ejecutar_automatizacion (Selenium) mientras
    se completa esta parte.
    """
    nit_empresa = FIJOS_REMESA["NUMIDPROPIETARIO"]
    resumen = []
    flete = _limpiar_numero(v["Flete"])

    if v.get("ModoPractica"):
        # No hay forma de "llenar pero no guardar" con el Web Service --
        # a diferencia del navegador, la llamada crea o no crea, sin punto
        # medio. Por eso aquí ni siquiera se llama a la API real: se avisa
        # y se devuelve un radicado de mentira, igual que hace Selenium.
        log(f"🎓 MODO PRÁCTICA: aquí se habrían creado la remesa y el manifiesto "
            f"{v['Consecutivo']}. No se creó nada real, no se descarga ningún PDF.")
        radicado_practica = f"PRACTICA-{int(time.time())}"
        return {
            "ok": True, "error": None,
            "resumen": [f"(modo práctica) Remesa/Manifiesto {v['Consecutivo']}"],
            "archivos": [],
            "radicado_remesa": radicado_practica,
            "radicado_manifiesto": radicado_practica,
        }

    try:
        # Arma la lista de tramos a crear como remesa: uno solo para un
        # viaje Normal, o varios (uno por parada, con letra) para Ida y
        # Regreso -- igual que hace Selenium.
        letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if v.get("IdaYRegreso"):
            tramos = [
                {
                    "consecutivo": f"{v['Consecutivo']}{letras[i]}",
                    "origen": parada["Origen"], "destino": parada["Destino"],
                    "producto": parada["Producto"], "peso": _limpiar_numero(parada["Peso"]),
                }
                for i, parada in enumerate(v["Paradas"])
            ]
        else:
            tramos = [{
                "consecutivo": v["Consecutivo"],
                "origen": v["Origen"], "destino": v["Destino"],
                "producto": v["Producto"], "peso": _limpiar_numero(v["Peso"]),
            }]

        for tramo in tramos:
            log(f"Creando remesa {tramo['consecutivo']} vía Web Service...")
            radicado_tramo = crear_remesa_api(
                usuario, password, nit_empresa, tramo["consecutivo"],
                origen=tramo["origen"], destino=tramo["destino"],
                producto_codigo=FIJOS_REMESA["CODIGOPRODUCTO"],
                descripcion_producto=tramo["producto"],
                peso_kg=tramo["peso"],
                sede_propietario_contiene=FIJOS_REMESA["SEDE_PROPIETARIO_CONTIENE"],
                log=log,
            )
            log(f"✅ Radicado de la remesa {tramo['consecutivo']}: {radicado_tramo}")
            resumen.append(f"Remesa {tramo['consecutivo']} -> radicado: {radicado_tramo}")
            tramo["radicado"] = radicado_tramo

        fopat_aplica = bool(v.get("Placa_Remolque"))
        origen_manifiesto = tramos[0]["origen"]
        destino_manifiesto = tramos[-1]["destino"]
        # Para Ida y Regreso, el punto intermedio (de retorno) es el
        # destino del primer tramo -- igual que hace Selenium.
        municipio_intermedio_contiene = (
            tramos[0]["destino"] if (v.get("IdaYRegreso") and len(tramos) >= 2) else None
        )
        tipo_operacion = "I" if v.get("IdaYRegreso") else "G"
        consecutivos_remesa = [t["consecutivo"] for t in tramos]

        retencion_ica = calcular_retencion_ica(
            origen_manifiesto, destino_manifiesto, FIJOS_MANIFIESTO["RETENCIONICA"]
        )

        log(f"Creando manifiesto {v['Consecutivo']} vía Web Service...")
        intentos_fopat = 0
        intentos_flete = 0
        while True:
            try:
                radicado_manifiesto = crear_manifiesto_api(
                    usuario, password, nit_empresa, v["Consecutivo"], consecutivos_remesa,
                    origen=origen_manifiesto, destino=destino_manifiesto,
                    cedula_titular=v["Cedula_Titular"], placa=v["Placa"],
                    placa_remolque=v.get("Placa_Remolque"),
                    cedula_conductor=v["Cedula_Conductor"],
                    cedula_conductor2=v.get("Cedula_Conductor2"),
                    flete=flete, retencion_ica=retencion_ica,
                    retencion_fuente="1",
                    fopat_aplica=fopat_aplica,
                    log=log,
                    municipio_intermedio_contiene=municipio_intermedio_contiene,
                    tipo_operacion=tipo_operacion,
                )
                break
            except ErrorRNDC as e:
                mensaje_mayus = e.mensaje.upper()
                es_por_fopat = "FOPAT" in mensaje_mayus or "PEAJE" in mensaje_mayus
                es_por_flete_bajo = any(
                    palabra in mensaje_mayus
                    for palabra in ("VALOR PACTADO MUY BAJO", "SICETAC", "COSTO MINIMO",
                                     "COSTOS EFICIENTES", "VALOR MINIMO")
                )
                if es_por_fopat and intentos_fopat < 2:
                    # Si el rechazo es específicamente por el FOPAT (falta o
                    # sobra), se ajusta y se reintenta -- unas pocas veces,
                    # para no ciclar sin fin si el rechazo fuera por otra razón.
                    intentos_fopat += 1
                    fopat_aplica = not fopat_aplica
                    log(f"    El RNDC rechazó por el FOPAT ({e.mensaje}) -- "
                        f"{'agregándolo' if fopat_aplica else 'quitándolo'} y reintentando...")
                elif es_por_flete_bajo and intentos_flete < 10:
                    # El flete no alcanza el mínimo (SiceTac u otra validación
                    # parecida) -- se sube de $300.000 en $300.000 y se
                    # reintenta, hasta 10 veces (hasta $3.000.000 de más).
                    intentos_flete += 1
                    flete = str(int(flete) + 300000)
                    log(f"    El RNDC rechazó por el valor del flete ({e.mensaje}) -- "
                        f"subiéndolo a {int(flete):,} y reintentando "
                        f"(intento {intentos_flete} de 10)...")
                else:
                    raise
        log(f"✅ Radicado del manifiesto: {radicado_manifiesto}")
        resumen.append(f"Manifiesto {v['Consecutivo']} -> radicado: {radicado_manifiesto}")

    except ErrorRNDC as e:
        log(f"❌ El RNDC rechazó la solicitud: {e.mensaje}")
        return {"ok": False, "error": e.mensaje, "resumen": resumen, "archivos": []}

    archivos = descargar_pdfs_del_viaje(usuario, password, v, tramos, radicado_manifiesto, log)
    return {
        "ok": True, "error": None, "resumen": resumen, "archivos": archivos,
        "radicado_remesa": tramos[0]["radicado"],
        "radicado_manifiesto": radicado_manifiesto,
    }


def descargar_pdfs_del_viaje(usuario, password, v, tramos, radicado_manifiesto, log):
    """Después de crear la(s) Remesa(s) y el Manifiesto por el Web
    Service, se abre un navegador SOLO para bajar los PDF (el Web
    Service no los entrega directamente) -- reutiliza exactamente la
    misma función de descarga que ya usa la vía de Selenium, así que el
    nombre y el formato del archivo quedan iguales de cualquiera de las
    dos formas. 'tramos' es la lista de remesas creadas (con su
    'consecutivo' y 'radicado'); para Ida y Regreso trae más de una, y
    cada PDF de remesa se descarga con su propia letra (_A, _B...) igual
    que hace Selenium. Devuelve la lista de nombres de archivo
    descargados -- si alguna descarga falla, no se considera un fallo
    del viaje en sí, ya se creó bien."""
    archivos = []
    driver = None
    letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    try:
        log("    Abriendo el navegador solo para descargar los PDF...")
        chrome_options = crear_opciones_chrome()
        driver = crear_driver_con_limite(chrome_options, obtener_chromedriver_path())
        driver.set_page_load_timeout(25)
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.common.by import By
        import time as _time
        import os as _os

        wait = WebDriverWait(driver, 20)
        driver.get("https://rndc2.mintransporte.gov.co/Ingresar/Iniciar-Sesión")
        _time.sleep(1)
        wait.until(lambda d: d.find_element(By.ID, "dnn_ctr390_FormLogIn_edUsername")).send_keys(usuario)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_edPassword").send_keys(password)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_btIngresar").click()
        _time.sleep(2)

        multiples_tramos = len(tramos) > 1
        for i, tramo in enumerate(tramos):
            sufijo = f"_{letras[i]}" if multiples_tramos else ""
            nombre_remesa = f"remesa{v['Placa']}{sufijo}"
            archivo_remesa = descargar_pdf_documento(
                driver, wait, URL_REIMPRIMIR_REMESA, tramo["radicado"], nombre_remesa,
                CARPETA_DESCARGAS, "dnn_ctr394_ReimprimirRemesa_RADICADO",
                "dnn_ctr394_ReimprimirRemesa_btImprimir", log,
            )
            if archivo_remesa:
                archivos.append(_os.path.basename(archivo_remesa))
                log(f"✅ PDF de la remesa {tramo['consecutivo']} descargado: "
                    f"{_os.path.basename(archivo_remesa)}")

        nombre_manifiesto = f"{v['Placa']}"
        archivo_manifiesto = descargar_pdf_documento(
            driver, wait, URL_REIMPRIMIR_MANIFIESTO, radicado_manifiesto, nombre_manifiesto,
            CARPETA_DESCARGAS, "dnn_ctr394_ReimprimirManifiesto_RADICADO",
            "dnn_ctr394_ReimprimirManifiesto_btImprimir", log,
            id_boton_consultar="dnn_ctr394_ReimprimirManifiesto_btConsultar",
        )
        if archivo_manifiesto:
            archivos.append(_os.path.basename(archivo_manifiesto))
            log(f"✅ PDF del manifiesto descargado: {_os.path.basename(archivo_manifiesto)}")

    except Exception as e:
        log(f"⚠️  No se pudieron descargar los PDF (el viaje ya quedó creado igual): {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return archivos
