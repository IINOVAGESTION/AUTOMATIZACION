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
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

try:
    from zeep import Client
    from zeep.transports import Transport
    import requests
    _ZEEP_DISPONIBLE = True
except ImportError:
    _ZEEP_DISPONIBLE = False

from .config import FIJOS_REMESA, FIJOS_MANIFIESTO, CARPETA_DESCARGAS, URL_REIMPRIMIR_REMESA, URL_REIMPRIMIR_MANIFIESTO, URL_LOGIN, ARCHIVO_SEDES
from .utilidades import calcular_retencion_ica, quitar_tildes, normalizar_tipo_id, calcular_fecha_pago_por_defecto
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

# Segundos que se espera la respuesta del RNDC en cada llamada. IMPORTANTE:
# en zeep, Transport(timeout=...) solo vale para CARGAR el WSDL; el tiempo de
# espera de las llamadas es "operation_timeout", y por defecto es None =
# esperar PARA SIEMPRE. Antes la app se quedaba colgada para siempre (por
# ejemplo en "Verificando...") cuando el RNDC no contestaba.
TIMEOUT_OPERACION = 45


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
        transporte = Transport(session=sesion, timeout=25, operation_timeout=TIMEOUT_OPERACION)
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
    # Las consultas (tipo=3) se pueden reintentar sin riesgo -- no crean
    # ni modifican nada, así que un reintento ante un hipo de red es
    # seguro. Las que SÍ crean/modifican (tipo=1, Grabar) NUNCA se
    # reintentan aquí: si el RNDC alcanzó a procesarla pero la respuesta
    # se perdió por el mismo hipo, reintentar podría duplicarla -- eso lo
    # maneja (con cuidado) la lógica de reintentos de ejecutar_viaje_api,
    # no esta función genérica.
    intentos_maximos = 3 if tipo == 3 else 1
    ultimo_error = None
    for intento in range(1, intentos_maximos + 1):
        cliente = _cliente_para(servidor)
        try:
            return cliente.service.AtenderMensajeRNDC(Request=xml_pedido)
        except Exception as e:
            ultimo_error = e
            # Un timeout NO se reintenta (ya se esperó TIMEOUT_OPERACION
            # segundos; repetir solo alarga la espera cuando el RNDC está
            # caído). Los demás hipos de red sí se reintentan en consultas.
            es_timeout = _ZEEP_DISPONIBLE and isinstance(e, requests.exceptions.Timeout)
            if tipo == 3 and intento < intentos_maximos and not es_timeout:
                time.sleep(1.5)
                continue
            break
    # Un "Fault" de zeep es un error del protocolo SOAP en sí (no un
    # ErrorMSG normal del RNDC) -- antes se perdía el detalle y solo
    # quedaba un mensaje genérico tipo "Unknown fault occured". Se saca
    # todo lo que zeep sepa del fallo, para poder diagnosticarlo si se
    # repite.
    if _ZEEP_DISPONIBLE and isinstance(ultimo_error, (requests.exceptions.Timeout,)):
        aviso_escritura = (
            " OJO: como era una solicitud de CREAR/MODIFICAR, puede que el RNDC sí la haya recibido; "
            "revisa en el RNDC si quedó creada antes de repetirla."
            if tipo != 3 else ""
        )
        raise ErrorRNDC(
            f"El RNDC no respondió en {TIMEOUT_OPERACION} segundos (está lento o caído). "
            f"Intenta de nuevo en unos minutos.{aviso_escritura}",
            xml_pedido,
        )
    if _ZEEP_DISPONIBLE and isinstance(ultimo_error, requests.exceptions.ConnectionError):
        raise ErrorRNDC(
            "No se pudo conectar con el RNDC. Revisa tu conexión a internet; "
            "si está bien, el RNDC puede estar caído: intenta de nuevo en unos minutos.",
            xml_pedido,
        )
    detalle = str(ultimo_error)
    for atributo in ("message", "code", "detail", "subcodes"):
        valor = getattr(ultimo_error, atributo, None)
        if valor:
            detalle += f" | {atributo}: {valor}"
    raise ErrorRNDC(
        f"El RNDC devolvió un error de protocolo (no un rechazo normal): {detalle}",
        xml_pedido,
    )


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
        if error is not None:
            # A veces el RNDC anida el ErrorMSG adentro de otro ErrorMSG
            # (un formato raro que no se había visto antes) -- error.text
            # solo lee el texto DIRECTO, así que con itertext() se junta
            # el texto de adentro sin importar cuántos niveles tenga.
            texto_error = "".join(error.itertext()).strip()
            if texto_error:
                return None, texto_error
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


def _obtener_sedes(usuario, password, nit_empresa, log=None, forzar=False):
    """Trae TODAS las sedes registradas para nit_empresa (el Tercero del
    que se quieren las sedes -- puede ser la empresa misma, o un cliente
    remitente/destinatario distinto), una sola vez por corrida (se
    guarda en caché) -- antes se repetía esta consulta (que puede traer
    cientos de sedes) una vez por cada campo, inundando el log sin
    necesidad.

    Se consulta tanto con tipo 'N' (NIT) como 'C' (cédula) y se juntan
    los resultados -- un mismo Tercero puede tener sedes registradas
    bajo cualquiera de los dos tipos (se ha visto en la práctica), y no
    hay forma de saber de antemano cuál es el correcto solo con el
    número."""
    clave = (usuario, nit_empresa)
    if forzar:
        # "Actualizar ciudades" / una sede que no aparece: se vuelve a
        # preguntar al RNDC en vez de usar la lista guardada (que puede ser
        # vieja si la sede se creó después en la página del RNDC).
        _cache_sedes.pop(clave, None)
    if clave in _cache_sedes:
        return _cache_sedes[clave]

    variables = "CODSEDETERCERO,NOMSEDETERCERO,CODMUNICIPIORNDC"
    sedes = []
    hubo_fallo = False
    for tipo_id in ("N", "C"):
        documento = (
            # NUMNITEMPRESATRANSPORTE siempre es la empresa transportadora
            # (quién pregunta), NUNCA el Tercero que se está consultando --
            # antes se mandaba el mismo NIT en los dos campos, y para
            # cualquier cliente que no fuera la empresa misma, eso armaba
            # una consulta sin sentido que el RNDC no podía responder
            # (siempre volvía vacía, aunque el Tercero sí tuviera sedes).
            f"<NUMNITEMPRESATRANSPORTE>{FIJOS_REMESA['NUMIDPROPIETARIO']}</NUMNITEMPRESATRANSPORTE>"
            f"<CODTIPOIDTERCERO>'{tipo_id}'</CODTIPOIDTERCERO>"
            f"<NUMIDTERCERO>{nit_empresa}</NUMIDTERCERO>"
        )
        respuesta = _llamar(usuario, password, tipo=3, procesoid=11,
                             variables_xml=variables, documento_xml=documento,
                             servidor="real_terceros")
        try:
            raiz = ET.fromstring(str(respuesta))
        except ET.ParseError as e:
            hubo_fallo = True
            if log:
                log(f"    ⚠️ No se pudo leer la lista de sedes de {nit_empresa} "
                    f"(tipo {tipo_id}, se reintentará en la próxima búsqueda): {e}")
            continue

        for doc in raiz.findall("documento"):
            sedes.append({
                "codigo_sede": doc.findtext("codsedetercero", default="").strip(),
                "nombre": doc.findtext("nomsedetercero", default="").strip(),
                "municipio": _codigo_municipio_rndc(doc.findtext("codmunicipiorndc", default="").strip()),
                "tipo_id": tipo_id,
            })

    if log:
        log(f"    ({len(sedes)} sedes encontradas para el NIT {nit_empresa})")
    if hubo_fallo:
        # Si UNA de las dos consultas falló de verdad (no solo vino
        # vacía), no se cachea nada -- para que se reintenten ambas en
        # la próxima búsqueda, en vez de quedarse con una lista a medias.
        return sedes
    _cache_sedes[clave] = sedes
    return sedes


# Códigos DANE de departamento (los 2 primeros dígitos del código de municipio
# del RNDC, ej. 76863000 -> 76 = Valle del Cauca). Sirve para que, cuando se
# escribe "VERSALLES VALLE DEL CAUCA", no se elija una sede llamada VERSALLES
# que en realidad queda en otro departamento (ej. Santa Bárbara, Antioquia).
_DEPARTAMENTOS_DANE = {
    "05": ["ANTIOQUIA"], "08": ["ATLANTICO"], "11": ["BOGOTA D C", "BOGOTA DC", "BOGOTA", "D C", "DC", "CUNDINAMARCA D C"],
    "13": ["BOLIVAR"], "15": ["BOYACA"], "17": ["CALDAS"], "18": ["CAQUETA"], "19": ["CAUCA"],
    "20": ["CESAR"], "23": ["CORDOBA"], "25": ["CUNDINAMARCA"], "27": ["CHOCO"], "41": ["HUILA"],
    "44": ["LA GUAJIRA", "GUAJIRA"], "47": ["MAGDALENA"], "50": ["META"], "52": ["NARINO"],
    "54": ["NORTE DE SANTANDER", "NORTE SANTANDER", "N DE SANTANDER", "N SANTANDER"],
    "63": ["QUINDIO"], "66": ["RISARALDA"], "68": ["SANTANDER"], "70": ["SUCRE"], "73": ["TOLIMA"],
    "76": ["VALLE DEL CAUCA", "VALLE"], "81": ["ARAUCA"], "85": ["CASANARE"], "86": ["PUTUMAYO"],
    "88": ["SAN ANDRES Y PROVIDENCIA", "SAN ANDRES"], "91": ["AMAZONAS"], "94": ["GUAINIA"],
    "95": ["GUAVIARE"], "97": ["VAUPES"], "99": ["VICHADA"],
}


def _codigo_municipio_rndc(codigo):
    """Código de municipio en su forma completa de 8 dígitos (ej. 05792000).
    El listado de sedes del RNDC lo entrega como número y pierde el cero de
    adelante (Tarso, Antioquia llega como 5792000). Al crear el manifiesto,
    el RNDC compara el municipio destino con el de las remesas (MAN354:
    "Debe asociar mínimo una Remesa con el mismo municipio destino"), así
    que se manda siempre completo, como en los ejemplos de la guía
    (11001000, 76001000)."""
    digitos = "".join(c for c in str(codigo or "") if c.isdigit())
    return digitos.zfill(8) if digitos else str(codigo or "").strip()


def _depto_de_municipio(codigo):
    """Código DANE de departamento (2 dígitos) de un código de municipio del
    RNDC. OJO: el RNDC guarda el código como número y le pierde el cero de
    adelante (Barranquilla 08001000 llega como 8001000), así que primero se
    rellena a 8 dígitos; si no, se leería 80 en vez de 08."""
    digitos = "".join(c for c in str(codigo or "") if c.isdigit())
    return digitos.zfill(8)[:2] if digitos else ""


def _departamento_en_texto(texto):
    """Si el texto termina con el nombre de un departamento (y antes de él hay
    al menos una palabra, la ciudad), devuelve su código DANE de 2 dígitos;
    si no, None. Ej: 'VERSALLES VALLE DEL CAUCA' -> '76'."""
    limpio = "".join(c if c.isalnum() or c == " " else " " for c in quitar_tildes(str(texto).upper()))
    palabras = limpio.split()
    for n in range(min(4, len(palabras) - 1), 0, -1):
        cola = " ".join(palabras[-n:])
        for codigo, nombres in _DEPARTAMENTOS_DANE.items():
            if cola in nombres:
                return codigo
    return None


