# Changelog

## [Unreleased] - 2026-06-02

### Added
- 🆕 **Aba "Carregamentos"** — histórico de cada sessão de carga com SOC inicial/final, kWh, custo e duração
- 🆕 **Tabela `charge_sessions`** no SQLite — persiste cada sessão com UUID, start/end times, energy delivered, total cost
- 🆕 **Endpoints**:
  - `GET /api/charge/sessions` — lista sessões (ativas + finalizadas)
  - `GET /api/charge/summary` — resumo agregado (períodos: 7d, 30d, 90d, 1 ano, tudo)
- 🆕 **Auto-shutdown inteligente** — só desliga disjuntor quando SOC efetivo ≥ meta E consumo zera por 120s
- 🆕 **SOC efetivo** calculado a partir de energia real entregue (não SOC declarado)
- 🆕 Cards de info do breaker: Fault, Temperatura, Corrente por fase
- 🆕 Endpoints pra controlar modo prepayment (`/api/breaker/prepay/on|off`)
- 🆕 Toggle "Auto-desligar" no dashboard

### Changed
- ✏️ **Bugfix**: disjuntor usava DPS 11 (prepay) em vez de DPS 16 (real switch) — agora liga/desliga corretamente
- ✏️ **Bugfix**: `no_balance_alarm` agora detectado via fault_code bitfield
- ✏️ **Bugfix**: dashboard tinha layout quebrado nos breaker cards (faltava CSS)
- ✏️ **Bugfix**: `phase_a` agora formatado como inteiro (mA)
- ✏️ Refatorado: credenciais agora em `data/devices.json` (não hardcoded)
- ✏️ Refatorado: BASE_DIR aponta pro raiz do projeto, DB em `data/`

### Migration from old tuya-dashboard
1. Copie `data/devices.json` com suas credenciais
2. O serviço antigo (`tuya-dashboard.service`) precisa ser desabilitado
3. Rode `./deploy/install.sh` pra instalar o novo `energia-domestica.service`
