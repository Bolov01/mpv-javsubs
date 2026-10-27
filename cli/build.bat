@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === 安装/升级 PyInstaller ===
python -m pip install --upgrade pyinstaller || goto :fail
echo.
echo === 打包单文件 exe ===
python -m PyInstaller --onefile --console --name javsubs-cli --clean --log-level WARN javsubs_cli.py || goto :fail
echo.
echo === 产物 ===
dir /b dist\javsubs-cli.exe
echo 完成。可直接运行：dist\javsubs-cli.exe --help
exit /b 0
:fail
echo 构建失败，请检查上方输出。
exit /b 1