def _buscar_sede_en(sedes, texto_buscar, log=None):
    """Busca, entre las sedes YA registradas para nit_empresa (usa la
    caché de _obtener_sedes, no repite la consulta gigante), la que
    coincida con texto_buscar. Usa la misma estrategia que ya usa
    Selenium para el autocompletado de ciudades: primero intenta una
    coincidencia EXACTA (sin tildes ni mayúsculas) probando con el texto
    completo y, si no hay, quitando palabras del final una por una --
    porque el departamento suele venir al final (ej: buscar
    "APARTADO ANTIOQUIA" debe encontrar una sede que solo se llama
    "APARTADO"). Si tampoco hay ninguna coincidencia exacta así, cae a
    una coincidencia parcial (que el texto buscado esté contenido en el
    nombre de la sede, o al revés). Devuelve un dict
    {'codigo_sede': ..., 'municipio': ...}, o None si no encuentra nada.

    Cuando hay varias sedes que coinciden, se prefiere la que tenga un
    código "limpio" (sin un '+' adelante) -- los códigos con '+' parecen
    venir de una carga antigua de datos, y hay indicios de que el RNDC a
    veces los interpreta mal (quitándoles el '+' y leyéndolos como un
    código totalmente distinto)."""
    departamento = _departamento_en_texto(texto_buscar)

    def normalizar(texto):
        return quitar_tildes(" ".join(texto.strip().upper().split()))

    def del_departamento(coincidencias, es_nombre_completo=False):
        """Si el texto trae departamento, solo valen las sedes de ESE
        departamento (según su código de municipio). Una sede sin código de
        municipio no se puede verificar, así que no se descarta. Si el texto
        es EXACTAMENTE el nombre de la sede (tal como lo ofrece la lista del
        RNDC), no se filtra: ya es justo la que se escogió."""
        if not departamento or es_nombre_completo:
            return coincidencias
        validas = [s for s in coincidencias
                   if not s["municipio"] or _depto_de_municipio(s["municipio"]) == departamento]
        if not validas and coincidencias and log:
            otros = sorted({_depto_de_municipio(s["municipio"]) for s in coincidencias if s["municipio"]})
            log(f"    '{texto_buscar}': hay sede(s) con ese nombre pero en OTRO departamento "
                f"(código {', '.join(otros)}), no en el {departamento} que pediste -- se descartan.")
        return validas

    def elegir(coincidencias, motivo):
        limpias = [s for s in coincidencias if not s["codigo_sede"].startswith("+")]
        elegida = limpias[0] if limpias else coincidencias[0]
        if log:
            extra = (f" [de {len(coincidencias)} coincidencias, se evitaron las "
                      f"que empiezan con '+']") if len(coincidencias) > 1 else ""
            log(f"    '{texto_buscar}' -> sede {elegida['codigo_sede']} "
                f"({elegida['nombre']}, municipio {elegida['municipio']}) [{motivo}]{extra}")
        # 'candidatas': todas las coincidencias, la elegida primero. Si el RNDC
        # rechaza la elegida (REM180), crear_remesa_api prueba las demás.
        candidatas = [elegida] + [c for c in limpias if c is not elegida] + \
                     [c for c in coincidencias if c not in limpias and c is not elegida]
        return {"codigo_sede": elegida["codigo_sede"], "municipio": elegida["municipio"],
                "nombre": elegida["nombre"], "tipo_id": elegida.get("tipo_id"),
                "candidatas": candidatas}

    palabras = texto_buscar.strip().split()
    # Primero se prueba con el texto completo, luego quitando palabras del
    # final de a una (el departamento casi siempre es la última palabra).
    frases_a_probar = [" ".join(palabras[:n]) for n in range(len(palabras), 0, -1)]

    for i_frase, frase in enumerate(frases_a_probar):
        frase_norm = normalizar(frase)
        exactas = del_departamento([s for s in sedes if normalizar(s["nombre"]) == frase_norm],
                                    es_nombre_completo=(i_frase == 0))
        if exactas:
            return elegir(exactas, "coincidencia exacta")

    # Ninguna coincidencia exacta -- se cae a una coincidencia parcial con
    # el texto completo original, en cualquiera de los dos sentidos.
    texto_buscar_norm = normalizar(texto_buscar)
    parciales = del_departamento([
        s for s in sedes
        if texto_buscar_norm in normalizar(s["nombre"]) or normalizar(s["nombre"]) in texto_buscar_norm
    ])
    if parciales:
        return elegir(parciales, "coincidencia parcial")
    return None


def buscar_sede(usuario, password, nit_empresa, texto_buscar, log=None):
    """Busca la sede (ver _buscar_sede_en). Si no aparece en la lista
    guardada, vuelve a pedir la lista al RNDC UNA vez antes de rendirse: así
    una sede que se acaba de crear en la página del RNDC sí la toma la app."""
    clave = (usuario, nit_empresa)
    estaba_en_cache = clave in _cache_sedes
    sede = _buscar_sede_en(_obtener_sedes(usuario, password, nit_empresa, log=log), texto_buscar, log=log)
    if sede or not estaba_en_cache:
        return sede
    if log:
        log(f"    '{texto_buscar}' no está en la lista guardada; actualizando las sedes desde el RNDC...")
    return _buscar_sede_en(_obtener_sedes(usuario, password, nit_empresa, log=log, forzar=True),
                            texto_buscar, log=log)


def buscar_sede_con_respaldo(usuario, password, numid_principal, nit_empresa, texto_buscar, log=None):
    """Busca la sede primero en las del NIT/cédula principal (el
    remitente o destinatario real del viaje); si no tiene ninguna
    registrada -- lo normal cuando es una persona natural, ya que las
    sedes son de la empresa transportadora, no de cualquier persona --
    cae de vuelta a las sedes de la empresa para esa misma ciudad."""
    sede = buscar_sede(usuario, password, numid_principal, texto_buscar, log=log)
    if sede:
        return sede
    if numid_principal == nit_empresa:
        return None
    if log:
        log(f"    (el {numid_principal} no tiene sedes propias -- probando con las de la empresa)")
    return buscar_sede(usuario, password, nit_empresa, texto_buscar, log=log)


def obtener_lista_sedes_empresa_api(usuario, password, nit_empresa, log=None):
    """Trae la lista de sedes de la empresa por el Web Service (lo mismo
    que ya usa buscar_sede para todo lo demás), en vez de abrir un
    navegador a leer el desplegable de la página de Remesa -- más rápido,
    y no depende de tener Chrome/chromedriver listos en la máquina. La
    guarda en el mismo archivo que ya usaba la versión por navegador
    (ARCHIVO_SEDES), así que las sugerencias del formulario siguen
    funcionando igual. Devuelve la lista de nombres."""
    os.makedirs(os.path.dirname(ARCHIVO_SEDES) or ".", exist_ok=True)
    sedes = _obtener_sedes(usuario, password, nit_empresa, log=log, forzar=True)
    opciones = sorted(set(s["nombre"] for s in sedes if s["nombre"]))
    with open(ARCHIVO_SEDES, "w", encoding="utf-8") as f:
        json.dump(opciones, f, ensure_ascii=False, indent=2)
    if log:
        log(f"✅ Se guardaron {len(opciones)} sedes reales en {ARCHIVO_SEDES} (por el Web Service).")
    return opciones


def crear_tercero_via_navegador(usuario, password, tipo_id, numero_id,
                                 nombre, apellido1, apellido2, municipio_contiene,
                                 direccion=None, log=None):
    """Registra/"refresca" un Tercero abriendo un navegador liviano y
    usando la misma página que ya usa Selenium -- necesario porque el
    sitio dispara, al escribir la cédula, una consulta propia (contra el
    RUNT, probablemente) que trae los datos de la licencia de conducción
    y los deja fijos en el formulario; eso no se puede replicar por la
    API. El resto del viaje (remesa, manifiesto, sedes, FOPAT, flete,
    vehículos) sigue por el Web Service -- este es el único paso que
    todavía necesita el navegador. Devuelve True si quedó guardado."""
    from .terceros import crear_tercero as crear_tercero_selenium
    driver = None
    try:
        if log:
            log("    Abriendo el navegador solo para registrar/refrescar al Tercero...")
        chrome_options = crear_opciones_chrome()
        driver = crear_driver_con_limite(chrome_options, obtener_chromedriver_path())
        driver.set_page_load_timeout(25)
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.common.by import By
        import time as _time

        wait = WebDriverWait(driver, 20)
        driver.get(URL_LOGIN)
        _time.sleep(1)
        wait.until(lambda d: d.find_element(By.ID, "dnn_ctr390_FormLogIn_edUsername")).send_keys(usuario)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_edPassword").send_keys(password)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_btIngresar").click()
        _time.sleep(2)

        guardado = crear_tercero_selenium(
            driver, wait, tipo_id, numero_id, nombre, apellido1, apellido2,
            municipio_contiene, log or (lambda m: None), direccion=direccion,
        )
        return guardado
    except Exception as e:
        if log:
            log(f"    ⚠️  No se pudo registrar el Tercero por el navegador: {e}")
        return False
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def crear_tercero_api(usuario, password, nit_empresa, tipo_id, numero_id,
                       nombre, apellido1, apellido2, municipio_contiene, direccion=None, log=None):
    """Registra a una persona (conductor/titular) como Tercero en el
    RNDC -- lo mismo que hace Selenium cuando un conductor no está
    registrado todavía, o cuando su licencia salió vencida y hay que
    "refrescarlo". A diferencia del sitio web (que completa varios
    campos por su cuenta con solo la cédula, usando su propio
    JavaScript antes de mandar el formulario), la API sí necesita estos
    datos explícitos: nombre, primer apellido y municipio son
    obligatorios (segundo apellido es opcional).

    El municipio se manda como CÓDIGO en CODMUNICIPIORNDC (ej. 11001000,
    según el ejemplo de la guía oficial). Si esta persona YA existe como
    Tercero (el caso más común: se está "refrescando" para poner al día
    una licencia vencida), se consulta su registro actual primero y se
    reusan su código de municipio y su sede tal cual, para apuntar al
    mismo registro. Si de verdad es alguien nuevo, el código sale de la
    sede que coincida con municipio_contiene.

    Devuelve el radicado si funciona; lanza ErrorRNDC si no."""
    texto_municipio = None
    codigo_municipio = None
    codigo_sede = None
    nombre_sede = None
    variables_consulta = ("CODTIPOIDTERCERO,NUMIDTERCERO,MUNICIPIORNDC,CODMUNICIPIORNDC,"
                          "CODSEDETERCERO,NOMSEDETERCERO")
    documento_consulta = (
        f"<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>"
        f"<CODTIPOIDTERCERO>'{tipo_id}'</CODTIPOIDTERCERO>"
        f"<NUMIDTERCERO>{numero_id}</NUMIDTERCERO>"
    )
    respuesta_consulta = _llamar(usuario, password, tipo=3, procesoid=11,
                                  variables_xml=variables_consulta,
                                  documento_xml=documento_consulta,
                                  servidor="real_terceros")
    try:
        raiz_consulta = ET.fromstring(str(respuesta_consulta))
        doc_existente = raiz_consulta.find("documento")
        if doc_existente is not None:
            texto_municipio = doc_existente.findtext("municipiorndc", default="").strip() or None
            codigo_municipio = doc_existente.findtext("codmunicipiorndc", default="").strip() or None
            codigo_sede = doc_existente.findtext("codsedetercero", default="").strip() or None
            nombre_sede = doc_existente.findtext("nomsedetercero", default="").strip() or None
    except ET.ParseError:
        pass

    if texto_municipio and codigo_municipio:
        if log:
            log(f"    (ya existe como Tercero -- reusando su municipio, código y sede tal cual: "
                f"'{texto_municipio}' / {codigo_municipio})")
        nombre_sede = nombre_sede or municipio_contiene
    else:
        # Persona nueva (o la consulta no dio el código): se toma el
        # código del municipio de la sede que coincida con lo escrito.
        sede_municipio = buscar_sede(usuario, password, nit_empresa, municipio_contiene, log=log)
        if not sede_municipio:
            raise ErrorRNDC(f"No se encontró ninguna sede que contenga '{municipio_contiene}' "
                             f"para registrar el municipio del conductor.")
        texto_municipio = sede_municipio["nombre"]
        codigo_municipio = sede_municipio["municipio"]
        codigo_sede = None
        nombre_sede = municipio_contiene

    # Según la guía oficial y el propio sitio, el municipio va como CÓDIGO
    # en CODMUNICIPIORNDC (no como texto en MUNICIPIORNDC).
    variables = f"""
<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>
<CODTIPOIDTERCERO>{tipo_id}</CODTIPOIDTERCERO>
<NUMIDTERCERO>{numero_id}</NUMIDTERCERO>
<NOMIDTERCERO>{nombre}</NOMIDTERCERO>
<PRIMERAPELLIDOIDTERCERO>{apellido1}</PRIMERAPELLIDOIDTERCERO>
{f'<SEGUNDOAPELLIDOIDTERCERO>{apellido2}</SEGUNDOAPELLIDOIDTERCERO>' if apellido2 else ''}
<CODMUNICIPIORNDC>{codigo_municipio}</CODMUNICIPIORNDC>
{f'<CODSEDETERCERO>{codigo_sede}</CODSEDETERCERO>' if codigo_sede else ''}
<NOMSEDETERCERO>{nombre_sede}</NOMSEDETERCERO>
<NOMENCLATURADIRECCION>{direccion or texto_municipio}</NOMENCLATURADIRECCION>
""".strip()

    respuesta = _llamar(usuario, password, tipo=1, procesoid=11,
                         variables_xml=variables, servidor="real_terceros")
    radicado, error = _extraer_radicado_o_error(respuesta)
    if error:
        raise ErrorRNDC(error, respuesta)
    return radicado


