"""
backend/rangos_consecutivos.py — Rastreador local de "en qué número voy"
dentro del rango de consecutivos asignado a cada trabajador (ej. Juan:
AF50000-AF50999). Independiente del historial de viajes (que solo
guarda los últimos 500 registros): esto es un archivo chiquito, uno por
trabajador, que nunca se recorta.
"""
import os
import re
import json
import threading

import rndc_core

ARCHIVO_RANGOS = os.path.join(rndc_core.obtener_carpeta_programa(), "progreso_consecutivos.json")
candado_rangos = threading.Lock()


def parsear_consecutivo(texto):
    """Separa un consecutivo tipo 'AF50000' en su prefijo de letras y su
    número, como una tupla (prefijo, numero). Devuelve (None, None) si el
    texto no tiene ese formato (letras seguidas de números)."""
    if not texto:
        return None, None
    m = re.fullmatch(r"([A-Za-z]*)(\d+)", texto.strip())
    if not m:
        return None, None
    return m.group(1).upper(), int(m.group(2))


def _leer():
    try:
        with open(ARCHIVO_RANGOS, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _guardar(datos):
    try:
        with open(ARCHIVO_RANGOS, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def siguiente_consecutivo_sugerido(usuario_app, consecutivo_inicio, consecutivo_fin):
    """Devuelve el próximo consecutivo libre (texto, ej. 'AF50003') dentro
    del rango [consecutivo_inicio, consecutivo_fin] asignado a este
    usuario de la app, o None si el rango está mal formado o ya se
    agotó por completo."""
    prefijo_ini, num_ini = parsear_consecutivo(consecutivo_inicio)
    prefijo_fin, num_fin = parsear_consecutivo(consecutivo_fin)
    if num_ini is None or num_fin is None or prefijo_ini != prefijo_fin or num_ini > num_fin:
        return None
    with candado_rangos:
        datos = _leer()
        ultimo_usado = datos.get(usuario_app, {}).get("ultimo_usado")
    siguiente = (ultimo_usado + 1) if ultimo_usado is not None and ultimo_usado >= num_ini else num_ini
    if siguiente > num_fin:
        return None  # se agotó el rango asignado
    return f"{prefijo_ini}{siguiente}"


def dentro_del_rango(consecutivo, consecutivo_inicio, consecutivo_fin):
    """True si 'consecutivo' cae dentro de [consecutivo_inicio,
    consecutivo_fin] (mismo prefijo de letras, número en el rango)."""
    prefijo, numero = parsear_consecutivo(consecutivo)
    prefijo_ini, num_ini = parsear_consecutivo(consecutivo_inicio)
    prefijo_fin, num_fin = parsear_consecutivo(consecutivo_fin)
    if numero is None or num_ini is None or num_fin is None:
        return False
    return prefijo == prefijo_ini == prefijo_fin and num_ini <= numero <= num_fin


def marcar_consecutivo_usado(usuario_app, consecutivo):
    """Avanza el rastreador de este usuario para que la próxima
    sugerencia sea posterior a 'consecutivo' -- se llama después de
    crear un viaje con éxito, sin importar si usó el sugerido o escribió
    otro número válido de su mismo rango a mano."""
    _, numero = parsear_consecutivo(consecutivo)
    if numero is None:
        return
    with candado_rangos:
        datos = _leer()
        actual = datos.get(usuario_app, {}).get("ultimo_usado")
        if actual is None or numero > actual:
            datos.setdefault(usuario_app, {})["ultimo_usado"] = numero
            _guardar(datos)
