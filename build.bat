@echo off
REM Genera ClonDisk.exe (requiere Python 3.10+ de 64 bits en Windows)
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --uac-admin ^
  --name ClonDisk --collect-submodules clondisk run_clondisk.py
echo.
echo Listo: dist\ClonDisk.exe