# Peso vacío (kg) con el que se registra una placa nueva, según el tipo de
# vehículo -- son los mismos valores que se escriben a mano en la página de
# Vehículo.
PESO_VACIO_POR_TIPO = {
    "camioneta": "1500",
    "camion": "2000",        # camión rígido de 2 ejes
    "tractocamion": "5000",  # tractocamión de 3 ejes
}
PESO_VACIO_REMOLQUE = "5000"  # semirremolque de 3 ejes

# Código de configuración de unidad de carga -- confirmado consultando dos
# vehículos reales ya registrados de la flota (JVM353 y R82591). Sin este
# campo, el RNDC rechaza tractocamiones y remolques con "Error VEH040: La
# configuración del vehículo no corresponde a los códigos" (para camión y
# camioneta, hasta ahora, el registro sin este campo sí funcionó -- si
# alguno también lo llegara a pedir, avisar para agregar su código real).
CONFIGURACION_POR_TIPO = {
    "tractocamion": "54",  # tractocamión de 3 ejes
}
CONFIGURACION_REMOLQUE = "63"  # semirremolque de 3 ejes


def vehiculo_existe(usuario, password, nit_empresa, placa):
    """Consulta (solo lectura) si la placa ya está en el maestro de
    vehículos. Devuelve True, False, o None si no se pudo saber (respuesta
    rara) -- en ese caso no se debe registrar nada a ciegas."""
    documento = (
        f"<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>"
        f"<NUMPLACA>'{placa}'</NUMPLACA>"
    )
    respuesta = _llamar(usuario, password, tipo=3, procesoid=12,
                         variables_xml="NUMPLACA", documento_xml=documento,
                         servidor="real_terceros")
    try:
        raiz = ET.fromstring(str(respuesta))
    except ET.ParseError:
        return None
    if raiz.find("documento") is not None:
        return True
    error = raiz.find("ErrorMSG")
    texto_error = "".join(error.itertext()).upper() if error is not None else ""
    if "RNDC11" in texto_error or "NO ENCONTRADO" in texto_error:
        return False
    return None


def crear_vehiculo_api(usuario, password, nit_empresa, placa, peso_vacio, configuracion=None,
                        modelo=None, combustible=None, log=None):
    """Registra una placa nueva (vehículo o remolque) en el RNDC mandando
    la placa, el peso vacío, y opcionalmente el código de configuración
    de unidad de carga, el modelo (año, 4 dígitos) y el tipo de
    combustible -- ninguno de estos tres es obligatorio siempre (se ha
    visto que algunos vehículos los piden -- VEH040/VEH060/VEH200 -- y
    otros no, sin un patrón claro todavía por tipo de vehículo), así que
    se mandan solo si se dan. Devuelve el número de ingreso; lanza
    ErrorRNDC si el RNDC la rechaza."""
    variables = (
        f"<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>"
        f"<NUMPLACA>{placa}</NUMPLACA>"
        f"<PESOVEHICULOVACIO>{peso_vacio}</PESOVEHICULOVACIO>"
        f"{f'<CODCONFIGURACIONUNIDADCARGA>{configuracion}</CODCONFIGURACIONUNIDADCARGA>' if configuracion else ''}"
        f"{f'<ANOFABRICACIONVEHICULOCARGA>{modelo}</ANOFABRICACIONVEHICULOCARGA>' if modelo else ''}"
        f"{f'<CODTIPOCOMBUSTIBLE>{combustible}</CODTIPOCOMBUSTIBLE>' if combustible else ''}"
    )
    respuesta = _llamar(usuario, password, tipo=1, procesoid=12,
                         variables_xml=variables, servidor="real_terceros")
    radicado, error = _extraer_radicado_o_error(respuesta)
    if error:
        raise ErrorRNDC(error, respuesta)
    return radicado


def crear_remesa_api(usuario, password, nit_empresa, consecutivo_remesa,
                      origen, destino, producto_codigo, descripcion_producto,
                      peso_kg, sede_propietario_contiene,
                      tipoid_remitente, numid_remitente,
                      tipoid_destinatario, numid_destinatario,
                      fecha_cargue, fecha_descargue, log=None):
    """Crea una Remesa vía Web Service. tipoid/numid remitente y
    destinatario pueden ser un cliente distinto de la empresa (si el
    viaje es para otro cliente) -- la sede de cada uno se busca con su
    propio NIT/cédula, no siempre con el de la empresa.
    Devuelve el radicado (texto) si funciona; lanza ErrorRNDC si no."""

    sede_remitente = buscar_sede_con_respaldo(usuario, password, numid_remitente, nit_empresa, origen, log=log)
    if not sede_remitente:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga '{origen}' "
                         f"para el remitente {numid_remitente} (en el departamento indicado, si lo escribiste). "
                         f"Revisa que la ciudad y el departamento estén bien escritos; si es correcto, hay que crearla en el RNDC primero.")

    sede_destinatario = buscar_sede_con_respaldo(usuario, password, numid_destinatario, nit_empresa, destino, log=log)
    if not sede_destinatario:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga '{destino}' "
                         f"para el destinatario {numid_destinatario} (en el departamento indicado, si lo escribiste). "
                         f"Revisa que la ciudad y el departamento estén bien escritos; si es correcto, hay que crearla en el RNDC primero.")

    sede_propietario = buscar_sede(usuario, password, nit_empresa,
                                    sede_propietario_contiene, log=log)
    if not sede_propietario:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga "
                         f"'{sede_propietario_contiene}' (propietario) para el NIT {nit_empresa}.")

    if log:
        log(f"    Sede remitente ({origen}, {numid_remitente}): {sede_remitente['codigo_sede']} | "
            f"Sede destinatario ({destino}, {numid_destinatario}): {sede_destinatario['codigo_sede']} | "
            f"Sede propietario: {sede_propietario['codigo_sede']} | "
            f"Municipio destino de la remesa: {sede_destinatario['municipio']}")

    def _enviar(sede_remitente, sede_destinatario):
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
<CODTIPOIDREMITENTE>{tipoid_remitente}</CODTIPOIDREMITENTE>
<NUMIDREMITENTE>{numid_remitente}</NUMIDREMITENTE>
<CODSEDEREMITENTE>{sede_remitente['codigo_sede']}</CODSEDEREMITENTE>
<CODTIPOIDDESTINATARIO>{tipoid_destinatario}</CODTIPOIDDESTINATARIO>
<NUMIDDESTINATARIO>{numid_destinatario}</NUMIDDESTINATARIO>
<CODSEDEDESTINATARIO>{sede_destinatario['codigo_sede']}</CODSEDEDESTINATARIO>
<HORASPACTOCARGA>1</HORASPACTOCARGA>
<MINUTOSPACTOCARGA>0</MINUTOSPACTOCARGA>
<HORASPACTODESCARGUE>1</HORASPACTODESCARGUE>
<MINUTOSPACTODESCARGUE>0</MINUTOSPACTODESCARGUE>
<CODTIPOIDPROPIETARIO>N</CODTIPOIDPROPIETARIO>
<NUMIDPROPIETARIO>{nit_empresa}</NUMIDPROPIETARIO>
<CODSEDEPROPIETARIO>{sede_propietario['codigo_sede']}</CODSEDEPROPIETARIO>
<DUENOPOLIZA>N</DUENOPOLIZA>
<FECHACITAPACTADACARGUE>{fecha_cargue}</FECHACITAPACTADACARGUE>
<HORACITAPACTADACARGUE>08:00</HORACITAPACTADACARGUE>
<FECHACITAPACTADADESCARGUE>{fecha_descargue}</FECHACITAPACTADADESCARGUE>
<HORACITAPACTADADESCARGUEREMESA>10:00</HORACITAPACTADADESCARGUEREMESA>
""".strip()

        respuesta = _llamar(usuario, password, tipo=1, procesoid=3,
                             variables_xml=variables, servidor="real_remesas")
        radicado, error = _extraer_radicado_o_error(respuesta)
        if error:
            raise ErrorRNDC(error, respuesta)
        return radicado

    try:
        return _enviar(sede_remitente, sede_destinatario)
    except ErrorRNDC as primer_error:
        # REM180: "el tipo y/o identificación del remitente/destinatario no
        # coinciden con los reportados en la tabla de Terceros". Es un rechazo
        # de validación (no se crea nada), así que es seguro probar con las
        # OTRAS sedes que tienen el mismo nombre (puede haber varias, de
        # distinto tipo de tercero) antes de rendirse.
        if "REM180" not in str(primer_error):
            raise
        es_destinatario = "destinatario" in str(primer_error).lower()
        rol = "destinatario" if es_destinatario else "remitente"
        sede_mala = sede_destinatario if es_destinatario else sede_remitente
        alternativas = [c for c in (sede_mala.get("candidatas") or [])
                        if c["codigo_sede"] != sede_mala["codigo_sede"]]
        if log:
            todas = ", ".join(f"{c['codigo_sede']} (tipo {c.get('tipo_id')}, municipio {c['municipio']})"
                              for c in (sede_mala.get("candidatas") or [sede_mala]))
            log(f"    El RNDC rechazó la sede {sede_mala['codigo_sede']} del {rol} (REM180). "
                f"Sedes con ese nombre: {todas}.")
        for alt in alternativas:
            if log:
                log(f"    Probando con la otra sede {alt['codigo_sede']} (tipo {alt.get('tipo_id')}) del {rol}...")
            try:
                if es_destinatario:
                    return _enviar(sede_remitente, alt)
                return _enviar(alt, sede_destinatario)
            except ErrorRNDC as e:
                if "REM180" not in str(e):
                    raise
        raise primer_error


def crear_manifiesto_api(usuario, password, nit_empresa, consecutivo_manifiesto,
                          consecutivos_remesa, origen, destino,
                          numid_remitente, numid_destinatario,
                          cedula_titular, placa, placa_remolque,
                          cedula_conductor, cedula_conductor2,
                          flete, retencion_ica, retencion_fuente,
                          fopat_aplica, fecha_expedicion, fecha_pago,
                          observaciones, anticipo=None, log=None,
                          tipo_operacion="G", municipio_intermedio_contiene=None,
                          tipo_id_conductor="C", tipo_id_conductor2="C"):
    """Crea un Manifiesto vía Web Service, uniéndolo a una o varias
    remesas ya creadas (consecutivos_remesa puede ser un texto -- un solo
    consecutivo -- o una lista, para Multiparada/Ida y Regreso). Calcula
    el FOPAT automáticamente (0.1% del flete) si fopat_aplica es True.
    'tipo_operacion' es el código que espera el RNDC ("G" normal, "I"
    Ida y Regreso, "M" Multiparada, "U" Viaje Municipal o Urbano). El municipio de origen/destino se
    busca en las sedes del remitente y del destinatario respectivamente
    (los mismos que se usaron para la Remesa) -- no siempre en las de la
    empresa, porque el viaje puede ser para un cliente distinto.
    'municipio_intermedio_contiene' es el texto de ciudad del punto de
    retorno; el RNDC lo exige (Error MAN094) cuando tipo_operacion es
    "I" (Ida y Regreso) -- el campo real se llama CODMUNICIPIOINTERMEDIO
    (confirmado inspeccionando el formulario del sitio; un intento
    anterior con "CODMUNICIPIOINTERMEDIOMANIFIESTO" no existía).
    Devuelve el radicado; lanza ErrorRNDC si no."""
    if isinstance(consecutivos_remesa, str):
        consecutivos_remesa = [consecutivos_remesa]

    sede_origen = buscar_sede_con_respaldo(usuario, password, numid_remitente, nit_empresa, origen, log=log)
    sede_destino = buscar_sede_con_respaldo(usuario, password, numid_destinatario, nit_empresa, destino, log=log)
    if not sede_origen:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga '{origen}' "
                         f"para el remitente {numid_remitente}, no se puede saber el municipio de origen.")
    if not sede_destino:
        raise ErrorRNDC(f"No se encontró ninguna sede que contenga '{destino}' "
                         f"para el destinatario {numid_destinatario}, no se puede saber el municipio de destino.")
    municipio_origen = sede_origen["municipio"]
    municipio_destino = sede_destino["municipio"]
    if log:
        log(f"    Municipio del manifiesto: origen {municipio_origen} | destino {municipio_destino}")

    municipio_intermedio = None
    if municipio_intermedio_contiene:
        sede_intermedia = buscar_sede_con_respaldo(usuario, password, numid_remitente, nit_empresa,
                                                     municipio_intermedio_contiene, log=log)
        if not sede_intermedia:
            raise ErrorRNDC(f"No se encontró ninguna sede que contenga "
                             f"'{municipio_intermedio_contiene}' para el punto intermedio (Ida y Regreso).")
        municipio_intermedio = sede_intermedia["municipio"]

    valor_fopat = round(float(flete) * 0.001) if fopat_aplica else None
    remesas_xml = "".join(
        f"<REMESA><CONSECUTIVOREMESA>{c}</CONSECUTIVOREMESA></REMESA>"
        for c in consecutivos_remesa
    )

    variables = f"""
