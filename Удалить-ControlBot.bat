@echo off
chcp 65001 >nul
echo Удаление ControlBot...
taskkill /IM ControlBot.exe /F >nul 2>&1
taskkill /IM Helper24Sync.exe /F >nul 2>&1
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ControlBot.lnk" >nul 2>&1
del "%~dp0controlbot_instance.lock" >nul 2>&1
echo.
echo Готово. Автозапуск снят, процессы остановлены.
echo Данные (activity_data.json, bot.log, .env) оставлены рядом с программой.
pause
