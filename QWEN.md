# QWEN.md — Energia Doméstica

## Visão Geral

Sistema **100% local** de monitoramento e controle de energia residencial com integração a dispositivos Tuya via LAN (sem cloud). Inclui dashboard web em tempo real, controle de carregamento de veículo elétrico com auto-shutdown inteligente, e histórico persistido em SQLite.

### Stack

| Camada | Tecnologia |
|---|---|
| Backend | Python 3.10+ / FastAPI / uvicorn |
| Dispositivos | tinytuya (comunicação local LAN) |
| Storage | SQLite (`data/tuya_history.db`) |
| Frontend | HTML + vanilla JS + Chart.js 4.4 (CDN) — servido pelo próprio FastAPI |
| Deploy | systemd (`energia-domestica.service`) na porta 8050 |

### Arquitetura

- **Monolito single-file**: toda a lógica do backend vive em `src/dashboard.py` (~2300 linhas) — endpoints FastAPI, polling loop em thread daemon, state machine de carregamento, helpers de DB.
- **`ChargingTracker`** (classe em `dashboard.py`): máquina de estados (`idle → charging → completing → idle`) que calcula SOC efetivo a partir do delta de energia do disjuntor. Auto-stop quando a potência fica ociosa por N segundos (o fim é detectado por potência, funciona também sem meta de SOC). Ao finalizar volta sempre a `idle` — a auto-detecção no `poll_loop` só dispara a partir de `idle`.
- **Thread-safe**: `State` e `ChargingTracker` usam `threading.Lock`. O polling loop roda em thread daemon separada.
- **Frontend**: `src/index.html` — SPA vanilla JS com abas (Dashboard, Carregamentos, Config). Sem build step.
- **DB**: 3 tabelas — `readings`, `daily_snapshots`, `charge_sessions`. Migrations inline via `ALTER TABLE` em `init_db()`.

### Dispositivos Tuya

- **fase1**: medidor de energia principal (tensão, corrente, potência, energia acumulada)
- **breaker**: disjuntor WiFi inteligente (switch real no **DPS 16**, prepay no DPS 11, fault_code no DPS 9, saldo no DPS 13)

Credenciais em `data/devices.json` (gitignored). Template em `src/devices.example.json`.

## Comandos

### Rodar localmente

```bash
python3 src/dashboard.py
# Dashboard em http://localhost:8050
# Override: ENERGIA_HOST=0.0.0.0 ENERGIA_PORT=8050
```

### Testes

```bash
python3 -m pytest tests/ -v
```

Testes usam `unittest.TestCase` + `unittest.mock`. O arquivo `tests/test_charging_tracker.py` importa `dashboard` via `importlib` e redireciona `DB_FILE` para um tempdir.

### Lint

Não há config formal de linting (sem `pyproject.toml` / `.ruff.toml`). Há um `.ruff_cache`, indicando uso ad-hoc:

```bash
ruff check src/ tests/
```

### Deploy (systemd)

```bash
sudo ./deploy/install.sh
# Cria venv em .venv_energia/, instala deps, registra serviço systemd
sudo systemctl status energia-domestica
journalctl -u energia-domestica -f
```

## Convenções

- **Idioma**: comentários, docstrings, mensagens de log e UI em **português (BR)**. Nomes de código (variáveis, funções, classes) em inglês.
- **Estilo**: Python direto, sem type hints formais em toda parte. Seções separadas por comentários `# ─── Seção ───`.
- **DB**: SQLite com `sqlite3` puro (sem ORM). Conexões via `get_db()`. Migrations inline em `init_db()`.
- **Config**: `data/tuya_config.json` (gitignored) com defaults em `DEFAULT_CONFIG` no código.
- **Sem dependências extras**: não introduzir bibliotecas além das listadas em `requirements.txt` (fastapi, uvicorn, tinytuya, pydantic) sem necessidade clara.
- **Frontend**: vanilla JS inline no `index.html`. Sem framework, sem build step.
- **Git**: commits em inglês, prefixados com tipo (`fix:`, `feat:`). Mensagens concisas.

## Arquivos Importantes

| Arquivo | Papel |
|---|---|
| `src/dashboard.py` | Backend completo (FastAPI + polling + DB + charging tracker) |
| `src/index.html` | Frontend SPA (vanilla JS + Chart.js) |
| `src/devices.example.json` | Template de credenciais Tuya |
| `tests/test_charging_tracker.py` | Testes da state machine e helpers de DB |
| `deploy/install.sh` | Instalador (venv + systemd) |
| `docs/MITM_GUIDE.md` | Guia para capturar `local_key` dos devices Tuya |
| `scripts/check_db.py` | Script de inspeção do SQLite |
| `data/` | Runtime: DB, config, credenciais (tudo gitignored) |

## Cuidados

- **`data/devices.json`** contém credenciais — nunca commitar.
- **DPS 16** é o switch real do disjuntor. `tinytuya.turn_on()` default usa DPS 1, que **não** controla o relé.
- O polling loop conecta nos devices via LAN; se os devices estiverem offline, o dashboard sobe mas sem dados em tempo real.
- `DB_MAX_ROWS = 200000` — limite de linhas na tabela `readings` para evitar crescimento infinito.