<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>
<NUMMANIFIESTOCARGA>{consecutivo_manifiesto}</NUMMANIFIESTOCARGA>
<CODOPERACIONTRANSPORTE>{tipo_operacion}</CODOPERACIONTRANSPORTE>
<FECHAEXPEDICIONMANIFIESTO>{fecha_expedicion}</FECHAEXPEDICIONMANIFIESTO>
<CODMUNICIPIOORIGENMANIFIESTO>{municipio_origen}</CODMUNICIPIOORIGENMANIFIESTO>
<CODMUNICIPIODESTINOMANIFIESTO>{municipio_destino}</CODMUNICIPIODESTINOMANIFIESTO>
{f'<CODMUNICIPIOINTERMEDIO>{municipio_intermedio}</CODMUNICIPIOINTERMEDIO>' if municipio_intermedio else ''}
<CODIDTITULARMANIFIESTO>C</CODIDTITULARMANIFIESTO>
<NUMIDTITULARMANIFIESTO>{cedula_titular}</NUMIDTITULARMANIFIESTO>
<NUMPLACA>{placa}</NUMPLACA>
{f'<NUMPLACAREMOLQUE>{placa_remolque}</NUMPLACAREMOLQUE>' if placa_remolque else ''}
<CODIDCONDUCTOR>{tipo_id_conductor}</CODIDCONDUCTOR>
<NUMIDCONDUCTOR>{cedula_conductor}</NUMIDCONDUCTOR>
{f'<CODIDCONDUCTOR2>{tipo_id_conductor2}</CODIDCONDUCTOR2><NUMIDCONDUCTOR2>{cedula_conductor2}</NUMIDCONDUCTOR2>' if cedula_conductor2 else ''}
<VALORFLETEPACTADOVIAJE>{flete}</VALORFLETEPACTADOVIAJE>
<RETENCIONFUENTEMANIFIESTO>{retencion_fuente}</RETENCIONFUENTEMANIFIESTO>
<RETENCIONICAMANIFIESTOCARGA>{retencion_ica}</RETENCIONICAMANIFIESTOCARGA>
{f'<RETENCIONFOPAT>{valor_fopat}</RETENCIONFOPAT>' if fopat_aplica else ''}
<CODMUNICIPIOPAGOSALDO>{municipio_destino}</CODMUNICIPIOPAGOSALDO>
<FECHAPAGOSALDOMANIFIESTO>{fecha_pago}</FECHAPAGOSALDOMANIFIESTO>
<CODRESPONSABLEPAGOCARGUE>R</CODRESPONSABLEPAGOCARGUE>
<CODRESPONSABLEPAGODESCARGUE>D</CODRESPONSABLEPAGODESCARGUE>
{f'<VALORANTICIPOMANIFIESTO>{anticipo}</VALORANTICIPOMANIFIESTO>' if anticipo else ''}
<OBSERVACIONES>{observaciones}</OBSERVACIONES>
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


def _limpiar_nit(texto):
    """Limpia un NIT/cédula tal como la gente lo suele escribir (con
    puntos de miles, o con el dígito de verificación después de un
    guion, ej: '900.536.415-9'). El RNDC espera solo el número base, sin
    el DV -- si se manda con el DV pegado, no coincide con el Tercero
    real y sale un error de que "no existe" aunque sí esté registrado."""
    if not texto:
        return texto
    base = str(texto).split("-")[0]
    return "".join(c for c in base if c.isdigit())


def tercero_existe(usuario, password, nit_empresa, tipo_id, numero_id):
    """Consulta (solo lectura) si una cédula/NIT ya está registrada como
    Tercero -- sin importar si tiene sedes o no, solo si existe. Devuelve
    True, False, o None si no se pudo saber (para no registrar nada a
    ciegas en ese caso)."""
    documento = (
        f"<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>"
        f"<CODTIPOIDTERCERO>'{tipo_id}'</CODTIPOIDTERCERO>"
        f"<NUMIDTERCERO>{numero_id}</NUMIDTERCERO>"
    )
    respuesta = _llamar(usuario, password, tipo=3, procesoid=11,
                         variables_xml="NUMIDTERCERO", documento_xml=documento,
                         servidor="real_terceros")
    try:
        raiz = ET.fromstring(str(respuesta))
    except ET.ParseError:
        return None
    if raiz.find("documento") is not None:
        return True
    error = raiz.find("ErrorMSG")
    texto_error = "".join(error.itertext()).upper() if error is not None else ""
    if "RNDC11" in texto_error or "NO ENCONTRADO" in texto_error:
        return False
    return None


def consultar_nombre_tercero_api(usuario, password, nit_empresa, tipo_id, numero_id, log=None):
    """Consulta (solo lectura) el nombre completo de un Tercero ya
    registrado, para dejarlo en el resumen/en Sheets (columna "Nombre
    Conductor"/"Nombre Titular") -- el equivalente, por el Web Service,
    de lo que Selenium ya hacía leyendo la página. Devuelve el nombre
    completo (nombre + apellidos) o None si no se encontró o no se pudo
    leer."""
    documento = (
        f"<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>"
        f"<CODTIPOIDTERCERO>'{tipo_id}'</CODTIPOIDTERCERO>"
        f"<NUMIDTERCERO>{numero_id}</NUMIDTERCERO>"
    )
    try:
        respuesta = _llamar(usuario, password, tipo=3, procesoid=11,
                             variables_xml="NOMIDTERCERO,PRIMERAPELLIDOIDTERCERO,SEGUNDOAPELLIDOIDTERCERO",
                             documento_xml=documento, servidor="real_terceros")
        raiz = ET.fromstring(str(respuesta))
        doc = raiz.find("documento")
        if doc is None:
            return None
        partes = [
            doc.findtext("nomidtercero", default="").strip(),
            doc.findtext("primerapellidoidtercero", default="").strip(),
            doc.findtext("segundoapellidoidtercero", default="").strip(),
        ]
        nombre = " ".join(p for p in partes if p)
        return nombre or None
    except Exception as e:
        if log:
            log(f"    (no se pudo consultar el nombre de {numero_id}: {e})")
        return None


def verificar_antes_de_crear(v, tramos, usuario, password, nit_empresa, log):
    """Consulta (solo lectura, no crea nada) si el remitente, destinatario,
    conductor, titular y placa(s) del viaje ya existen -- para avisar de
    una vez, ANTES de crear la Remesa, en vez de descubrirlo a medio
    camino y dejar una Remesa creada sin Manifiesto. Ojo: esto NO puede
    confirmar si la licencia del conductor está vigente (el RNDC no
    tiene una consulta directa para eso) -- solo si existe como Tercero;
    lo de la licencia se sigue descubriendo, como hasta ahora, al crear
    el Manifiesto. Devuelve una lista de avisos (vacía si todo está
    listo o se puede arreglar solo con los datos que ya trae el
    formulario)."""
    avisos = []
    # Datos numéricos obligatorios: se revisan PRIMERO y sin llamar al RNDC.
    # Antes, un Flete vacío solo reventaba al crear el Manifiesto
    # ("could not convert string to float: ''"), cuando las Remesas ya
    # estaban creadas en el RNDC y quedaban huérfanas.
    def _vacio_o_cero(texto):
        digitos = _limpiar_numero(texto or "")
        return not digitos or int(digitos) == 0
    if _vacio_o_cero(v.get("Flete")):
        avisos.append("Falta el valor del flete.")
    for tramo in tramos:
        if _vacio_o_cero(tramo.get("peso")):
            avisos.append(f"Falta el peso de la remesa {tramo['consecutivo']}.")
        if not str(tramo.get("producto") or "").strip():
            avisos.append(f"Falta el producto de la remesa {tramo['consecutivo']}.")
    if avisos:
        return avisos
    log("    Verificando remitente, destinatario, conductor, titular y placa(s) antes de crear...")

    vistos = set()
    for tramo in tramos:
        for rol, tipoid, numid in (
            ("remitente", tramo["tipoid_remitente"], tramo["numid_remitente"]),
            ("destinatario", tramo["tipoid_destinatario"], tramo["numid_destinatario"]),
        ):
            clave = (tipoid, numid)
            if numid == nit_empresa or clave in vistos:
                continue  # la empresa misma siempre existe; no repetir la misma persona dos veces
            vistos.add(clave)
            existe = tercero_existe(usuario, password, nit_empresa, tipoid, numid)
            if existe is False:
                avisos.append(f"El {rol} {numid} no está registrado como Tercero en el RNDC.")

    cedula_conductor = _limpiar_numero(v["Cedula_Conductor"])
    tipo_id_conductor = (v.get("Tipo_Documento_Conductor") or "C").strip()
    if tercero_existe(usuario, password, nit_empresa, tipo_id_conductor, cedula_conductor) is False:
        if v.get("Nombre_Conductor") and v.get("Apellido1_Conductor") and v.get("Municipio_Conductor"):
            log(f"    (el conductor {cedula_conductor} no existe todavía, pero el formulario trae "
                f"sus datos -- se registrará solo cuando haga falta)")
        else:
            avisos.append(f"El conductor {cedula_conductor} no está registrado como Tercero en el RNDC. "
                           f"Créalo primero en Herramientas → Crear Tercero.")

    cedula_titular = _limpiar_numero(v["Cedula_Titular"])
    if tercero_existe(usuario, password, nit_empresa, "C", cedula_titular) is False:
        if v.get("Nombre_Titular") and v.get("Apellido1_Titular") and v.get("Municipio_Titular"):
            log(f"    (el titular {cedula_titular} no existe todavía, pero el formulario trae "
                f"sus datos -- se registrará solo cuando haga falta)")
        else:
            avisos.append(f"El titular {cedula_titular} no está registrado como Tercero en el RNDC. "
                           f"Créalo primero en Herramientas → Crear Tercero.")

    placas = [(v["Placa"], "principal")]
    if v.get("Placa_Remolque"):
        placas.append((v["Placa_Remolque"], "remolque"))
    for placa, rol in placas:
        if vehiculo_existe(usuario, password, nit_empresa, placa) is False:
            if rol == "principal" and not (v.get("Tipo_Vehiculo_Nuevo") or "").strip():
                avisos.append(f"La placa {placa} ({rol}) no está registrada, y falta elegir su "
                               f"\"Tipo de vehículo\" en el formulario para registrarla sola.")
            else:
                log(f"    (la placa {placa} [{rol}] no existe todavía, pero se puede registrar "
                    f"sola cuando haga falta)")

    return avisos


