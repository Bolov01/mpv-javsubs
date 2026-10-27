#!/usr/bin/env sh
# PyInstaller 不能交叉编译：要 Windows exe 必须在 Windows 上跑（用 build.bat）
set -e
cd "$(dirname "$0")"
python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --console --name javsubs-cli --clean --log-level WARN javsubs_cli.py
ls -lh dist/javsubs-cli
