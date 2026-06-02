# 🔌 Energia Doméstica — Tuya Local Dashboard

Sistema de **monitoramento e controle de energia residencial** com integração local a dispositivos Tuya (medidor de energia + disjuntor WiFi inteligente). Inclui dashboard web, controle automático de carregamento de veículo elétrico e histórico de sessões.

> 🏠 **100% local** — sem dependência de cloud Tuya, sem custos de API, sem enviar dados pessoais pra fora da sua rede.

---

## ✨ Funcionalidades

### Monitoramento
- 📊 **Dashboard em tempo real** — consumo (W), tensão (V), corrente (A), energia acumulada (kWh)
- 📈 **Gráficos** — potência em tempo real, energia diária, consumo horário
- 💰 **Custo calculado** — baseado em tarifa configurável (R$/kWh)
- 🌡️ **Status do disjuntor** — fault codes, temperatura, corrente por fase

### Carregamento de Veículo
- ⚡ **Controle ON/OFF** do disjuntor (que alimenta o carregador do carro)
- 🎯 **Projeção de tempo** — calcula tempo restante baseado em SOC atual vs meta
- 📊 **SOC efetivo** — calculado a partir de **energia real entregue** (não declarado)
- 🧠 **Auto-shutdown inteligente** — só desliga quando:
  1. SOC efetivo ≥ meta **E**
  2. Consumo no disjuntor zera por X segundos (carro parou de aceitar carga)
- 🔄 **Recálculo automático** — se a potência real for diferente do nominal, a projeção se corrige sozinha
- 📋 **Histórico de cada carregamento** — SOC inicial/final, kWh, custo, duração, status

### Disjuntor Tuya
- **DPS 16** = switch real (controla o relé)
- **DPS 11** = switch_prepayment (modo prepay - opcional)
- **DPS 13** = saldo (kWh, read-only)
- **DPS 9** = fault_code bitfield (65536 = `no_balance`)

---

## 🏗️ Arquitetura

```
energia-domestica/
├── src/
│   ├── dashboard.py             # App FastAPI principal
│   ├── index.html               # Frontend (vanilla JS + Chart.js)
│   ├── devices.example.json     # Template de credenciais Tuya
│   ├── logger.py                # Logger secundário
│   └── monitor.py               # Monitor alternativo
├── scripts/
│   └── check_db.py              # Inspecionar SQLite
├── deploy/
│   ├── install.sh               # Instalador
│   └── energia-domestica.service # Systemd unit
├── docs/
│   └── MITM_GUIDE.md            # Como capturar local_key do Tuya
├── data/                        # Gerado em runtime (gitignored)
│   ├── tuya_history.db
│   ├── tuya_config.json
│   └── devices.json             # Suas credenciais (gitignored!)
└── logs/
```

### Stack
- **Backend**: Python 3.10+ / FastAPI / uvicorn / tinytuya / SQLite
- **Frontend**: HTML + vanilla JS + Chart.js 4.4 (CDN)
- **Storage**: SQLite (history + charge_sessions + daily_snapshots)
- **Deploy**: systemd + Nginx (opcional)

---

## 🚀 Instalação

```bash
# 1. Clone
git clone https://github.com/maronezzi/energia-domestica.git
cd energia-domestica

# 2. Configure credenciais
cp src/devices.example.json data/devices.json
# edite data/devices.json com seus device IDs e local keys

# 3. Rode o instalador
chmod +x deploy/install.sh
./deploy/install.sh
```

O `install.sh` faz:
- Cria venv
- Instala dependências (`pip install -r requirements.txt`)
- Cria `data/` e `logs/`
- Instala o serviço systemd `energia-domestica`
- Inicia o serviço na porta 8050

Acesse em: **http://localhost:8050**

---

## ⚙️ Configuração

### Primeira vez
Edite `data/devices.json` com as credenciais dos seus devices Tuya:

```json
{
  "fase1": {
    "name": "Medição Fase 1",
    "id": "YOUR_FASE1_DEVICE_ID",
    "ip": "192.168.1.100",
    "key": "YOUR_LOCAL_KEY",
    "version": 3.4
  },
  "breaker": {
    "name": "Breaker WiFi",
    "id": "YOUR_BREAKER_DEVICE_ID",
    "ip": "192.168.1.101",
    "key": "YOUR_LOCAL_KEY",
    "version": 3.5
  }
}
```

> 🔑 Para descobrir o `local_key` de cada device, veja [`docs/MITM_GUIDE.md`](docs/MITM_GUIDE.md).

### Tarifa de energia
No dashboard, aba **Config**, ajuste `R$/kWh` (padrão: 0.956).

### Auto-shutdown
Por padrão, o sistema desliga o disjuntor automaticamente quando:
1. SOC efetivo ≥ meta
2. Consumo no disjuntor zera por **120 segundos** consecutivos

Configurável em `data/tuya_config.json`:
```json
{
  "car_charge_auto_stop": true,
  "car_charge_idle_power_w": 15,
  "car_charge_idle_seconds_to_stop": 120
}
```

---

## 📊 API

| Endpoint | Método | Descrição |
|---|---|---|
| `/api/status` | GET | Estado atual dos devices |
| `/api/today` | GET | Estatísticas do dia |
| `/api/charge/state` | GET | Estado da sessão de carga ativa |
| `/api/charge/sessions` | GET | Lista de sessões (ativas e finalizadas) |
| `/api/charge/summary` | GET | Resumo agregado (kWh, R$, duração) |
| `/api/car/start-charge` | POST | Liga disjuntor + inicia tracking |
| `/api/car/stop-charge` | POST | Desliga + finaliza sessão no DB |
| `/api/breaker/on` | POST | Liga disjuntor direto |
| `/api/breaker/off` | POST | Desliga disjuntor direto |
| `/api/breaker/prepay/on` | POST | Ativa modo prepayment |
| `/api/breaker/prepay/off` | POST | Desativa modo prepayment |

---

## 🐛 Troubleshooting

### "no_balance_alarm" ao iniciar carga
O disjuntor está em **modo prepayment** com saldo 0. Duas opções:
- Desativar prepay: `POST /api/breaker/prepay/off`
- Recarregar saldo pelo app Smart Life

### "Breaker não respondeu"
- Verifique se o device está na mesma rede (IP acessível)
- Confirme `local_key` e `version` (3.4 vs 3.5)

### Logs
```bash
journalctl -u energia-domestica -f
```

---

## 🛡️ Segurança

- **Tudo local** — devices Tuya comunicam só na sua LAN
- **Credenciais em gitignore** — `data/devices.json` nunca vai pro git
- **DB em gitignore** — histórico pessoal fica local

⚠️ **Não exponha a porta 8050 na internet sem autenticação!**

---

## 📜 Licença

MIT — use à vontade.

---

## 🙏 Créditos

- [tinytuya](https://github.com/jasonacox/tinytuya) — Python lib pra Tuya local
- [make-all/tuya-local#536](https://github.com/make-all/tuya-local/issues/536) — DP mapping reference
- Chart.js — gráficos