def ejecutar_viaje_api(v, usuario, password, log):
    """Crea la Remesa (o remesas, para Ida y Regreso/Multiparada) y el
    Manifiesto de un viaje usando el Web Service, en vez de manejar un
    navegador. Recibe 'v' con la misma forma que ya usa
    ejecutar_automatizacion, y devuelve un diccionario compatible con lo
    que ese devuelve, para que el resto de la app no tenga que cambiar.

    NOTA: por ahora cubre "Normal", "Ida y Regreso" y "Multiparada" --
    Récord (cola) y segundo conductor todavía no están conectados aquí;
    para esos, seguir usando ejecutar_automatizacion (Selenium) mientras
    se completa esta parte.
    """
    nit_empresa = FIJOS_REMESA["NUMIDPROPIETARIO"]
    resumen = []
    ajustes = []  # cosas que la app hizo sola por detrás, para el resumen final
    flete = _limpiar_numero(v["Flete"])

    # El remitente/destinatario pueden ser un cliente distinto de la
    # empresa (si el viaje es para otro cliente) -- si el formulario no
    # trae esos datos, se usa la empresa misma como respaldo, igual que
    # hace Selenium. Estos son los valores por DEFECTO para todo el
    # viaje; cada parada de Ida y Regreso puede pisarlos con los suyos
    # propios más abajo.
    # El tipo y el número van SIEMPRE juntos del mismo origen -- los dos
    # del cliente, o los dos de la empresa -- nunca mezclados. Antes cada
    # uno caía a su valor por defecto por separado: si el NIT del cliente
    # quedaba vacío pero el Tipo tenía un valor viejo (de un intento
    # anterior guardado en el formulario), se terminaba mandando el tipo
    # del cliente con el número de la empresa -- una combinación que el
    # RNDC rechaza (REM150/REM180: "tipo y/o identificación no coinciden").
    if v.get("NIT_Remitente_Cliente"):
        tipoid_remitente_defecto = normalizar_tipo_id(v.get("TipoID_Remitente_Cliente") or FIJOS_REMESA["TIPOIDREMITENTE"])
        numid_remitente_defecto = _limpiar_nit(v["NIT_Remitente_Cliente"])
    else:
        tipoid_remitente_defecto = normalizar_tipo_id(FIJOS_REMESA["TIPOIDREMITENTE"])
        numid_remitente_defecto = _limpiar_nit(FIJOS_REMESA["NUMIDREMITENTE"])
    if v.get("NIT_Destinatario_Cliente"):
        tipoid_destinatario_defecto = normalizar_tipo_id(v.get("TipoID_Destinatario_Cliente") or FIJOS_REMESA["TIPOIDDESTINATARIO"])
        numid_destinatario_defecto = _limpiar_nit(v["NIT_Destinatario_Cliente"])
    else:
        tipoid_destinatario_defecto = normalizar_tipo_id(FIJOS_REMESA["TIPOIDDESTINATARIO"])
        numid_destinatario_defecto = _limpiar_nit(FIJOS_REMESA["NUMIDDESTINATARIO"])

    # Las fechas de cargue/descargue de la Remesa siempre son de hoy (así
    # lo hace también Selenium); la Fecha de Expedición y la Fecha de
    # Pago del Manifiesto sí se pueden escribir distintas a mano en el
    # formulario -- si no se escriben, caen en los mismos valores por
    # defecto que ya usa Selenium.
    fecha_cargue = time.strftime("%d/%m/%Y")
    fecha_descargue = (datetime.now() + timedelta(days=5)).strftime("%d/%m/%Y")
    fecha_expedicion = v.get("FechaExpedicion") or fecha_cargue
    fecha_pago = v.get("FechaPago") or calcular_fecha_pago_por_defecto(fecha_expedicion, fecha_descargue)
    observaciones = v.get("Observaciones") or FIJOS_MANIFIESTO["RECOMENDACIONES"]
    anticipo = _limpiar_numero(v["Anticipo"]) if v.get("Anticipo") else None

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
        if v.get("IdaYRegreso") or v.get("Multiparada"):
            tramos = []
            for i, parada in enumerate(v["Paradas"]):
                # Igual que arriba: tipo y número van siempre juntos del
                # mismo origen -- los de la parada, o si la parada no
                # trae NIT propio, los defecto ya calculados (que a su
                # vez ya son atómicos entre el cliente y la empresa).
                if parada.get("NIT_Remitente"):
                    tipoid_remitente = normalizar_tipo_id(parada.get("TipoID_Remitente") or FIJOS_REMESA["TIPOIDREMITENTE"])
                    numid_remitente = _limpiar_nit(parada["NIT_Remitente"])
                else:
                    tipoid_remitente = tipoid_remitente_defecto
                    numid_remitente = numid_remitente_defecto
                if parada.get("NIT_Destinatario"):
                    tipoid_destinatario = normalizar_tipo_id(parada.get("TipoID_Destinatario") or FIJOS_REMESA["TIPOIDDESTINATARIO"])
                    numid_destinatario = _limpiar_nit(parada["NIT_Destinatario"])
                else:
                    tipoid_destinatario = tipoid_destinatario_defecto
                    numid_destinatario = numid_destinatario_defecto
                tramos.append({
                    "consecutivo": f"{v['Consecutivo']}{letras[i]}",
                    "origen": parada["Origen"], "destino": parada["Destino"],
                    "producto": parada["Producto"], "peso": _limpiar_numero(parada["Peso"]),
                    "tipoid_remitente": tipoid_remitente, "numid_remitente": numid_remitente,
                    "tipoid_destinatario": tipoid_destinatario, "numid_destinatario": numid_destinatario,
                })
        else:
            tramos = [{
                "consecutivo": v["Consecutivo"],
                "origen": v["Origen"], "destino": v["Destino"],
                "producto": v["Producto"], "peso": _limpiar_numero(v["Peso"]),
                "tipoid_remitente": tipoid_remitente_defecto, "numid_remitente": numid_remitente_defecto,
                "tipoid_destinatario": tipoid_destinatario_defecto, "numid_destinatario": numid_destinatario_defecto,
            }]

        avisos_previos = verificar_antes_de_crear(v, tramos, usuario, password, nit_empresa, log)
        if avisos_previos:
            for aviso in avisos_previos:
                log(f"❌ {aviso}")
            log("❌ No se creó nada -- corrige lo anterior en el formulario y vuelve a intentar. "
                "(No se puede confirmar de antemano si la licencia del conductor está vigente; "
                "eso se sigue viendo solo al crear el Manifiesto.)")
            return {"ok": False, "error": " / ".join(avisos_previos), "resumen": [], "archivos": []}

        for tramo in tramos:
            log(f"Creando remesa {tramo['consecutivo']} vía Web Service...")
            radicado_tramo = crear_remesa_api(
                usuario, password, nit_empresa, tramo["consecutivo"],
                origen=tramo["origen"], destino=tramo["destino"],
                producto_codigo=FIJOS_REMESA["CODIGOPRODUCTO"],
                descripcion_producto=tramo["producto"],
                peso_kg=tramo["peso"],
                sede_propietario_contiene=FIJOS_REMESA["SEDE_PROPIETARIO_CONTIENE"],
                tipoid_remitente=tramo["tipoid_remitente"], numid_remitente=tramo["numid_remitente"],
                tipoid_destinatario=tramo["tipoid_destinatario"], numid_destinatario=tramo["numid_destinatario"],
                fecha_cargue=fecha_cargue, fecha_descargue=fecha_descargue,
                log=log,
            )
            log(f"✅ Radicado de la remesa {tramo['consecutivo']}: {radicado_tramo}")
            resumen.append(f"Remesa {tramo['consecutivo']} -> radicado: {radicado_tramo}")
            tramo["radicado"] = radicado_tramo

        fopat_aplica = bool(v.get("Placa_Remolque"))
        origen_manifiesto = tramos[0]["origen"]
        destino_manifiesto = tramos[-1]["destino"]
        tipo_operacion = (
            "M" if v.get("Multiparada") else ("I" if v.get("IdaYRegreso") else ("U" if v.get("Municipal") else "G"))
        )
        # En Ida y Regreso, el punto intermedio (de retorno) es el destino
        # del primer tramo -- igual que hace Selenium.
        municipio_intermedio_contiene = (
            tramos[0]["destino"] if (v.get("IdaYRegreso") and len(tramos) >= 2) else None
        )
        consecutivos_remesa = [t["consecutivo"] for t in tramos]

        retencion_ica = calcular_retencion_ica(
            origen_manifiesto, destino_manifiesto, FIJOS_MANIFIESTO["RETENCIONICA"]
        )

        log(f"Creando manifiesto {v['Consecutivo']} vía Web Service...")
        intentos_fopat = 0
        intentos_flete = 0
        intentos_conductor = 0
        intentos_titular = 0
        intentos_conductor2 = 0
        intentos_vehiculo = 0
        esperas_vehiculo = 0
        cedula_conductor2_actual = _limpiar_numero(v["Cedula_Conductor2"]) if v.get("Cedula_Conductor2") else None
        while True:
            try:
                radicado_manifiesto = crear_manifiesto_api(
                    usuario, password, nit_empresa, v["Consecutivo"], consecutivos_remesa,
                    origen=origen_manifiesto, destino=destino_manifiesto,
                    numid_remitente=tramos[0]["numid_remitente"],
                    numid_destinatario=tramos[-1]["numid_destinatario"],
                    cedula_titular=_limpiar_numero(v["Cedula_Titular"]), placa=v["Placa"],
                    placa_remolque=v.get("Placa_Remolque"),
                    cedula_conductor=_limpiar_numero(v["Cedula_Conductor"]),
                    cedula_conductor2=cedula_conductor2_actual,
                    flete=flete, retencion_ica=retencion_ica,
                    retencion_fuente="1",
                    fopat_aplica=fopat_aplica,
                    fecha_expedicion=fecha_expedicion, fecha_pago=fecha_pago,
                    observaciones=observaciones, anticipo=anticipo,
                    log=log,
                    tipo_operacion=tipo_operacion,
                    municipio_intermedio_contiene=municipio_intermedio_contiene,
                    tipo_id_conductor=(v.get("Tipo_Documento_Conductor") or "C").strip(),
                    tipo_id_conductor2=(v.get("Tipo_Documento_Conductor2") or "C").strip(),
                )
                break
            except ErrorRNDC as e:
                mensaje_mayus = e.mensaje.upper()
                es_por_titular = "TITULAR" in mensaje_mayus and "NO EXISTE" in mensaje_mayus
                es_por_vehiculo = "MAN140" in mensaje_mayus or "MAN041" in mensaje_mayus or (
                    ("PLACA" in mensaje_mayus or "VEH" in mensaje_mayus or "REMOLQUE" in mensaje_mayus)
                    and ("NO EXISTE" in mensaje_mayus or "NO EST" in mensaje_mayus or "NO CREADA" in mensaje_mayus)
                )
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
                    ajustes.append(f"FOPAT {'agregado' if fopat_aplica else 'quitado'}")
                    log(f"    El RNDC rechazó por el FOPAT ({e.mensaje}) -- "
                        f"{'agregándolo' if fopat_aplica else 'quitándolo'} y reintentando...")
                elif es_por_flete_bajo and intentos_flete < 10:
                    # El flete no alcanza el mínimo (SiceTac u otra validación
                    # parecida) -- se sube de $300.000 en $300.000 y se
                    # reintenta, hasta 10 veces (hasta $3.000.000 de más).
                    intentos_flete += 1
                    flete_anterior = flete
                    flete = str(int(flete) + 300000)
                    ajustes.append(f"Flete subido de {int(flete_anterior):,} a {int(flete):,}")
                    log(f"    El RNDC rechazó por el valor del flete ({e.mensaje}) -- "
                        f"subiéndolo a {int(flete):,} y reintentando "
                        f"(intento {intentos_flete} de 10)...")
                elif "EXPIDIENDO" in mensaje_mayus or "PROCESANDO" in mensaje_mayus:
                    # Mensaje nunca antes visto, con pinta de que el RNDC
                    # se quedó a medio procesar la solicitud (no es un
                    # rechazo de negocio normal, como el FOPAT o el flete).
                    # No se sabe con certeza si el manifiesto quedó creado
                    # o no -- reintentar a ciegas podría crear uno
                    # duplicado, así que mejor detenerse aquí y avisar
                    # para que se revise a mano antes de hacer cualquier
                    # otra cosa con este viaje.
                    log(f"🛑 El RNDC devolvió una respuesta rara, que no se ve como un "
                        f"rechazo normal: \"{e.mensaje}\" -- puede que el manifiesto SÍ haya "
                        f"quedado creado. NO se va a reintentar, para no arriesgarse a un "
                        f"duplicado. Entra al RNDC y busca este manifiesto a mano (Consultas) "
                        f"para confirmarlo. Consecutivo: {v['Consecutivo']}.")
                    raise
                elif es_por_vehiculo and intentos_vehiculo < 1:
                    # La placa (del vehículo o del remolque) no está en el
                    # maestro de vehículos. El mensaje no dice cuál, así que
                    # se consulta cada una; solo se registran las que de
                    # verdad no existen (nunca a ciegas). Se registran con el
                    # peso vacío según el tipo de vehículo, y el RNDC
                    # completa el resto por su cuenta a partir de la placa.
                    intentos_vehiculo += 1
                    placas = [(v["Placa"], "principal")]
                    if v.get("Placa_Remolque"):
                        placas.append((v["Placa_Remolque"], "remolque"))
                    faltan = [
                        (p, rol) for p, rol in placas
                        if vehiculo_existe(usuario, password, nit_empresa, p) is False
                    ]
                    if not faltan:
                        log(f"❌ El RNDC dice que una placa no existe ({e.mensaje}), pero no pude "
                            f"confirmar cuál falta consultándolas. Revísalas a mano en el RNDC.")
                        raise
                    for placa_nueva, rol in faltan:
                        modelo = None
                        combustible = None
                        if rol == "principal":
                            tipo_veh = (v.get("Tipo_Vehiculo_Nuevo") or "").strip()
                            peso_vacio = PESO_VACIO_POR_TIPO.get(tipo_veh)
                            if not peso_vacio:
                                log(f"❌ La placa {placa_nueva} no está registrada en el RNDC. Para "
                                    f"registrarla automáticamente elige el \"Tipo de vehículo\" en el "
                                    f"bloque \"¿La placa es nueva en el RNDC?\" del formulario (define "
                                    f"el peso vacío).")
                                raise
                            configuracion = CONFIGURACION_POR_TIPO.get(tipo_veh)
                            modelo = (v.get("Modelo_Vehiculo_Nuevo") or "").strip() or None
                            combustible = (v.get("Combustible_Vehiculo_Nuevo") or "").strip() or None
                        else:
                            peso_vacio = PESO_VACIO_REMOLQUE
                            configuracion = CONFIGURACION_REMOLQUE
                        log(f"    La placa {placa_nueva} ({rol}) no está registrada -- "
                            f"registrándola con peso vacío {peso_vacio} kg"
                            f"{f' y configuración {configuracion}' if configuracion else ''}"
                            f"{f', modelo {modelo}' if modelo else ''}"
                            f"{f', combustible {combustible}' if combustible else ''}"
                            f" y reintentando...")
                        crear_vehiculo_api(usuario, password, nit_empresa, placa_nueva, peso_vacio,
                                            configuracion=configuracion, modelo=modelo,
                                            combustible=combustible, log=log)
                        log(f"    ✅ Placa {placa_nueva} registrada.")
                        ajustes.append(f"Placa {placa_nueva} ({rol}) registrada en el RNDC")
                    time.sleep(3)
                elif es_por_vehiculo and esperas_vehiculo < 3:
                    # Ya se registró la placa pero el RNDC todavía no la
                    # refleja (a veces tarda unos segundos en propagarse) --
                    # se espera un poco y se reintenta, sin volver a crearla.
                    esperas_vehiculo += 1
                    log(f"    El RNDC aún no refleja la placa recién registrada -- esperando "
                        f"unos segundos (intento {esperas_vehiculo} de 3)...")
                    time.sleep(8)
                elif es_por_titular and intentos_titular < 1:
                    # El titular no está registrado como Tercero. A
                    # diferencia del conductor, el campo del titular en el
                    # Manifiesto no pide ningún dato de licencia -- solo
                    # nombre, dirección y ciudad -- así que esto sí se puede
                    # registrar por la API, sin necesitar el navegador.
                    intentos_titular += 1
                    if not v.get("Nombre_Titular") or not v.get("Apellido1_Titular") or not v.get("Municipio_Titular"):
                        log(f"❌ El titular no está registrado en el RNDC ({e.mensaje}). Para "
                            f"registrarlo automáticamente hacen falta su Nombre, Apellido y "
                            f"Municipio en el formulario.")
                        raise
                    log(f"    El titular no está registrado ({e.mensaje}) -- "
                        f"registrándolo como Tercero y reintentando...")
                    crear_tercero_api(
                        usuario, password, nit_empresa, "C",
                        _limpiar_numero(v["Cedula_Titular"]),
                        v["Nombre_Titular"], v["Apellido1_Titular"],
                        v.get("Apellido2_Titular"), v["Municipio_Titular"],
                        direccion=v.get("Direccion_Titular"), log=log,
                    )
                    log(f"    ✅ Titular {v['Cedula_Titular']} registrado.")
                    ajustes.append(f"Titular {v['Cedula_Titular']} registrado como Tercero")
                elif (
                    cedula_conductor2_actual
                    and ("CONDUCTOR2" in mensaje_mayus.replace(" ", "") or "SEGUNDOCONDUCTOR" in mensaje_mayus.replace(" ", ""))
                    and intentos_conductor2 < 1
                ):
                    # El segundo conductor no existe como Tercero -- a
                    # diferencia del principal, Selenium tampoco lo
                    # registra automáticamente en este caso (no hay campos
                    # de nombre/apellido/municipio para un segundo
                    # conductor en el formulario); simplemente se sigue
                    # sin él y se avisa, en vez de bloquear todo el viaje.
                    intentos_conductor2 += 1
                    log(f"⚠️  El segundo conductor no está registrado en el RNDC ({e.mensaje}) -- "
                        f"el manifiesto sigue sin él, regístralo primero si de verdad hace falta "
                        f"en este viaje.")
                    cedula_conductor2_actual = None
                    ajustes.append("Segundo conductor quitado del manifiesto (no estaba registrado)")
                elif (
                    "CONDUCTOR" in mensaje_mayus
                    and ("NO EXISTE" in mensaje_mayus or "LICENCIA" in mensaje_mayus or "VENCID" in mensaje_mayus)
                    and intentos_conductor < 1
                ):
                    # El conductor no está registrado como Tercero, o su
                    # licencia salió vencida (aunque el conductor sí haya
                    # renovado de verdad -- el RNDC no se entera solo, hay
                    # que volver a registrarlo para que jale el dato
                    # actualizado). Esto se hace SIEMPRE por el navegador,
                    # no por la API: el sitio dispara, al escribir la
                    # cédula, una consulta propia (al RUNT, probablemente)
                    # que trae la licencia de conducción vigente y la deja
                    # fija en el formulario -- eso no se puede replicar por
                    # la API, y sin la licencia el RNDC vuelve a rechazar el
                    # manifiesto (MAN246). Si el formulario trae el nombre,
                    # apellido y municipio, se registra y se reintenta.
                    intentos_conductor += 1
                    if not v.get("Nombre_Conductor") or not v.get("Apellido1_Conductor") or not v.get("Municipio_Conductor"):
                        log(f"❌ El conductor no está registrado en el RNDC ({e.mensaje}). Para "
                            f"registrarlo automáticamente hacen falta su Nombre, Apellido y "
                            f"Municipio en el formulario.")
                        raise
                    log(f"    El conductor no está registrado o su licencia salió vencida ({e.mensaje}) -- "
                        f"registrándolo con el navegador (para traer la licencia vigente) y reintentando...")
                    guardado = crear_tercero_via_navegador(
                        usuario, password, (v.get("Tipo_Documento_Conductor") or "C").strip(),
                        _limpiar_numero(v["Cedula_Conductor"]),
                        v["Nombre_Conductor"], v["Apellido1_Conductor"],
                        v.get("Apellido2_Conductor"), v["Municipio_Conductor"],
                        direccion=v.get("Direccion_Conductor"), log=log,
                    )
                    if not guardado:
                        log(f"❌ No se pudo registrar al conductor {v['Cedula_Conductor']} "
                            f"por el navegador. Revísalo a mano en el RNDC.")
                        raise
                    log(f"    ✅ Conductor {v['Cedula_Conductor']} registrado.")
                    ajustes.append(f"Conductor {v['Cedula_Conductor']} registrado/refrescado como Tercero")
                else:
                    raise
        log(f"✅ Radicado del manifiesto: {radicado_manifiesto}")
        resumen.append(f"Manifiesto {v['Consecutivo']} -> radicado: {radicado_manifiesto}")

    except ErrorRNDC as e:
        if e.mensaje.startswith(("El RNDC no respondió", "No se pudo conectar con el RNDC")):
            log(f"❌ {e.mensaje}")
            if resumen:
                log("⚠️  Ya se había creado algo en el RNDC antes de este fallo (mira el resumen del viaje): "
                    "revisa en el RNDC antes de volver a intentar para no duplicar.")
        else:
            log(f"❌ El RNDC rechazó la solicitud: {e.mensaje}")
        return {"ok": False, "error": e.mensaje, "resumen": resumen, "archivos": []}

    archivos = descargar_pdfs_del_viaje(usuario, password, v, tramos, radicado_manifiesto, log)

    # Para la columna "Nombre Conductor"/"Nombre Titular" de Sheets --
    # igual que ya hacía Selenium: se usa el nombre que trajo el
    # formulario si ya se tenía (ej. al registrar a alguien nuevo en este
    # mismo viaje), y si no, se consulta al RNDC por su cédula.
    nombre_conductor_real = v.get("Nombre_Conductor") or None
    if not nombre_conductor_real and v.get("Cedula_Conductor"):
        nombre_conductor_real = consultar_nombre_tercero_api(
            usuario, password, nit_empresa, (v.get("Tipo_Documento_Conductor") or "C").strip(),
            _limpiar_numero(v["Cedula_Conductor"]), log=log
        )
    nombre_titular_real = v.get("Nombre_Titular") or None
    if not nombre_titular_real and v.get("Cedula_Titular"):
        nombre_titular_real = consultar_nombre_tercero_api(
            usuario, password, nit_empresa, "C", _limpiar_numero(v["Cedula_Titular"]), log=log
        )

    log("┌─────────── ✅ RESUMEN DEL VIAJE ───────────")
    for linea in resumen:
        log(f"│ {linea}")
    if ajustes:
        log("│ Se ajustó solo, por detrás:")
        for ajuste in ajustes:
            log(f"│   • {ajuste}")
    else:
        log("│ No hizo falta ajustar nada por detrás.")
    if archivos:
        log(f"│ PDF descargados: {', '.join(archivos)}")
    else:
        log("│ ⚠️  No se pudo descargar ningún PDF (revisa el log de arriba).")
    log("└─────────────────────────────────────────────")

    return {
        "ok": True, "error": None, "resumen": resumen, "archivos": archivos,
        "radicado_remesa": tramos[0]["radicado"],
        "radicado_manifiesto": radicado_manifiesto,
        "ajustes": ajustes,
        "nombre_conductor_real": nombre_conductor_real,
        "nombre_titular_real": nombre_titular_real,
    }


