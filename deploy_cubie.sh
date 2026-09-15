#!/bin/bash
# deploy_cubie.sh — deploy do codigo do energia-domestica (PC → cubie2)
# O DB e configs (data/) vivem NA PLACA — este script NAO sobrescreve.
# Uso: ./deploy_cubie.sh [IP]
set -euo pipefail

IP="${1:-192.168.18.133}"
SRC="$(cd "$(dirname "$0")" && pwd)"
URL="http://100.124.147.12:8050"

echo "→ Deploy energia-domestica → cubie2 ($IP)"
rsync -az --no-perms --no-owner --no-group \
  --exclude='.git' --exclude='.gitignore' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='.qwen' --exclude='logs' --exclude='venv' --exclude='.venv' \
  --exclude='data/' \
  "$SRC/" "root@${IP}:/media/mmcblk0p1/energia/"

echo "→ Reiniciando serviço energia na placa..."
ssh "root@${IP}" "rc-service energia restart"

echo "✓ Deploy OK — ${URL}"
