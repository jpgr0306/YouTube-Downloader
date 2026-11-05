@echo off
cd /d "%~dp0"
echo Instalando/atualizando dependencias...
python -m pip install -q --upgrade -r requirements.txt uvicorn
python local.py
pause