MOTIVOS_ANULACION_MANIFIESTO = {
    "D": "Error Digitación",
    "S": "Cancelación Servicio",
    "R": "Cambio en las Remesas",
    "T": "Cambio de Tarifa",
    "G": "Cambio de Destino por el Generador",
    "C": "Cambio de Conductor",
    "V": "Cambio de Vehículo",
}


def anular_manifiesto_api(usuario, password, nit_empresa, numero_manifiesto, motivo,
                           manifiesto_nuevo=None, observaciones=None, log=None):
    """Anula un Manifiesto YA CREADO (procesoid=32) -- ESTO NO SE PUEDE
    DESHACER. numero_manifiesto es el radicado del manifiesto (el mismo
    que devuelve crear_manifiesto_api/aparece en el resumen del viaje).
    motivo debe ser uno de los códigos de MOTIVOS_ANULACION_MANIFIESTO
    ('D', 'S', 'R', 'T', 'G', 'C' o 'V') -- campo obligatorio en el
    sitio real (tiene asterisco). manifiesto_nuevo (el consecutivo del
    manifiesto que lo reemplaza, si lo hay) y observaciones son
    opcionales. Devuelve la respuesta cruda del RNDC; lanza ErrorRNDC si
    la rechaza."""
    if motivo not in MOTIVOS_ANULACION_MANIFIESTO:
        raise ErrorRNDC(f"Motivo de anulación inválido: '{motivo}'. Debe ser uno de: "
                         f"{', '.join(f'{k} ({v})' for k, v in MOTIVOS_ANULACION_MANIFIESTO.items())}.")
    variables = (
        f"<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>"
        f"<NUMMANIFIESTOCARGA>{numero_manifiesto}</NUMMANIFIESTOCARGA>"
        f"<MOTIVOANULACIONMANIFIESTO>{motivo}</MOTIVOANULACIONMANIFIESTO>"
        f"{f'<NUMMANIFIESTOCARGANUEVO>{manifiesto_nuevo}</NUMMANIFIESTOCARGANUEVO>' if manifiesto_nuevo else ''}"
        f"{f'<OBSERVACIONES>{observaciones}</OBSERVACIONES>' if observaciones else ''}"
    )
    # La guía del RNDC reserva el servidor rndcws2 SOLO para expedir
    # remesas y manifiestos; anular (proceso 32) va por el servidor general
    # (rndcws). Por eso antes daba "WS2 No se puede atender la solicitud".
    respuesta = _llamar(usuario, password, tipo=1, procesoid=32,
                         variables_xml=variables, servidor="real_terceros")
    _, error = _extraer_radicado_o_error(respuesta)
    if error:
        raise ErrorRNDC(error, respuesta)
    if log:
        log(f"✅ Manifiesto {numero_manifiesto} anulado (motivo: {MOTIVOS_ANULACION_MANIFIESTO[motivo]}).")
    return respuesta


