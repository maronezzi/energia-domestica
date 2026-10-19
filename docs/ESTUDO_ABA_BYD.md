# Estudo — Nova aba "Carro (BYD)" no dashboard via pybyd

*Estudo de viabilidade e desenho de solução — 18/10/2026. Não implementado; sujeito a decisão do dono.*

---

## 1. Objetivo

Adicionar uma aba no dashboard para acompanhar (e, opcionalmente, comandar) o carro BYD
usando a biblioteca **pybyd**, que fala com a mesma API de nuvem do aplicativo MY BYD.
Hoje o SOC do carro é 100% **modelo** (SOC = semente + Δenergia do disjuntor × η).
O pybyd seria a primeira **fonte real** de SOC, autonomia e estado de carga.

---

## 2. Veredito (TL;DR)

**Viável, com ressalvas administráveis.** Recomendo seguir em 3 fases:

| Fase | O que entrega | Esforço estimado |
|---|---|---|
| 0 — Validação | Script standalone na placa loga na BYD e despeja dados reais (confirma conta/endpoint/campos, inclusive GPS) | 1–2 h |
| 1 — Telemetria | Poller em background + tabelas `byd_snapshots`/`byd_positions` + aba "Carro" read-only (SOC real, autonomia, carga, posição ao vivo) + âncora de SOC no modelo | ½ a 1 dia |
| 2 — Diário de bordo | Viagens automáticas (km rodados + trajeto no mapa), geocerca de casa com partida/chegada (alimenta a regra da η), consumo diário (gráfico nativo BYD 7 dias + cálculo próprio + kWh carregados em casa, em R$) | ≈ 1 dia |
| 3 — Comandos (opcional) | Travar/destravar, ar-condicionado, find-car — atrás de PIN e flag desligada por default | ½ dia |

Principais riscos: API **não-oficial em alpha** (pode quebrar quando a BYD atualizar o app),
**rate limit/conta** (polling agressivo acorda o T-Box do carro a cada leitura), e
instalação de dependências na placa **armv7l** (resolvido via `apk`, ver §5).
Para o diário de bordo, a Fase 0 precisa confirmar também que **o GPS vem preenchido**
na conta BR — sem isso, viagens ficam limitadas a km (odômetro) sem trajeto.

---

## 3. O que é o pybyd

