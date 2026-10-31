#!/bin/bash
# deploy_cubie.sh — deploy do codigo do energia-domestica (PC → cubie2)
# O DB e configs (data/) vivem NA PLACA — este script NAO sobrescreve.
# Uso: ./deploy_cubie.sh [IP]
#      FORCE=1 ./deploy_cubie.sh   # força mesmo com carga em curso (evite)
# Também reinstala o vigia-energia (deploy/vigia-energia.sh → /etc/vigia/).
set -euo pipefail

IP="${1:-192.168.18.133}"
SRC="$(cd "$(dirname "$0")" && pwd)"
URL="http://100.124.147.12:8050"
FORCE="${FORCE:-0}"

# ── Guarda de carga (incidente 30/10/2026) ────────────────────────────
# O deploy reinicia o serviço; com sessão de carga ativa isso derruba o
# tracker no meio da carga — naquele dia o boot com sessão ativa
# envenenou o poll loop por 12h. NÃO reinicie com o carro carregando.
echo "→ Verificando estado da carga na placa ($IP)..."
check_charging() {
    ssh "root@${IP}" python3 - <<'PYEOF'
import json, urllib.request
try:
    st = json.load(urllib.request.urlopen("http://127.0.0.1:8050/api/charge/state", timeout=8))
    print("sim" if st.get("charging") else "nao")
except Exception:
    print("indecifravel")
PYEOF
}
CHARGING="$(check_charging 2>/dev/null || echo indecifravel)"

if [ "$CHARGING" = "sim" ] && [ "$FORCE" != "1" ]; then
    echo "✗ CARGA EM CURSO — reiniciar agora interrompe a sessão do carro."
    echo "  Conclua/aguarde a carga, ou rode FORCE=1 ./deploy_cubie.sh para forçar."
    exit 1
fi
if [ "$CHARGING" != "nao" ]; then
    echo "  ⚠️ Estado da carga indecifrável (API fora?) — seguindo mesmo assim"
fi

echo "→ Deploy energia-domestica → cubie2 ($IP)"
rsync -az --no-perms --no-owner --no-group \
  --exclude='.git' --exclude='.gitignore' \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  --exclude='.qwen' --exclude='logs' --exclude='venv' --exclude='.venv' \
  --exclude='data/' \
  "$SRC/" "root@${IP}:/media/mmcblk0p1/energia/"

echo "→ Reiniciando serviço energia na placa..."
ssh "root@${IP}" "rc-service energia restart"

# Vigia versionado no repo — sincroniza a cada deploy (idempotente).
if ssh "root@${IP}" "test -d /etc/vigia" 2>/dev/null; then
    echo "→ Sincronizando vigia-energia..."
    ssh "root@${IP}" "cp /media/mmcblk0p1/energia/deploy/vigia-energia.sh /etc/vigia/vigia-energia.sh && chmod +x /etc/vigia/vigia-energia.sh"
fi

echo "✓ Deploy OK — ${URL}"