def _rndc_responde():
    """Chequeo rápido (máx. 6 s) de si el sitio web del RNDC contesta. Se usa
    para no repetir una descarga que va a fallar igual cuando el RNDC está
    caído o muy lento."""
    try:
        import requests as _rq
        r = _rq.get(URL_LOGIN, timeout=6)
        return r.status_code < 500
    except Exception:
        return False


def _reimprimir_documento_una_vez(usuario, password, tipo_documento, radicado, nombre_archivo, log=None,
                                   por="radicado", invisible=False):
    """Descarga de nuevo el PDF de una Remesa o un Manifiesto YA
    creado, dado su radicado -- para cuando se perdió el PDF original o
    hace falta una copia extra. Abre un navegador solo para este paso
    (el Web Service no entrega el PDF directamente -- ver
    descargar_pdfs_del_viaje). tipo_documento es 'remesa' o
    'manifiesto'. 'por' dice qué es lo que se escribió en 'radicado':
    "radicado" (el número que da el RNDC) o "consecutivo" (el de la
    empresa, ej. AF46561) -- la página de Reimprimir del RNDC tiene una
    casilla distinta para cada uno, y el dato tiene que ir en la suya.
    Devuelve el nombre del archivo descargado, o None si no se pudo."""
    log = log or (lambda m: None)
    driver = None
    por_consecutivo = (por == "consecutivo")
    try:
        log(f"    Abriendo el navegador solo para reimprimir el PDF (buscando por {'consecutivo' if por_consecutivo else 'radicado'})...")
        chrome_options = crear_opciones_chrome(carpeta_descargas=CARPETA_DESCARGAS, invisible=invisible)
        driver = crear_driver_con_limite(chrome_options, obtener_chromedriver_path())
        driver.set_page_load_timeout(25)
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.common.by import By
        import time as _time
        import os as _os

        wait = WebDriverWait(driver, 20)
        driver.get(URL_LOGIN)
        _time.sleep(1)
        wait.until(lambda d: d.find_element(By.ID, "dnn_ctr390_FormLogIn_edUsername")).send_keys(usuario)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_edPassword").send_keys(password)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_btIngresar").click()
        _time.sleep(2)

        if tipo_documento == "remesa":
            # Casilla del consecutivo de la remesa: el nombre sale de la
            # variable CONSECUTIVOREMESA del RNDC (igual que el manifiesto
            # usa NUMMANIFIESTOCARGA); si el sitio la llama distinto, el
            # error dirá qué casillas se buscaron.
            casilla = (
                ["dnn_ctr394_ReimprimirRemesa_CONSECUTIVOREMESA", "dnn_ctr394_ReimprimirRemesa_NUMREMESA"]
                if por_consecutivo else "dnn_ctr394_ReimprimirRemesa_RADICADO"
            )
            archivo = descargar_pdf_documento(
                driver, wait, URL_REIMPRIMIR_REMESA, radicado, nombre_archivo,
                CARPETA_DESCARGAS, casilla,
                "dnn_ctr394_ReimprimirRemesa_btImprimir", log,
            )
        else:
            casilla = (
                "dnn_ctr394_ReimprimirManifiesto_NUMMANIFIESTOCARGA"
                if por_consecutivo else "dnn_ctr394_ReimprimirManifiesto_RADICADO"
            )
            archivo = descargar_pdf_documento(
                driver, wait, URL_REIMPRIMIR_MANIFIESTO, radicado, nombre_archivo,
                CARPETA_DESCARGAS, casilla,
                "dnn_ctr394_ReimprimirManifiesto_btImprimir", log,
                id_boton_consultar="dnn_ctr394_ReimprimirManifiesto_btConsultar",
            )
        return _os.path.basename(archivo) if archivo else None
    except Exception as e:
        log(f"⚠️  No se pudo reimprimir: {e}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def reimprimir_documento_api(usuario, password, tipo_documento, radicado, nombre_archivo, log=None,
                               por="radicado"):
    """El navegador trabaja INVISIBLE y se intenta UNA sola vez (sin reintento
    con ventana visible: si falla, falla rápido)."""
    log = log or (lambda m: None)
    archivo = _reimprimir_documento_una_vez(usuario, password, tipo_documento, radicado,
                                             nombre_archivo, log, por, invisible=True)
    if not archivo and not _rndc_responde():
        log("⚠️  El sitio del RNDC no está respondiendo ahora. Prueba de nuevo en unos minutos.")
    return archivo


def _limpiar_para_nombre(texto):
    """Deja solo letras, números, guion y guion bajo (para nombres de archivo)."""
    return "".join(c for c in str(texto or "") if c.isalnum() or c in "-_").upper()


def consultar_manifiesto_api(usuario, password, nit_empresa, consecutivo_manifiesto, log=None):
    """Consulta (solo lectura) un manifiesto ya creado y devuelve
    {"placa": str|None, "remesas": [consecutivos]}. Es "a mejor esfuerzo":
    si el RNDC no responde como se espera, devuelve lo que pudo (o vacío)
    y NUNCA lanza error -- quien la llama pide los datos a mano en ese caso."""
    log = log or (lambda m: None)
    documento = (
        f"<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>"
        f"<NUMMANIFIESTOCARGA>{consecutivo_manifiesto}</NUMMANIFIESTOCARGA>"
    )
    for variables in ("NUMPLACA,REMESASMAN", "NUMPLACA"):
        try:
            respuesta = _llamar(usuario, password, tipo=3, procesoid=4,
                                 variables_xml=variables, documento_xml=documento,
                                 servidor="real_terceros")
            raiz = ET.fromstring(str(respuesta))
        except Exception as e:
            log(f"    (No se pudo consultar el manifiesto en el RNDC con '{variables}': {e})")
            continue
        placa = None
        for el in raiz.iter("NUMPLACA"):
            if el.text and el.text.strip():
                placa = el.text.strip().upper()
                break
        remesas = []
        for el in raiz.iter("CONSECUTIVOREMESA"):
            if el.text and el.text.strip() and el.text.strip() not in remesas:
                remesas.append(el.text.strip())
        if placa or remesas:
            return {"placa": placa, "remesas": remesas}
        error = raiz.find("ErrorMSG")
        if error is not None:
            log(f"    (El RNDC respondió a la consulta del manifiesto: {''.join(error.itertext()).strip()[:160]})")
    return {"placa": None, "remesas": []}