- **Repo:** [jkaberg/pyBYD](https://github.com/jkaberg/pyBYD) · **PyPI:** `pybyd` 0.0.79 (13/10/2026) · MIT · **Alpha**
- Cliente **async** (aiohttp) que converte a API do app MY BYD em modelos Pydantic tipados.
- Baseada em engenharia reversa (Niek/BYD-re, TA2k/ioBroker.byd) — como todo projeto desse tipo,
  pode quebrar silenciosamente se a BYD mudar a API. **Pinar versão exata** em `requirements.txt`.
- Requisitos: **Python ≥ 3.11**, `pydantic >=2.6,<3` (a placa tem 2.12.5 ✓).

### 3.1 Leitura de dados (o que a aba mostraria)

| Método | Dado |
|---|---|
| `get_vehicles()` | Lista de veículos da conta (VIN, modelo) |
| `get_vehicle_realtime(vin)` | **SOC** (`elec_percent`), **autonomia** (`endurance_mileage`), **odômetro**, velocidade, **estado de carga** (`effective_charging_state`: não carrega / carregando / conectado), **tempo até carga completa** (`time_to_full_minutes`), corrente de carga (`rate`), temp. interna, portas/janelas/travas, **pressão dos pneus**, consumo médio (kWh/100km), sentinela, timestamp do dado |
| `get_gps_info(vin)` | **Posição**: latitude, longitude, **velocidade**, **direção/heading** e `gps_timestamp` (data/hora do próprio fix de GPS — permite trajeto temporalmente correto mesmo se a coleta atrasar). Sem altitude |
| `get_charging_homepage(vin)` | Status + agenda de carga inteligente numa chamada |
| `get_energy_consumption(vin)` | **Consumo**: gráfico de 7 dias (kWh/100km **por dia** — base pronta do consumo diário), média de vida útil (kWh/100km + odômetro total) e janela dos últimos 50 km (taxa + kWh totais usados). ⚠️ A API **não tem consumo por viagem** — viagens são construídas localmente (§6.5) |

Propriedades de conveniência prontas: `is_charging`, `is_charger_connected`, `is_locked`,
`is_any_door_open`, `is_online`, `time_to_full_minutes`, `timestamp` (data real do dado —
essencial para mostrar "dado de X min atrás", pois o carro reporta quando acorda o T-Box).

### 3.2 Comandos (fase 2, opcional)

Exigem `control_pin` (PIN de 6 dígitos do app) + `verify_command_access(vin)` uma vez:

`lock/unlock`, `start_climate(21 °C, 10–30 min)/stop_climate`, `set_seat_climate`,
`set_battery_heat`, `flash_lights`, `find_car` (buzina+luzes), `open/close_windows`,
`open/close_trunk`, e `start_charging` (⚠️ **não existe `stop_charging`** — o comando é
no-op na API; parar carga continua sendo pelo disjuntor Tuya, que já fazemos).

Com MQTT habilitado o ACK do comando chega por push (com fallback HTTP). Para começar,
deixar MQTT **desligado** (`BYD_MQTT_ENABLED=0`) — polling HTTP basta e reduz complexidade.

### 3.3 Autenticação e sessão

`BydConfig(username, password, base_url, country_code, language, time_zone, control_pin, ...)`
com defaults sensatos. O cliente mantém sessão (~12 h, `session_ttl`) e re-loga sozinho
quando expira (códigos 1002/1005/1010). Basta manter **um** `BydClient` vivo no poller —
não criar cliente por request (cada login é uma chamada de API a mais).

---

## 4. Região Brasil — ponto de atenção

O default do pybyd é o servidor **europeu** (`dilinkappoversea-eu.byd.auto`, país `NL`).
Uma conta BYD brasileira provavelmente **não autentica lá**. O catálogo de regiões
derivado do `hass-byd-vehicle` (citado em Overdrive-release/BydCloudRegionCatalog) indica:

| Região | `base_url` | country_code | language |
|---|---|---|---|
| **Brasil** | `https://dilinkappoversea-br.byd.auto` | `BR` | `pt` |

⚠️ Essa URL é **inferida** por padrão de nomes (só `eu` e `no`/`kr` são explícitas nos
códigos que analisei). Por isso a **Fase 0 é obrigatória**: um script standalone que testa
login e dump de dados **antes** de qualquer integração, ajustando `BYD_BASE_URL`,
`BYD_COUNTRY_CODE` e `BYD_LANGUAGE` por variável de ambiente se o padrão não bater.
Caminho alternativo de descoberta: capturar o tráfego do app MY BYD (mesmo método do
`docs/MITM_GUIDE.md`, que já existe no repo para a Tuya).

---

## 5. A placa (cubie2) — achados e plano de instalação

Verificado via SSH em 18/10/2026:

| Item | Valor |
|---|---|
| Arquitetura | **armv7l** (32 bits — Cubieboard2/A20) |
| Alpine | 3.24.2 · Python **3.14.8** (pybyd pede ≥3.11 ✓) |
| Runtime do serviço | `/usr/bin/python3` direto, **sem venv** (site-packages do sistema; OpenRC `supervise-daemon`) |
| Instalados | fastapi 0.136.1, tinytuya 1.20.0 (via pip no sistema), pydantic **2.12.5** ✓ |
| Faltando para o pybyd | `aiohttp`, `cryptography`, `paho-mqtt` |

### Problema do armv7l

`aiohttp` e `cryptography` **não têm wheels musllinux para armv7l** — `pip install` tentaria
compilar (cryptography precisa de Rust: inviável na A20). Solução: **apk**, que tem os três
pré-compilados para armv7:

```sh
# na placa, como root
apk add py3-aiohttp py3-cryptography        # 3.13.5 ✓ (pybyd pede >=3.9,<4) e 47.0.0 ✓ (pede >=42)
pip install paho-mqtt                        # apk só tem 1.6.1, pybyd pede >=2.1 — PyPI é Python puro, instala fácil
pip install pybyd==0.0.79                    # aiohttp/cryptography já satisfeitos pelo apk, pip não recompila
```

Caveats:
- `py3-paho-mqtt` do apk (1.6.1) **não** satisfaz o pybyd — por isso o `pip install paho-mqtt` explícito.
- Atualizações futuras do Alpine/Python na placa exigem repetir o par apk+pip (o par
  tinytuya-pip + fastapi-apk já convive bem nesse esquema hoje).
- Disco: a partição já encheu uma vez (há vigia de disco); os três pacotes somam poucos MB —
  sem risco relevante.

---

## 6. Arquitetura proposta

Seguir os moldes existentes do projeto (coleta em thread daemon, `State` thread-safe,
config em `tuya_config.json`, segredos em JSON gitignored em `data/`).

```
┌──────────────────── src/byd_client.py (NOVO) ────────────────────┐
│  thread daemon própria com event loop asyncio próprio            │
│  (asyncio.new_event_loop + run_forever)                          │
│                                                                  │
│  loop adaptativo:                                                │
│    carro EM MOVIMENTO (speed > 0)  → 60 s realtime + GPS 1:2     │
│    carro carregando (is_charging OU breaker em carga) → 60 s     │
│    idle / carro parado                  → 15 min (jitter ±2 min) │
│    após comando remoto                  → leitura imediata       │
│    consumo BYD (gráfico 7 dias)         → 1× ao dia              │
│    erro de API/rede                     → backoff até 30 min     │
│                                                                  │
│  escreve em: State compartilhado (byd_*) + tabela byd_snapshots  │
└──────────────────────────────────────────────────────────────────┘
        │                                   │
        ▼                                   ▼
  GET /api/byd/*                    sqlite: byd_snapshots (NOVA)
        │                                   │
        ▼                                   ▼
  aba "Carro" (index.html)          gráfico SOC do dia / histórico
```

**Por que thread com loop próprio e não task no loop do uvicorn:** todo o app é
thread-based (`_reader_loop`, `State` com `threading.Lock`); uma thread própria isola
totalmente o pybyd (se a lib travar, o dashboard não sente) e não exige mexer no
ciclo de vida do FastAPI. A conexão SQLite já é `check_same_thread=False`
(`dashboard.py:105`), então a thread nova reusa o padrão com lock.

### 6.1 Credenciais e configuração

`data/byd.json` (gitignored, mesmo modelo de `devices.json`):

```json
{
  "username": "email@da.conta",
  "password": "senha do app MY BYD",
  "base_url": "https://dilinkappoversea-br.byd.auto",
  "country_code": "BR",
  "language": "pt",
  "time_zone": "America/Sao_Paulo",
  "control_pin": null,
  "enabled": true,
  "vin": null
}
```

Runtime/config de comportamento entra no `DEFAULT_CONFIG` de `dashboard.py`
(persistido em `tuya_config.json`): `byd_poll_idle_sec=900`, `byd_poll_charging_sec=60`,
`byd_commands_enabled=false` (**default desligado**), `byd_soc_anchor=true`.

### 6.2 Storage

Tabela nova (migration gateada por flag, como as existentes):

```sql
CREATE TABLE IF NOT EXISTS byd_snapshots (
  ts INTEGER PRIMARY KEY,          -- epoch UTC
  vin TEXT, soc REAL,              -- elec_percent
  range_km REAL, odo_km REAL,      -- endurance_mileage, total_mileage
  charging_state INTEGER,          -- 0 não carrega / 1 carregando / 15 conectado
  time_to_full_min INTEGER, rate_kw REAL,
  temp_in_car REAL, locked INTEGER,
  lat REAL, lon REAL, online INTEGER,
  data_age_sec INTEGER             -- ts_agora - timestamp reportado pelo carro
);
```

Volume irrisório: ~96 leituras/dia em idle, ~1440/dia durante carga — sem necessidade
de prune agressivo (30 dias brutos ≈ 200 KB).

Tabela de posições (base do diário de bordo, §6.5):

```sql
CREATE TABLE IF NOT EXISTS byd_positions (
  ts INTEGER PRIMARY KEY,          -- epoch UTC da COLETA
  gps_ts INTEGER,                  -- epoch do FIX de GPS (gps_timestamp)
  lat REAL, lon REAL,
  speed REAL, heading REAL,
  odo_km REAL                      -- odômetro lido no mesmo ciclo (realtime)
);
```

Volume: dirigindo com GPS a cada 2ª leitura de 60 s ≈ 30 pontos/hora de direção
(~1 km entre pontos a 60 km/h); parado, 1 ponto por ciclo idle. ~100 pontos/dia
típico — irrelevante para o disco.

### 6.3 Endpoints novos

| Endpoint | Retorna |
|---|---|
| `GET /api/byd/status` | Última leitura (SOC, autonomia, carga, portas, pneus, temp, GPS) + `data_age_sec` + estado do poller (ok/erro/última tentativa) — cache do `State`, nunca chama a BYD em tempo real |
| `GET /api/byd/history?from=&to=` | Série de `byd_snapshots` para gráfico (mesmo formato do `/api/hourly`) |
| `GET /api/byd/positions?date=dd/mm/aaaa` | Pontos de `byd_positions` do dia (para o mapa) |
| `GET /api/byd/trips?from=&to=` | Viagens derivadas (§6.5): km, duração, origem/destino, ΔSOC |
| `GET /api/byd/daily?days=7` | Resumo diário: km rodados, consumo BYD (kWh/100km), kWh calculados, kWh carregados em casa, R$/dia |
| `POST /api/byd/command` *(fase 2)* | `{"cmd": "lock|unlock|climate|find_car", "pin": "..."}` — gated por `byd_commands_enabled` |

Regra de ouro: **nenhum endpoint do dashboard chama a API da BYD de forma síncrona** —
tudo lê a última snapshot do `State`. O frontend nunca espera rede chinesa/europeia.

### 6.4 Integração com o SOC/η existente (respeitando a regra de 10/10)

A regra atual (commit 7140727) é: **η só re-aprende com partida confirmada pelo dono**.
O SOC real do pybyd **não muda isso**; ele alimenta o modelo de fora:

1. **Âncora de semente:** quando chega leitura fresca (< 30 min) e `byd_soc_anchor=true`,
   `car_current_soc` do modelo passa a ser semeado pelo SOC real nas transições
   (plug-in / início de carga) — exatamente a âncora que faltava para a η não derivar
   (caso 135: η derivou 0,937→0,76 sem âncora).
2. **Divergência visível:** se |SOC estimado − SOC real| > 5 %, a aba mostra os dois
   lado a lado; correção manual continua sendo decisão do dono no dashboard.
3. **η real por sessão (fase 3):** cruzar `charge_sessions` (energia do disjuntor) com
   ΔSOC real do BYD → η medida de verdade, ainda assim só gravada com partida confirmada.
4. Revisar `car_battery_kwh` (hoje 12,9) contra a capacidade real do modelo BYD
   (ou manter como janela útil, se for intencional).

### 6.5 Diário de bordo: viagens, geocerca e consumo diário

**Detecção de movimento.** O realtime traz `speed`; entre ciclos, Δodômetro.
Movimento = `speed > 0` (ou Δodo > 0,2 km) em duas leituras consecutivas.
Entrando em modo "dirigindo": realtime a cada 60 s, GPS a cada 2º ciclo
(ponto a cada ~1–2 km); saindo (speed = 0 por 2 ciclos e posição estável):
volta ao cadência idle. Enquanto o carro anda ele já está acordado — o custo
de "acordar o T-Box" é zero nessa janela. Volume total mantém-se modesto:
~96 chamadas idle + ~60–90/hora dirigindo.

**Viagens (trips).** A API BYD não tem conceito de viagem — derivamos localmente.
Janela de movimento = 1 viagem, fechada quando o carro para de vez. Cada viagem
grava (tabela `byd_trips`, derivada de `byd_positions` + snapshots):

- início/fim (hora), **km = Δodômetro** (confiável; GPS só contextualiza),
- origem/destino (lat/lon + reverse-geocode opcional — ver nota),
- SOC no início e no fim → **kWh da viagem = ΔSOC × `car_battery_kwh`**,
- duração, velocidade média/máxima (do `speed` coletado).

Na UI, a lista de viagens do dia vira a legenda do mapa (clique na viagem →
destaca o trecho no trajeto).

**Geocerca de casa.** Aprende onde o carro dorme: cluster das posições
entre 00h–06h (média móvel, atualiza semanalmente). **Partida** = primeira
saída do raio (~300 m — margem generosa para o ruído de GPS); **chegada** =
entrada estável no raio por 2 ciclos. Sinergia direta com a regra do dono
(10/10, commit 7140727): a partida detectada pela geocerca **é** a "partida
confirmada" que hoje é manual — passa a confirmar sozinha o aprendizado de η,
sem mudar a regra (a geocerca só confirma partidas físicas; η não aprende
com evento que não seja partida real). Com SOC na chegada + kWh do disjuntor
na carga seguinte, a η por ciclo vira medida de verdade.

**Consumo diário — três fontes, uma tela.**

1. **Nativo BYD:** `get_energy_consumption` 1× ao dia → série de 7 dias em
   kWh/100km, direto do carro (gráfico de barras pronto).
2. **Calculado localmente:** km/dia (Δodômetro) × kWh/dia (ΔSOC × bateria) →
   kWh/100km próprio, independente da BYD — validação cruzada e histórico
   ilimitado (o gráfico BYD só cobre 7 dias; snapshotando diariamente no
   `byd_snapshots`/rollup, guardamos o ano inteiro).
3. **Da casa:** kWh carregados em casa (tabela `charge_sessions` do disjuntor)
   por dia, cruzados com ΔSOC real → eficiência parede→bateria, e **R$/dia**
   com a tarifa do `tuya_config.json` — o "quanto custei hoje" que o cabeçalho
   atual só mostra para a casa.

**Mapa.** Leaflet + tiles OpenStreetMap via CDN (mesmo modelo do Chart.js, que
já é CDN). Camada do dia selecionado: polyline temporal, marcadores de
início/fim de cada viagem, cor opcional por velocidade. **Privacidade:** as
posições vivem só no SQLite da placa; ao navegador vão apenas sob request do
dono; para terceiros não sai nada além do que a BYD já sabe (e os tiles OSM
recebem apenas a área do mapa renderizada, não o histórico).

*Nota reverse-geocode:* transformar lat/lon em "Supermercado X" exige serviço
externo (Nominatim etc.) — opcional, fase posterior; sem ele, mostra-se
coordenada + distância de casa ("a 4,2 km de casa").

---

## 7. A aba no frontend

Padrão idêntico às abas existentes (`index.html:294-298`, painéis 302/547/594/661,
gancho em `showTab` 682-688):

- Botão **`Carro`** entre "Carregamentos" e "Config".
- `<div id="tab-byd" class="tab-content">`; gancho `if (n === 'byd') loadByd();`.
- Polling **só com a aba ativa** (molde do `loadCharges` 30 s, `index.html:1502-1504`):
  `setInterval` de 30 s que checa `classList.contains('active')`.
- Chart.js já carregado — reutilizar o padrão do gráfico "Hora a Hora".

Layout proposto (mobile-first, igual ao resto):

```
┌ Carro ──────────────────────────────────┐
│  ◉ 67%         autonomy: 231 km         │  ← anel/barra de SOC grande
│  ⚡ Carregando · cheia em 1h12 · 6,5 kW  │  ← estado de carga real + tempo
│  ─────────────────────────────────────  │
│  SOC real 67%  |  SOC estimado 65%  ✓   │  ← comparação com o modelo
│  Odômetro 12.431 km  ·  14,2 kWh/100km  │
│  🔒 Travado · portas ok · janelas ok     │
│  Pneus 2,4/2,4/2,3/2,4 bar              │
│  Temp. interna 24 °C · 📍 Em casa       │
│  [gráfico SOC do dia — Chart.js]        │
│  ── Diário de bordo (fase 2) ────────── │
│  Hoje: 34,2 km · 2 viagens · R$ 4,10    │
│  Consumo 7d: ▂▃▂▅▂▃▂ kWh/100km (BYD)    │
│  [mapa do dia: trajeto + início/fim]    │
│  dado de 2 min atrás · online ✓         │  ← data_age_sec SEMPRE visível
│  (fase 3: travar/destravar/clima/find)  │
└─────────────────────────────────────────┘
```

Detalhes que valem ouro no dia a dia (e que o modelo atual não tem): **"cheia em 1h12"**
real da BYD, saber se o carro **conectou** (is_charger_connected) antes da carga agendada,
alerta visual se `data_age_sec` crescer demais (carro dormiu/sinal fraco), e — com o
diário de bordo — **quantos km o carro andou hoje e o que isso custou**, sem planilha.

---

## 8. Segurança

| Risco | Mitigação |
|---|---|
| Senha da conta BYD na placa | Mesmo modelo atual das local keys Tuya: `data/byd.json` gitignored, placa é a superfície de ataque de hoje |
| Dashboard **sem auth** + comandos no carro | Comandos **desligados por default** (`byd_commands_enabled=false`); quando ligar, exigir PIN por POST e confirmação no UI; considerar auth básica no dashboard antes da fase 2 (o README já adverte sobre isso) |
| Uso da API não-oficial → risco à conta | Polling generoso (§6), nunca < 60 s; sem retry agressivo; `session_ttl` default (não forçar re-login) |
| PIN exposto | `control_pin` só em `data/byd.json`; nunca volta em `/api/byd/status` (redact) |

---

## 9. Riscos e mitigação (resumo)

1. **API alpha não-oficial** — pode quebrar a qualquer update do app. Mitigação: pinar
   `pybyd==0.0.79`, poller com try/except amplo que nunca derruba o dashboard, badge de
   "erro de coleta BYD há X h" na aba, e o modelo SOC atual continua funcionando sem o BYD
   (degradação graciosa — o sistema nunca fica *dependente* da nuvem BYD).
2. **Endpoint BR não confirmado** — Fase 0 existe exatamente para isso; MITM do app como plano B.
3. **armv7l / dependências** — resolvido com apk + pip (§5); documentar no CHANGELOG.
4. **Carro acordado demais** — cada `get_vehicle_realtime` acorda o T-Box. 15 min em idle é
   conservador e suficiente; durante carga o carro já está acordado, 60 s é seguro.
5. **Disco da partição** — volume de dados irrisório (§6.2); vigia de disco já existente cobre.

---

## 10. Plano de implementação

**Fase 0 — Validação (1–2 h).** `scripts/byd_probe.py` standalone (fora do dashboard):
lê `data/byd.json`, loga, imprime `get_vehicles`, `get_vehicle_realtime`, `get_gps_info`,
`get_charging_homepage`, `get_energy_consumption` de um VIN. Roda na placa (ambiente
idêntico ao serviço). **Saída esperada: confirmação de login BR, campos reais do carro
do dono e — crítico para o diário de bordo — se o GPS vem preenchido com speed/heading.**
Só depois disso a Fase 1 começa.

**Fase 1 — Telemetria + aba (½–1 dia).** `src/byd_client.py` (poller com os modos
dirigindo/carga/idle), migrations `byd_snapshots` + `byd_positions`, `/api/byd/status` +
`/api/byd/history`, aba "Carro" read-only com posição ao vivo, âncora de SOC (§6.4),
deploy via `deploy_cubie.sh` + apk/pip na placa (§5).

**Fase 2 — Diário de bordo (≈ 1 dia).** Detecção de movimento e tabela `byd_trips`;
geocerca de casa (partida/chegada automáticas → confirma o aprendizado de η);
`/api/byd/trips` + `/api/byd/daily` + `/api/byd/positions`; poll diário do consumo BYD
(7 dias) com rollup local; gráfico de consumo/km/R$ por dia; mapa Leaflet do trajeto do
dia. **Parcialmente independente do GPS:** sem GPS na conta BR, km e consumo diário saem
mesmo assim (odômetro + ΔSOC) — só o trajeto no mapa fica de fora.

**Fase 3 — Comandos (½ dia, opcional).** `byd_commands_enabled`, PIN, `verify_command_access`,
botões (travar/destravar, clima 10–30 min, find car), leitura imediata pós-comando.
Avaliar MQTT on para ACK rápido. Só depois de o dashboard ter auth básica.

**Fase 4 — Refinamentos (quando apetecer).** η real por sessão, alerta de desconexão
durante carga agendada, reverse-geocode dos destinos ("Supermercado X" em vez de
coordenada), replay do trajeto por viagem, histórico mensal de km/consumo, agendar
carga pela aba.

---

## 11. Itens em aberto (decisão do dono)

1. **Credenciais:** qual conta/e-mail é usado no app MY BYD e se o PIN de controle está definido.
2. **Fase 2 vale a pena?** Comandos são o que mais aumenta risco (conta + segurança). Se o
   objetivo é só monitorar, parar na Fase 1 é um pacote completo por si só.
3. **`car_battery_kwh` 12,9** — confirmar se é janela útil ou placeholder; com SOC real dá
   para calibrar de vez.
4. **Modelo do carro** (Dolphin/Seal/Song…?) — define se campos de híbrido (combustível)
   são relevantes e se `gps` está liberado na conta BR. Sem GPS, o diário de bordo perde
   só o trajeto no mapa: km (odômetro), viagens (janelas de movimento) e consumo diário
   continuam funcionando.
