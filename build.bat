@echo off
setlocal EnableDelayedExpansion
title OmniPresence v2.3.1 - Automated Standalone .EXE Builder (Publisher: Raf446)

cd /d "%~dp0"

echo ====================================================================
echo    OmniPresence Standalone .EXE Builder
echo    App Name  : OmniPresence
echo    Version   : 2.3.1
echo    Publisher : Raf446
echo ====================================================================
echo.

:: 1. Verify Python installation
where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Python tidak ditemukan di PATH sistem.
    echo          Pastikan Python 3.11+ sudah ter-install dan ditambahkan ke PATH.
    if not defined CI pause
    exit /b 1
)

:: 2. Ensure runtime dependencies & PyInstaller are installed
echo [1/4] Memeriksa dependensi runtime ^& PyInstaller...
python -m pip install --upgrade pip >nul 2>nul
if exist "requirements.txt" (
    python -m pip install -r requirements.txt
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Gagal meng-install dependensi dari requirements.txt.
        if not defined CI pause
        exit /b 1
    )
)

python -c "import PyInstaller" >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo [INFO] Meng-install PyInstaller...
    python -m pip install pyinstaller
    if %ERRORLEVEL% neq 0 (
        echo [ERROR] Gagal meng-install PyInstaller.
        if not defined CI pause
        exit /b 1
    )
)

:: 3. Clean old build & dist folders
echo.
echo [2/4] Membersihkan folder build/ dan dist/ lama...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "OmniPresence.spec" del /f /q "OmniPresence.spec"

:: 4. Execute build_exe.py
echo.
echo [3/4] Menjalankan kompilasi PyInstaller via build_exe.py...
python build_exe.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Proses kompilasi gagal! Periksa pesan log di atas.
    if not defined CI pause
    exit /b 1
)

:: 5. Verify final output
echo.
echo [4/4] Memverifikasi hasil build...
if exist "dist\OmniPresence.exe" (
    echo.
    echo ====================================================================
    echo    BUILD BERHASIL!
    echo    Executable : "%~dp0dist\OmniPresence.exe"
    echo    Config Dir : "%~dp0dist\config\"
    echo ====================================================================
) else (
    echo [ERROR] File dist\OmniPresence.exe tidak ditemukan setelah kompilasi.
    if not defined CI pause
    exit /b 1
)

if not defined CI pause
exit /b 0

