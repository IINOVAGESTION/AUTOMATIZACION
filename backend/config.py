"""
backend/config.py — Valores de configuración editables por el usuario:
enlaces de las hojas de Google Sheets, versión de la app, y detalles
menores del servidor. Edita aquí, no en servidor_web.py.
"""
import rndc_core

RUTAS_SIN_ACTIVIDAD = {"estado", "static"}

URL_HOJA_USUARIOS = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSotDHXpy-hRY4T84tvx9czqG8LSQR_3kqS4lIJ6aHiH4MTFgxTUR3jfU5ronsEKYDm8hIcgAzPuKP0/pub?gid=0&single=true&output=csv"

VERSION_APP = "1.1"

URL_HOJA_VERSION = ""

URL_WEBHOOK_SHEETS = "https://script.google.com/macros/s/AKfycbz4nAmWVEuquOTF6xy8mwvkFKk3pa5fLf8DPoZurYEI6C4QiaLKkpbdOvqs9ZhpZrT5nQ/exec"

# Mismo texto por defecto que usa rndc_core.py, para que el formulario
# arranque ya lleno con algo razonable (y se pueda editar si hace falta).
OBSERVACIONES_POR_DEFECTO = rndc_core.FIJOS_MANIFIESTO["RECOMENDACIONES"]
