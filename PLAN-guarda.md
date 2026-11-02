# PLAN-guarda — Guarda de energia no PC (bateria como sentinel)

**Meta:** o PC (sempre ligado, com bateria) vira sentinela: detecta a falta de energia na casa, minimiza recursos dele (parar serviços pesados, ex.: ollama), alerta NA TELA se tiver usuário logado e por Telegram se tiver internet — e avisa a volta da energia com a duração.

**Fatos medidos (02/11/2026):**
- PC: BAT1 (98%) + ADP1 online=1 (mains); sinal de queda = `ADP1/online` → 0 (mais confiável que o status da bateria, que fica "Not charging" com carga retida).
- Placa: `http://192.168.18.133:8050/api/status` por IP (sem DNS — o vigia documenta que DNS morto mata o curl por nome).
- Sessão gráfica: bruno (1000) ativa — notify-send via `/run/user/1000/bus`.
- Telegram do vigia: token em `/etc/vigia/token` NA PLACA, chat 71802842 → copiar token para `/etc/guarda/token` no PC na instalação (ssh root@placa).
- sudo local exige senha → serviço systemd instalado pelo DONO (comandos prontos); o script roda e testa SEM root (unit-stop vira skip com log; notify/telegram funcionam como bruno).
- Pesado no PC: ollama.service (ativo). Minimização = lista configurável de units; padrão `["ollama.service"]`.

**Lógica (tick de 15s):**
- A = PC na bateria (`ADP1/online == 0`); B = placa inalcançável (2 checagens seguidas, 3s de timeout).
- A(2 ticks) + B(2 ticks) → **ENERGIA CAIU**: alerta tela + Telegram, PARA units, intervalo 15s→60s, abre queda (start no state file).
- A(2 ticks) sem B → "PC na bateria" (tomada do PC ou desplugado): alerta SÓ na tela, sem minimizar, sem Telegram.
- B sem A → log só (o vigia da placa cobre a saúde da placa).
- Recuperação (A sai OU B volta com queda aberta) → alerta de volta + duração + religa units + intervalo normal.
- Bateria < 20% com queda aberta → alerta "guarda vai morrer em ~XX min".

## Passos

1. **Núcleo puro + testes**: `deploy/guarda_energia.py` com funções de leitura (ac_online, battery_pct, board_up) e máquina de estados `GuardaState.on_tick()`; `tests/test_guarda.py` cobre debounce, os 4 estados, recuperação com duração e sobrevivência de restart (state file).
   - Check: `pytest tests/test_guarda.py` verde. ✅ **15 passed; suite 210 passed** (2026-11-02) — bug real achado no teste: reset do episódio "bateria-só" exigia tomada de volta por 2 ticks

2. **Efeitos + loop**: notify-send nas sessões gráficas (loginctl), Telegram best-effort (10s, token file/env), stop/start de units (skip gracioso sem root), arquivo de estado, main loop com intervalos e flags `--once`/`--test` (test: classifica como queda, alerta na tela + 1 Telegram rotulado 🧪, NÃO para units, NÃO grava estado).
   - Check: `pytest tests/test_guarda.py` (efeitos mockados) + `--once` real mostrando a classificação do momento. ✅ **--once: AC=ok, placa=ok, bateria 98%, evento=nada** (2026-11-02)

3. **Instalação**: `deploy/guarda-energia.service` (system, root) + fetch do token da placa + comandos prontos (sudo) para o dono; script testado ao vivo em modo usuário.
   - Check: `--test` dispara a notificação na tela do bruno + 1 Telegram 🧪. ✅ **ambos enviados (queda simulada, tela + Telegram)** (2026-11-02). PENDENTE do dono: os 3 comandos sudo de instalação → então `systemctl is-active guarda-energia`.

4. **Docs + memória + commit/push**: README (seção "Guarda de energia no PC"), CHANGELOG, memória.
   - Check: commit + push + docs consistentes.

## Riscos/decisões
- Internet morre junto com a rede elétrica (roteador sem no-break): Telegram vira best-effort — o alerta de QUEDA pode não sair; o de VOLTA sai (roteador já religado). Limitação documentada no vigia também.
- A-only ≠ falta confirmada: alguém pode ter desplugado o carregador do PC — alerta só na tela.
- Guarda nunca suspende o PC: ele É o sentinel.
