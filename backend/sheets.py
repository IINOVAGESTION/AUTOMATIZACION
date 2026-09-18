"""
backend/sheets.py — Todo lo relacionado con Google Sheets: validar el
login de usuarios de la app, registrar cada viaje terminado, y
revisar si hay una versión más nueva publicada.
"""
import time
import csv
import io
import requests
from datetime import datetime

from .config import URL_WEBHOOK_SHEETS, URL_HOJA_VERSION, VERSION_APP, URL_HOJA_USUARIOS

_cache_actualizacion = {"revisado_en": 0, "resultado": None}


def version_a_tupla(texto):
    """Convierte '1.10' -> (1, 10) para comparar versiones correctamente
    (no como texto, donde '1.10' < '1.9' por error)."""
    partes = []
    for p in (texto or "").strip().split("."):
        try:
            partes.append(int(p))
        except ValueError:
            partes.append(0)
    return tuple(partes) or (0,)


def registrar_en_sheets(v, resultado, log=print):
    """Después de un viaje terminado con éxito, manda una fila a la hoja
    de Google Sheets configurada (vía el 'Web App' de Apps Script), con
    el orden de columnas: Consecutivo, Fecha, Mes, Año, N (editable a
    mano), Pendiente/OK (editable a mano), Cliente, CC Conductor,
    Nombre Conductor, CC Titular, Nombre Titular, Placa, Remolque,
    Usuario (quién de la empresa hizo el viaje, según con qué usuario
    inició sesión en la app).
    Si no hay URL configurada, o algo falla, no interrumpe nada más
    (es un extra, no algo crítico para el viaje en sí)."""
    if not URL_WEBHOOK_SHEETS.strip():
        return
    ahora = datetime.now()
    payload = {
        "consecutivo": v.get("Consecutivo", ""),
        "fecha": ahora.strftime("%d/%m/%Y"),
        "mes": ahora.strftime("%m"),
        "anio": ahora.strftime("%Y"),
        "cliente": v.get("Cliente", ""),
        "cc_conductor": v.get("Cedula_Conductor", ""),
        "nombre_conductor": resultado.get("nombre_conductor_real") or "",
        "cc_titular": v.get("Cedula_Titular", ""),
        "nombre_titular": resultado.get("nombre_titular_real") or "",
        "placa": v.get("Placa", ""),
        "remolque": v.get("Placa_Remolque", ""),
        "usuario": v.get("usuario_app", ""),
    }
    try:
        resp = requests.post(URL_WEBHOOK_SHEETS, json=payload, timeout=10, allow_redirects=True)
        cuerpo = (resp.text or "").strip()
        parece_html_de_google = cuerpo.lower().startswith(("<!doctype html", "<html"))
        if resp.status_code == 200 and not parece_html_de_google:
            log(f"    📊 Fila registrada en la tabla de Google Sheets (usuario: '{payload['usuario']}').")
        else:
            adelanto = cuerpo[:200].replace("\n", " ")
            log(f"    ⚠️  Google respondió, pero no parece que haya guardado la fila de verdad "
                f"(código {resp.status_code}). Esto probablemente significa que el 'Web App' "
                f"todavía necesita que aceptes un permiso en Google, o no quedó bien publicado. "
                f"Lo que contestó: {adelanto}")
    except Exception as e:
        log(f"    ⚠️  No se pudo registrar la fila en Google Sheets (no es grave, "
            f"el viaje sí quedó bien hecho): {e}")


