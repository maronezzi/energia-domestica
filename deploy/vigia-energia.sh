#!/bin/sh
# Vigia do energia + DNS — restarta e alerta via Telegram quando caem.
# Nasceu de 28/10/2026: 3a queda da cubie2 em 5 dias; no boot diskless o
# OpenRC as vezes alcancar o energia antes do restore do apk (python3 atrasado)
# e o servico fica "failed to start" o dia inteiro sem ninguem perceber.
# Roda via cron */5. Debounce de alerta: 1x/6h por servico.
# LIMITACAO: se o DNS morrer, os alertas Telegram tambem falham (curl depende
# de DNS) — mas o restart automatico segue funcionando.
#
# Fonte da verdade: deploy/vigia-energia.sh NO REPO. A copia viva fica em
# /etc/vigia/vigia-energia.sh na cubie2 e e sincronizada a cada
# ./deploy_cubie.sh — nao edite a copia da placa direto.
#
# 31/10/2026: novo check de HEARTBEAT do poll loop. API HTTP de pé nao
# significa controle de pé — no incidente de 30/10 o uvicorn respondeu
# normal por 12h enquanto o poll loop morria em todo ciclo (7.656
# tentativas de sessao falhas, carga da noite fora do historico). O
# /api/status expoe poll_age_s (idade da ultima iteracao BEM-sucedida
# do poll loop); acima de 90s (~16 polls perdidos), restarta e alerta.

TOKEN=$(cat /etc/vigia/token)
CHAT="71802842"
STATE_DIR="/var/lib/vigia"
mkdir -p "$STATE_DIR"

send() {
    curl -s -m 10 -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHAT}" \
        --data-urlencode "text=$1" \
        --data-urlencode "parse_mode=Markdown" >/dev/null 2>&1
}

alerta() { # $1=slug $2=mensagem
    TSF="$STATE_DIR/vigia-energia-$1-ts"
    NOW=$(date +%s)
    LAST=$(cat "$TSF" 2>/dev/null || echo 0)
    if [ $((NOW - LAST)) -gt 21600 ]; then
        send "$2"
        echo "$NOW" > "$TSF"
    fi
}

# ── energia: API na 8050 respondendo? ──
if ! wget -q -O /dev/null --timeout=8 http://127.0.0.1:8050/api/status 2>/dev/null; then
    rc-service energia start >/dev/null 2>&1
    sleep 45  # carrega o DB de ~71MB antes de re-checar
    if ! wget -q -O /dev/null --timeout=8 http://127.0.0.1:8050/api/status 2>/dev/null; then
        alerta energia "🔴 *energia DOWN na cubie2* — watchdog tentou restart sem sucesso $(date '+%d/%m %H:%M')"
    else
        alerta energia "🟡 *energia caiu e foi reiniciado pelo watchdog* $(date '+%d/%m %H:%M')"
    fi
else
    # ── energia: API de pé, mas poll_loop travado? ──
    # poll_age_s vazio/None = serviço antigo sem heartbeat ou primeiro poll
    # ainda em curso — não julga (sem dado não há restart).
    _idade="$(wget -q -O- --timeout=8 http://127.0.0.1:8050/api/status 2>/dev/null | python3 -c "
import json, sys
try:
    v = json.load(sys.stdin).get('poll_age_s')
    print(v if v is not None else '')
except Exception:
    print('')
")"
    if [ -n "${_idade}" ] && [ "${_idade}" -gt 90 ] 2>/dev/null; then
        rc-service energia restart >/dev/null 2>&1
        alerta energia-poll "🟡 *energia: poll_loop travado (${_idade}s sem leitura bem-sucedida) — reiniciado pelo watchdog* $(date '+%d/%m %H:%M')"
    fi
fi

# ── DNS: AdGuardHome respondendo? (localhost = /etc/hosts, sem upstream) ──
if ! nslookup localhost 127.0.0.1 >/dev/null 2>&1; then
    rc-service adguardhome start >/dev/null 2>&1
    sleep 5
    if ! nslookup localhost 127.0.0.1 >/dev/null 2>&1; then
        alerta dns "🔴 *DNS morto na cubie2* — watchdog tentou restart sem sucesso $(date '+%d/%m %H:%M')"
    else
        alerta dns "🟡 *DNS caiu e foi reiniciado pelo watchdog* $(date '+%d/%m %H:%M')"
    fi
fi
