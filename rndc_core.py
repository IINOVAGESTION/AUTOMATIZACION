"""
RNDC - MOTOR DE AUTOMATIZACIÓN (para usar desde la página web)
=================================================================
Este archivo se mantiene solo por compatibilidad: todo el código real
ahora vive organizado en el paquete `core/` (config, navegador,
utilidades, terceros, documentos, automatizacion). Aquí simplemente
se reexporta todo con los mismos nombres de siempre, para que
`servidor_web.py` y cualquier otro script que use `import rndc_core`
siga funcionando exactamente igual, sin cambiar ni una línea.

Si necesitas modificar algo, ve directo al archivo correspondiente
dentro de `core/` en vez de aquí.
"""

from core.config import (
    URL_LOGIN, URL_LOGOUT, URL_REMESA, URL_REIMPRIMIR_REMESA,
    URL_REIMPRIMIR_MANIFIESTO, URL_MANIFIESTO, URL_TERCERO, URL_VEHICULO,
    URL_MAESTRO,
    obtener_carpeta_programa, ruta_captura,
    CARPETA_DESCARGAS, CARPETA_DESCARGAS_WINDOWS,
    ARCHIVO_SEDES, ARCHIVO_CONDUCTORES,
    FIJOS_REMESA, FIJOS_MANIFIESTO,
)

from core.navegador import (
    registrar_navegador_abandonado, obtener_chromedriver_path,
    crear_opciones_chrome,
)

from core.utilidades import (
    calcular_retencion_ica, traducir_error, limpiar_numero,
    normalizar_tipo_id, set_text, set_select, quitar_tildes,
    set_select_por_texto_parcial, set_autocomplete_municipio,
    esperar_nueva_descarga_en, renombrar_descarga,
    esperar_confirmacion_manifiesto, leer_radicado_y_aceptar_alerta,
    limpiar_capturas_viejas, EXTENSIONES_TEMPORALES, calcular_fecha_pago_por_defecto,
)

from core.terceros import (
    crear_tercero, crear_vehiculo, verificar_y_crear_conductor_si_falta,
    buscar_nombre_tercero_con_sesion_activa, verificar_tercero,
    obtener_lista_sedes_empresa, obtener_lista_conductores_empresa,
)

from core.documentos import descargar_pdf_documento

from core.automatizacion import ejecutar_automatizacion, ejecutar_cola

from core.api_rndc import ejecutar_viaje_api
