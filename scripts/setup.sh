#!/usr/bin/env bash
# macOS / Linux 下的等价安装脚本
set -euo pipefail
cd "$(dirname "$0")/.."

python3 --version
node --version
npx --version

python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env

npx -y playwright install chromium
python3 -m stagent doctor

echo "完成：python3 -m stagent run suites/smoke_login.yaml"
