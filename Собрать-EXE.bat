@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo Не найден Python 3. Установите с https://www.python.org/downloads/ и повторите.
  pause
  exit /b 1
)

if not exist ".venv-build\Scripts\python.exe" (
  echo Создаю окружение сборки...
  %PY% -m venv .venv-build
  if errorlevel 1 (
    echo Не удалось создать venv.
    pause
    exit /b 1
  )
)

call ".venv-build\Scripts\activate.bat"
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 (
  echo Не удалось установить зависимости.
  pause
  exit /b 1
)

echo.
echo === Собираю ControlBot.exe ===
pyinstaller --noconfirm --clean --distpath release --workpath build\tmp build\controlbot.spec
if errorlevel 1 goto :fail

echo.
echo === Собираю Helper24Sync.exe ===
pyinstaller --noconfirm --clean --distpath release --workpath build\tmp build\helper24_sync.spec
if errorlevel 1 goto :fail

echo.
echo Готово. Файлы:
dir /b release\*.exe
echo.
echo Дальше: положите .env рядом с ControlBot.exe и запустите его.
pause
exit /b 0

:fail
echo.
echo Сборка не удалась — смотрите сообщения выше.
pause
exit /b 1