def revisar_actualizacion():
    """Consulta la hoja de versión (con caché de 30 min) y devuelve
    {"hay_nueva": bool, "version": str, "enlace": str} o None si no hay
    hoja configurada o algo falla (nunca interrumpe el uso normal)."""
    if not URL_HOJA_VERSION.strip():
        return None

    ahora = time.time()
    if ahora - _cache_actualizacion["revisado_en"] < 1800:  # 30 minutos
        return _cache_actualizacion["resultado"]

    resultado = None
    try:
        resp = requests.get(URL_HOJA_VERSION, timeout=6)
        resp.raise_for_status()
        contenido = resp.content.decode("utf-8-sig")
        filas = list(csv.reader(io.StringIO(contenido)))
        if len(filas) >= 2:
            encabezado = [c.strip().lower() for c in filas[0]]
            fila = filas[1]
            idx_version = encabezado.index("versionmasreciente") if "versionmasreciente" in encabezado else None
            idx_enlace = encabezado.index("enlacedescarga") if "enlacedescarga" in encabezado else None
            if idx_version is not None and idx_enlace is not None and len(fila) > max(idx_version, idx_enlace):
                version_remota = fila[idx_version].strip()
                enlace = fila[idx_enlace].strip()
                if version_remota and version_a_tupla(version_remota) > version_a_tupla(VERSION_APP):
                    resultado = {"hay_nueva": True, "version": version_remota, "enlace": enlace}
                else:
                    resultado = {"hay_nueva": False, "version": version_remota, "enlace": enlace}
    except Exception:
        resultado = None  # cualquier falla se ignora en silencio; no es crítico

    _cache_actualizacion["revisado_en"] = ahora
    _cache_actualizacion["resultado"] = resultado
    return resultado


def validar_usuario_app(usuario, password):
    """Consulta la hoja de Google Sheets publicada y revisa si el usuario
    y contraseña coinciden con una fila marcada como Activo = Si. Si esa
    fila también tiene un usuario/contraseña del RNDC vinculado (columnas
    opcionales), los devuelve también."""
    try:
        resp = requests.get(URL_HOJA_USUARIOS, timeout=10)
        resp.raise_for_status()
        # 'utf-8-sig' quita el BOM que a veces agrega Google al inicio del CSV
        contenido = resp.content.decode("utf-8-sig")

        filas = list(csv.reader(io.StringIO(contenido)))
        if not filas:
            return False, "La lista de usuarios está vacía.", None

        encabezado = [c.strip().lower() for c in filas[0]]
        try:
            idx_usuario = encabezado.index("usuario")
            idx_password = encabezado.index("contraseña") if "contraseña" in encabezado else encabezado.index("contrasena")
            idx_activo = encabezado.index("activo")
        except ValueError:
            return False, (
                "La hoja de Google Sheets no tiene las columnas esperadas "
                f"(Usuario, Contraseña, Activo). Encabezados encontrados: {encabezado}"
            ), None

        # Columnas opcionales: usuario/contraseña del RNDC vinculado. Si no
        # existen en la hoja, simplemente no se vincula nada (como antes).
        nombres_usuario_rndc = ("usuariorndc", "usuario_rndc", "usuario rndc")
        nombres_password_rndc = ("contraseñarndc", "contraseña_rndc", "contraseña rndc",
                                  "contrasenarndc", "contrasena_rndc", "contrasena rndc")
        idx_usuario_rndc = next((encabezado.index(n) for n in nombres_usuario_rndc if n in encabezado), None)
        idx_password_rndc = next((encabezado.index(n) for n in nombres_password_rndc if n in encabezado), None)

        for fila in filas[1:]:
            if len(fila) <= max(idx_usuario, idx_password, idx_activo):
                continue
            if fila[idx_usuario].strip() == usuario and fila[idx_password].strip() == password:
                if fila[idx_activo].strip().lower() in ("si", "sí", "yes", "true", "1"):
                    rndc_vinculado = None
                    if idx_usuario_rndc is not None and idx_password_rndc is not None:
                        if len(fila) > max(idx_usuario_rndc, idx_password_rndc):
                            u_rndc = fila[idx_usuario_rndc].strip()
                            p_rndc = fila[idx_password_rndc].strip()
                            if u_rndc and p_rndc:
                                rndc_vinculado = (u_rndc, p_rndc)
                    return True, None, rndc_vinculado
                else:
                    return False, "Este usuario existe pero está desactivado.", None
        return False, "Usuario o contraseña incorrectos.", None
    except requests.RequestException as e:
        return False, f"No se pudo consultar la lista de usuarios autorizados: {e}", None
