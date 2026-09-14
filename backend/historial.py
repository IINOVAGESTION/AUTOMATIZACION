"""
backend/historial.py — Historial persistente de viajes (para las
estadísticas del panel, detectar consecutivos repetidos y sugerir el
conductor frecuente de cada placa).
"""
import os
import re
import json
import threading
from datetime import datetime, timedelta

import rndc_core

ARCHIVO_HISTORIAL = os.path.join(rndc_core.obtener_carpeta_programa(), "historial_viajes.json")
candado_historial = threading.Lock()


def registrar_historial(descripcion, ok, duracion_seg, placa=None, cedula_conductor=None):
    """Agrega un renglón al historial persistente cada vez que un viaje
    termina (bien o mal), para poder calcular las estadísticas del panel
    de inicio y detectar combinaciones frecuentes de placa+conductor."""
    registro = {
        "fecha": datetime.now().isoformat(),
        "descripcion": descripcion,
        "ok": bool(ok),
        "duracion_seg": round(duracion_seg, 1),
        "placa": (placa or "").strip().upper(),
        "cedula_conductor": (cedula_conductor or "").strip(),
    }
    with candado_historial:
        try:
            with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
                historial = json.load(f)
        except Exception:
            historial = []
        historial.append(registro)
        # Se guardan como máximo los últimos 500 registros, para que el
        # archivo no crezca sin límite.
        historial = historial[-500:]
        try:
            with open(ARCHIVO_HISTORIAL, "w", encoding="utf-8") as f:
                json.dump(historial, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def buscar_consecutivo_hoy(consecutivo):
    """Revisa el historial y devuelve la hora (texto) en la que ese
    consecutivo ya se usó HOY, o None si no se ha usado."""
    if not consecutivo:
        return None
    try:
        with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
            historial = json.load(f)
    except Exception:
        return None

    hoy = datetime.now().date()
    consecutivo_norm = consecutivo.strip().upper()

    for r in reversed(historial):  # el más reciente primero
        try:
            fecha_r = datetime.fromisoformat(r["fecha"])
        except Exception:
            continue
        if fecha_r.date() != hoy:
            continue
        m = re.search(r"Consecutivo\s+(\S+)", r.get("descripcion", ""))
        if m and m.group(1).strip().upper() == consecutivo_norm:
            return fecha_r.strftime("%I:%M %p")
    return None


def sugerir_conductor_para_placa(placa):
    """Revisa el historial y, si esa placa casi siempre se ha usado con el
    mismo conductor, devuelve esa cédula. Si no hay un patrón claro (menos
    de 2 coincidencias, o está repartido entre varios conductores
    distintos), devuelve None."""
    if not placa:
        return None
    try:
        with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
            historial = json.load(f)
    except Exception:
        return None

    placa_norm = placa.strip().upper()
    conteo = {}
    for r in historial:
        if r.get("placa") == placa_norm and r.get("cedula_conductor"):
            cedula = r["cedula_conductor"]
            conteo[cedula] = conteo.get(cedula, 0) + 1

    if not conteo:
        return None

    mas_frecuente = max(conteo, key=conteo.get)
    total = sum(conteo.values())
    # Solo se sugiere si aparece al menos 2 veces y representa la mayoría
    # clara de los casos (60% o más de las veces que se usó esa placa).
    if conteo[mas_frecuente] >= 2 and conteo[mas_frecuente] / total >= 0.6:
        return mas_frecuente
    return None


def calcular_estadisticas():
    """Lee el historial y calcula los números del panel de inicio."""
    try:
        with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
            historial = json.load(f)
    except Exception:
        historial = []

    ahora = datetime.now()
    hoy = ahora.date()
    hace_7_dias = ahora - timedelta(days=7)

    de_hoy = [r for r in historial if datetime.fromisoformat(r["fecha"]).date() == hoy]
    de_la_semana = [r for r in historial if datetime.fromisoformat(r["fecha"]) >= hace_7_dias]
    con_error_semana = [r for r in de_la_semana if not r["ok"]]

    duraciones = [r["duracion_seg"] for r in de_la_semana if r["ok"]]
    promedio = sum(duraciones) / len(duraciones) if duraciones else 0

    return {
        "hoy": len(de_hoy),
        "semana": len(de_la_semana),
        "con_error": len(con_error_semana),
        "promedio_seg": round(promedio),
    }