def _reimprimir_viaje_una_vez(usuario, password, nit_empresa, consecutivo_manifiesto,
                               remesas=None, placa=None, log=None, invisible=False):
    """Descarga de una vez el PDF del manifiesto y los de todas sus remesas,
    con la placa en el nombre de cada archivo (ej. Manifiesto_AF49232_JYM305).
    Si no se dan las remesas o la placa, se intentan averiguar consultando
    el manifiesto en el RNDC. Usa UN solo navegador para todo.
    Devuelve {"placa", "remesas", "archivos": [{"tipo","consecutivo","archivo"}],
    "faltantes": [textos]}."""
    log = log or (lambda m: None)
    remesas = [r for r in (remesas or []) if r]
    placa = _limpiar_para_nombre(placa)
    faltantes = []

    if not placa or not remesas:
        log("    Consultando el manifiesto en el RNDC para averiguar su placa y sus remesas...")
        datos = consultar_manifiesto_api(usuario, password, nit_empresa, consecutivo_manifiesto, log)
        if not placa and datos["placa"]:
            placa = _limpiar_para_nombre(datos["placa"])
        if not remesas and datos["remesas"]:
            remesas = datos["remesas"]
    if not placa:
        faltantes.append("No se pudo averiguar la placa; los archivos quedaron sin placa en el nombre "
                         "(puedes escribirla en el campo Placa y repetir).")
    if not remesas:
        faltantes.append("No se pudieron averiguar las remesas del manifiesto; solo se descargó el manifiesto "
                         "(escribe los consecutivos de las remesas en su campo y repite).")
    sufijo = f"_{placa}" if placa else ""

    archivos = []
    driver = None
    remesas_a_bajar = None
    try:
        log("    Abriendo el navegador solo para reimprimir los PDF...")
        chrome_options = crear_opciones_chrome(carpeta_descargas=CARPETA_DESCARGAS, invisible=invisible)
        driver = crear_driver_con_limite(chrome_options, obtener_chromedriver_path())
        driver.set_page_load_timeout(25)
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.common.by import By
        import time as _time
        import os as _os

        wait = WebDriverWait(driver, 20)
        driver.get(URL_LOGIN)
        _time.sleep(1)
        wait.until(lambda d: d.find_element(By.ID, "dnn_ctr390_FormLogIn_edUsername")).send_keys(usuario)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_edPassword").send_keys(password)
        driver.find_element(By.ID, "dnn_ctr390_FormLogIn_btIngresar").click()
        _time.sleep(2)

        log(f"    Descargando el manifiesto {consecutivo_manifiesto}...")
        ruta = descargar_pdf_documento(
            driver, wait, URL_REIMPRIMIR_MANIFIESTO, consecutivo_manifiesto,
            f"Manifiesto_{_limpiar_para_nombre(consecutivo_manifiesto)}{sufijo}",
            CARPETA_DESCARGAS, "dnn_ctr394_ReimprimirManifiesto_NUMMANIFIESTOCARGA",
            "dnn_ctr394_ReimprimirManifiesto_btImprimir", log,
            id_boton_consultar="dnn_ctr394_ReimprimirManifiesto_btConsultar",
        )
        if ruta:
            archivos.append({"tipo": "Manifiesto", "consecutivo": consecutivo_manifiesto,
                              "archivo": _os.path.basename(ruta)})
        else:
            faltantes.append(f"No se pudo descargar el manifiesto {consecutivo_manifiesto}.")
            if not _rndc_responde():
                log("⚠️  El sitio del RNDC no responde; se omiten las remesas para no esperar de más.")
                faltantes.append("El sitio del RNDC no está respondiendo; no se intentaron las remesas.")
                remesas_a_bajar = []
            else:
                remesas_a_bajar = remesas
        if archivos or remesas_a_bajar is None:
            remesas_a_bajar = remesas

        for remesa in remesas_a_bajar:
            log(f"    Descargando la remesa {remesa}...")
            ruta = descargar_pdf_documento(
                driver, wait, URL_REIMPRIMIR_REMESA, remesa,
                f"Remesa_{_limpiar_para_nombre(remesa)}{sufijo}",
                CARPETA_DESCARGAS,
                ["dnn_ctr394_ReimprimirRemesa_CONSECUTIVOREMESA", "dnn_ctr394_ReimprimirRemesa_NUMREMESA"],
                "dnn_ctr394_ReimprimirRemesa_btImprimir", log,
            )
            if ruta:
                archivos.append({"tipo": "Remesa", "consecutivo": remesa,
                                  "archivo": _os.path.basename(ruta)})
            else:
                faltantes.append(f"No se pudo descargar la remesa {remesa}.")
    except Exception as e:
        log(f"⚠️  No se pudo reimprimir: {e}")
        faltantes.append(f"Error durante la descarga: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
    return {"placa": placa or None, "remesas": remesas, "archivos": archivos, "faltantes": faltantes}


def reimprimir_viaje_api(usuario, password, nit_empresa, consecutivo_manifiesto,
                          remesas=None, placa=None, log=None):
    """Navegador INVISIBLE y UN solo intento (sin reintento con ventana visible)."""
    log = log or (lambda m: None)
    r = _reimprimir_viaje_una_vez(usuario, password, nit_empresa, consecutivo_manifiesto,
                                   remesas, placa, log, invisible=True)
    if not r["archivos"] and not _rndc_responde():
        log("⚠️  El sitio del RNDC no está respondiendo ahora. Prueba de nuevo en unos minutos.")
        r["faltantes"].append("El sitio del RNDC no está respondiendo ahora. Prueba de nuevo en unos minutos.")
    return r


def _descargar_pdfs_del_viaje_una_vez(usuario, password, v, tramos, radicado_manifiesto, log, invisible=False):
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
        chrome_options = crear_opciones_chrome(carpeta_descargas=CARPETA_DESCARGAS, invisible=invisible)
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
            elif not archivos and not _rndc_responde():
                log("⚠️  El sitio del RNDC no responde; se omiten los demás PDF para no esperar de más. "
                    "El viaje ya quedó creado: bájalos luego desde Herramientas → Reimprimir.")
                return archivos

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


def descargar_pdfs_del_viaje(usuario, password, v, tramos, radicado_manifiesto, log):
    """Baja los PDF del viaje recién creado con el navegador INVISIBLE y UN
    solo intento. Si falla, el viaje ya quedó creado: los PDF se piden luego
    desde Herramientas → Reimprimir."""
    archivos = _descargar_pdfs_del_viaje_una_vez(usuario, password, v, tramos, radicado_manifiesto,
                                                  log, invisible=True)
    if not archivos:
        log("⚠️  No se bajaron los PDF. El viaje ya quedó creado: puedes pedirlos luego desde "
            "Herramientas → Reimprimir.")
    return archivos


MOTIVOS_ANULACION_CUMPLIDO = {
    "D": "Error Digitación",
    "O": "Otro",
}

TIPOS_ANULACION_CUMPLIDO = {
    # clave: (procesoid, nombre en español)
    "inicial": (54, "cumplido inicial de remesa"),
    "remesa": (28, "cumplido de remesa"),
}


def anular_cumplido_remesa_api(usuario, password, nit_empresa, consecutivo_remesa, motivo,
                                tipo="inicial", cargue_descargue=None,
                                observaciones=None, log=None, numero_manifiesto=None):
    """Anula el cumplido de UNA remesa. tipo="inicial" -> proceso 54 (Anular
    Cumplido Inicial de Remesa); tipo="remesa" -> proceso 28 (Anular Cumplido de
    Remesa). Va por el servidor general (rndcws), no por WS2. Devuelve la
    respuesta cruda; lanza ErrorRNDC si el RNDC la rechaza.

    OJO: los nombres de las variables se dedujeron de las pantallas del
    RNDC. CARGUEDESCARGUE NO existe en el diccionario del proceso 54 (el
    RNDC respondió Error 13), por eso el parámetro cargue_descargue ya no
    se manda; se deja solo por compatibilidad."""
    if tipo not in TIPOS_ANULACION_CUMPLIDO:
        raise ErrorRNDC(f"Tipo de anulación inválido: '{tipo}'.")
    if motivo not in MOTIVOS_ANULACION_CUMPLIDO:
        raise ErrorRNDC(f"Motivo de anulación inválido: '{motivo}'. Debe ser uno de: "
                         f"{', '.join(f'{k} ({v})' for k, v in MOTIVOS_ANULACION_CUMPLIDO.items())}.")
    procesoid, nombre = TIPOS_ANULACION_CUMPLIDO[tipo]
    # El RNDC exige el consecutivo del manifiesto (Error AC1022) y
    # observaciones de MÍNIMO 20 caracteres (Error AC1052).
    if not numero_manifiesto:
        raise ErrorRNDC("Falta el consecutivo del manifiesto al que pertenece la remesa.")
    if len((observaciones or "").strip()) < 20:
        raise ErrorRNDC("Las observaciones son obligatorias y deben tener mínimo 20 caracteres "
                         "(el RNDC no acepta menos).")
    observaciones = observaciones.strip()
    # El RNDC no publica el nombre exacto de la variable del motivo para
    # estos procesos y ya rechazó "MOTIVOANULACIONCUMPLIDO" (Error 13: "no
    # se encuentra en Diccionario de Datos"). Un Error 13 significa que el
    # RNDC NO procesó nada, así que es seguro ir probando nombres
    # candidatos hasta que uno sea aceptado. El que funcione queda en el log.
    candidatos_motivo = [
        "CODMOTIVOANULACIONCUMPLIDO",
        "MOTIVOANULACIONCUMPLIDOREMESA",
        "MOTIVOANULACIONCUMPLIDOINICIAL",
        "CODMOTIVOANULACION",
        "MOTIVOANULACION",
        "NOMMOTIVOANULACIONCUMPLIDO",
    ]
    incluir_observaciones = bool(observaciones)
    probados = []
    for nombre_motivo in candidatos_motivo:
        variables = (
            f"<NUMNITEMPRESATRANSPORTE>{nit_empresa}</NUMNITEMPRESATRANSPORTE>"
            f"<CONSECUTIVOREMESA>{consecutivo_remesa}</CONSECUTIVOREMESA>"
            f"<NUMMANIFIESTOCARGA>{numero_manifiesto}</NUMMANIFIESTOCARGA>"
            f"<{nombre_motivo}>{motivo}</{nombre_motivo}>"
            + (f"<OBSERVACIONES>{observaciones}</OBSERVACIONES>" if incluir_observaciones else "")
        )
        respuesta = _llamar(usuario, password, tipo=1, procesoid=procesoid,
                             variables_xml=variables, servidor="real_terceros")
        _, error = _extraer_radicado_o_error(respuesta)
        if not error:
            if log:
                log(f"✅ Anulado el {nombre} de la remesa {consecutivo_remesa}. "
                    f"(variable de motivo aceptada por el RNDC: {nombre_motivo})")
            return respuesta
        probados.append(nombre_motivo)
        texto = str(error)
        if "no se encuentra en Diccionario" in texto:
            if nombre_motivo in texto:
                continue  # ese nombre no existe: probar el siguiente
            if "OBSERVACIONES" in texto and incluir_observaciones:
                incluir_observaciones = False  # el proceso no tiene observaciones
                if log:
                    log("ℹ️ El RNDC no acepta observaciones en este proceso; se reintenta sin ellas.")
                # reintentar el mismo nombre de motivo sin observaciones
                candidatos_motivo.insert(candidatos_motivo.index(nombre_motivo) + 1, nombre_motivo)
                continue
        raise ErrorRNDC(f"{error} [variable de motivo usada: {nombre_motivo}]", respuesta)
    raise ErrorRNDC(
        "El RNDC no reconoció ninguno de los nombres probados para el motivo de anulación ("
        + ", ".join(probados[:6]) + "). Se necesita el nombre exacto de la variable del proceso "
        f"{procesoid}.")
