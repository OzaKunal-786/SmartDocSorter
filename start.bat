@echo off
TITLE SmartDocSorter - Auto Setup & Launcher
color 0A

:: ============================================================================
:: SmartDocSorter - One-Click Setup & Launch Script
:: Automatically checks Python, creates an isolated virtual environment (.venv),
:: installs all required libraries from requirements.txt, checks Ollama AI,
:: and opens the SmartDocSorter Browser Interface.
:: ============================================================================

echo ========================================================
echo        SMART DOC SORTER - SETUP ^& LAUNCHER
echo ========================================================
echo.

:: Step 1: Verify Python is installed and available in PATH
echo [1/5] Checking Python installation...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not added to PATH.
    echo         Please install Python 3.11 or 3.12 from https://www.python.org/downloads/
    echo         Make sure to check "Add Python to PATH" during installation.
    goto ERROR
)
python --version

:: Step 2: Create a local virtual environment (.venv) if it does not exist yet
echo [2/5] Preparing Python virtual environment (.venv)...
if not exist ".venv" (
    echo       Creating .venv for the first time...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        goto ERROR
    )
) else (
    echo       Existing .venv found.
)

:: Step 3: Activate the virtual environment and install/update dependencies
echo [3/5] Installing required libraries from requirements.txt...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] Failed to install Python dependencies from requirements.txt.
    goto ERROR
)
echo       All Python libraries are ready!

:: Step 4: Check Ollama (Local AI) and ensure the lightweight model is available
echo [4/5] Checking Ollama Local AI...
where ollama >nul 2>&1
if errorlevel 1 (
    echo       [INFO] Ollama not found. Attempting automatic install via winget...
    winget install --id Ollama.Ollama -e --silent --accept-package-agreements --accept-source-agreements >nul 2>&1
    if errorlevel 1 (
        echo       [WARN] Could not auto-install Ollama.
        echo              You can install it manually from https://ollama.com/download
        echo              Note: SmartDocSorter still works without Ollama using fast Python rules!
    ) else (
        echo       Ollama installed! Please restart this script if Ollama is not yet in PATH.
    )
) else (
    echo       Ollama found! Checking for qwen2.5:1.5b model...
    ollama list 2>nul | findstr /i "qwen2.5:1.5b" >nul
    if errorlevel 1 (
        echo       Downloading qwen2.5:1.5b model (first time only, ~1 GB)...
        ollama pull qwen2.5:1.5b
    ) else (
        echo       qwen2.5:1.5b model is ready!
    )
)

:: Step 5: Launch the SmartDocSorter Browser Interface (NiceGUI)
echo [5/5] Launching SmartDocSorter Browser GUI...
echo.
python run_browser_gui.py
goto END

:ERROR
echo.
echo [FAILED] Setup could not complete. See the message above.
pause
exit /b 1

:END
pause
