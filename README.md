# 🔌 Energia Doméstica — Tuya Local Dashboard

Sistema de **monitoramento e controle de energia residencial** com integração local a dispositivos Tuya (medidor de energia + disjuntor WiFi inteligente). Inclui dashboard web, controle automático de carregamento de veículo elétrico e histórico de sessões.

> 🏠 **100% local** — sem dependência de cloud Tuya, sem custos de API, sem enviar dados pessoais pra fora da sua rede.

---

## ✨ Funcionalidades

### Monitoramento
- 📊 **Dashboard em tempo real** — consumo (W), tensão (V), corrente (A), energia acumulada (kWh)
- 📈 **Gráficos** — potência em tempo real, energia diária, consumo horário
- ⚡ **Analisador da rede (aba Rede)** — registro de **apagões** (a placa reinicia junto com a queda — início = última leitura, fim = boot da placa), tempo até restabelecer, eventos de **tensão baixa/alta** fora da faixa ANEEL e histórico de qualidade por dia
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

## 🛡️ Guarda de energia no PC (sentinela de bateria)

O PC de bateria que fica 24/7 vira **sentinela da casa**: quando a rede cai, o carregador dele desliga (`ADP1/online → 0`) e a placa sai do ar — a guarda confirma em ~30s, **alerta na tela** (notify-send crítico) e por **Telegram** (best-effort: o roteador morre junto; o alerta de VOLTA sai confiável), **para serviços pesados** (padrão: `ollama.service`) e reduz a própria cadência para poupar bateria. Carregador fora com a placa no ar ≠ falta (alguém desplugou o PC) — alerta só na tela. Aviso de bateria < 20% com a queda aberta. A volta da energia alerta com a duração e religa os serviços.

Instalação no PC (rode com sudo — o serviço é do sistema):

```bash
TOKEN_PLACA=$(ssh root@192.168.18.133 'cat /etc/vigia/token')
sudo install -Dm644 /dev/null /etc/guarda/token && echo "$TOKEN_PLACA" | sudo tee /etc/guarda/token >/dev/null
sudo install -m755 deploy/guarda_energia.py /usr/local/bin/guarda_energia.py
sudo install -m644 deploy/guarda-energia.service /etc/systemd/system/guarda-energia.service
sudo systemctl daemon-reload && sudo systemctl enable --now guarda-energia
```

Teste sem instalar: `python3 deploy/guarda_energia.py --test` (simula uma queda: notifica na tela + Telegram rotulado 🧪, não toca em nada) e `--once` (classifica o momento). Config por env no `.service`: `GUARDA_UNITS` (lista a parar), `GUARDA_BOARD_URL`, `GUARDA_CHAT`. Estado em `/var/lib/guarda/estado.json` (a queda sobrevive a restart do guarda).

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
| `/api/hourly` | GET | Consumo hora a hora de um dia |
| `/api/outages` | GET | Apagões e eventos de tensão (resumo + lista, `?days=N`) |
| `/api/power_quality` | GET | Analisador de tensão: mín/média/máx e % por faixa ANEEL/dia — casa (127 V) e carro (`car`, 220 V) |
| `/api/refresh` | POST | Botão "Atualizar": re-lê os dois medidores e pede coleta imediata do BYD |
| `/api/car/start-charge` | POST | Liga disjuntor + inicia tracking |
| `/api/car/stop-charge` | POST | Desliga + finaliza sessão no DB |
| `/api/breaker/on` | POST | Liga disjuntor direto |
| `/api/breaker/off` | POST | Desliga disjuntor direto |
| `/api/breaker/prepay/on` | POST | Ativa modo prepayment |
| `/api/breaker/prepay/off` | POST | Desativa modo prepayment |

---

## 📱 App (PWA)

O dashboard é uma **PWA instalável**: ícone na tela inicial do celular, abre em tela cheia (sem barra de navegador) e funciona offline com o shell em cache (o service worker guarda o HTML, os ícones e o Chart.js/Leaflet do CDN; os dados `/api/*` sempre vêm da rede — dado fresco vence cache).

### Instalar no iPhone (funciona já, por HTTP)

1. Abra `http://<ip-da-placa>:8050` no Safari.
2. Botão **Compartilhar** → **Adicionar à Tela de Início**.
3. O app abre standalone (tela cheia, sem barra do Safari).

### Instalar no Android

1. Abra `http://<ip-da-placa>:8050` no Chrome.
2. Menu ⋮ → **Adicionar à tela inicial**.
3. O ícone abre o dashboard; o modo standalone completo (e o cache offline) exigem **HTTPS** — veja abaixo.

### HTTPS para o Android ficar completo (opcional)

Service worker e modo standalone do Android exigem contexto seguro. A placa já tem Tailscale — basta um comando:

```bash
# na placa (uma vez; https://<nome-da-placa>.<sua-rede>.ts.net passa a servir o 8050)
tailscale serve --bg 8050
```

Depois instale a partir de `https://<nome>.<rede>.ts.net` no celular (com o Tailscale do celular ligado). Sem HTTPS o PWA continua funcionando como acima — o service worker simplesmente não registra (o código tem guarda `isSecureContext` e fica inert; nada quebra).

### Como o cache funciona

- `/api/*` **nunca** é cacheado (dados sempre frescos); POSTs de controle (iniciar/parar carga, config) nunca são interceptados.
- Navegação é **network-first**: depois de um deploy, o shell novo chega no primeiro reload — não há versão presa em cache.
- Para forçar atualização completa no celular: reabra o app; se algo estranho persistir, limpar dados do site no navegador.

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
