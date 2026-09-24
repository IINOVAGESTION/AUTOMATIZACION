@echo off
echo ====================================================
echo   Construyendo el .exe de Automatizacion RNDC
echo ====================================================
echo.

echo [1/3] Instalando dependencias necesarias...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo HUBO UN PROBLEMA instalando las dependencias. Revisa el mensaje de arriba.
    pause
    exit /b 1
)

echo.
echo [2/3] Generando el archivo .exe (puede tardar 1-2 minutos)...
set ICONO=
if exist icono_app.ico (
    set ICONO=--icon=icono_app.ico
) else (
    echo No se encontro icono_app.ico, se genera el .exe con el icono por defecto.
)
set TOKEN_DATA=
if exist token_por_defecto.txt (
    set TOKEN_DATA=--add-data "token_por_defecto.txt;semilla"
) else (
    echo No se encontro token_por_defecto.txt, el .exe quedara sin token de fabrica
    echo ^(el boton Actualizar seguira funcionando igual, solo con el limite normal
    echo de 60 consultas por hora en vez de 5000, a menos que cada quien configure
    echo su propio token desde la app^).
)
python -m PyInstaller --onefile --name AutomatizacionRNDC --noconsole %ICONO% --collect-all selenium --collect-all webdriver_manager --collect-all webview --collect-all flask --collect-all psutil --collect-all zeep --collect-all lxml --add-data "servidor_web.py;semilla" --add-data "rndc_core.py;semilla" --add-data "core;semilla/core" --add-data "backend;semilla/backend" --add-data "templates;semilla/templates" --add-data "static;semilla/static" %TOKEN_DATA% lanzador.py
if errorlevel 1 (
    echo.
    echo HUBO UN PROBLEMA generando el .exe. Revisa el mensaje de arriba.
    pause
    exit /b 1
)

echo.
echo [3/3] Listo!
echo.
echo El archivo quedo en: dist\AutomatizacionRNDC.exe
echo Puedes copiar SOLO ese archivo .exe a otra carpeta o a otra
echo computadora para probarlo (necesita tener Google Chrome instalado).
echo.
pause
