@echo off
echo [1/2] Menjalankan build PyInstaller / Nuitka...
call build.bat

echo [2/2] Mengompilasi Setup Installer via Inno Setup...
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\installer.iss

echo Selesai! File installer siap di dist_installer\
pause