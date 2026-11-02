# PLAN — Tensão do carro (220 V) na aba Rede + botão "Atualizar dados" (rev. 2 pós-crítica)

**Meta:** na aba ⚡ Rede, dar à tensão do disjuntor do carro (220 V) o mesmo tratamento da casa (127 V) — card ao vivo e analisador diário por faixas ANEEL — e um botão "Atualizar" no topo que força coleta de tudo (medidores Tuya + BYD).

**Decisões da crítica (subagente, 8 achados):**
- SEM watcher ao vivo do carro e SEM coluna `outages.circuit`: com 21% de leituras válidas no DPS 6, eventos ao vivo do carro seriam inertes e a fusão/migration trazia 3 achados graves. Tabela de eventos continua referenciada à casa (apagões já são da casa). Nota na UI explica.
- `POST /api/refresh` NÃO limpa `_ttl_cache` (power_quality 300s = ~10s de CPU por clique na placa; single-flight recompute empilhado documentado). Agregados seguem seus TTLs.
- Sinalização do poller BYD: `asyncio.Event` + `loop.call_soon_threadsafe` (sem executor — threads não-daemon atrasariam o shutdown).
- Migration/circuito, fusão cross-circuito, eventos órfãos no startup: eliminados pela decisão 1.

## Passos

1. **`/api/power_quality` ganha seção `car`**: segunda agregação SQL (`phase_a > 50`, GROUP BY dia) com bandas 220 de `_ANEEL_ZONES`; config `grid_car_nominal_v: 220` (+ whitelist/range no POST /api/config). Teste com leitura 0.0 no meio (não pode sujar min/max).
   - Check: `pytest tests/test_rede.py::TestApis` verde com asserts novos do `car`. ✅ **21 passed** (2026-11-02)

2. **Frontend aba Rede**: card "🚗 Tensão do carro" (valor ao vivo de /api/status + zona 220); segundo gráfico `cVoltCar` (mín–máx diário, linhas ANEEL 202/231, tooltip com nº de amostras); nota explicando que eventos ao vivo ficam na casa.
   - Check: `node --check` + screenshot com uvicorn local (porta 8051) mostrando o gráfico do carro. ✅ **6 dias renderizados (211–227 V, banda azul, seed sintético na cópia local)** (2026-11-02)

3. **byd_client: coleta sob demanda** — `asyncio.Event` + `loop.call_soon_threadsafe` (loop capturado no `_run_loop`); `_sleep_or_force()` substitui os 2 sleeps interruptíveis (normal + erro); backoff de auth permanece cego; `request_poll_now()` público (False se poller morto).
   - Check: `pytest tests/test_byd_refresh.py` (força interrompe sleep de 30s em <5s; sem loop → False; poller morto → False). ✅ **5 passed** (2026-11-02)

4. **`POST /api/refresh` + botão no header** — re-lê os 2 medidores (padrão api_breaker_on), pede poll BYD, SEM limpar TTL (docstring explica); botão `🔄 Atualizar` no `.header-right` com ⏳/✅/❌ + recarga dos loaders ativos.
   - Check: `pytest tests/test_byd_refresh.py` (endpoint com leituras mockadas; request_poll_now chamado) + `node --check`. ✅ **7 passed; JS/py OK** (2026-11-02)

5. **Suite completa + docs + deploy + verificação ao vivo** — pytest 100%, CHANGELOG/README, deploy_cubie.sh, `curl /api/power_quality` (seção car na placa), clique no botão ao vivo (BYD `data_age_s` cai; loaders recarregam), screenshots.
   - Check: suite verde + evidências da placa + browser ao vivo.
