# ESTUDO — Dashboard de Previsão de Manutenção (BYD)

> **Data:** 28/10/2026 · **Status:** especificação pronta para implementação (fase 1)
> **Veículo:** BYD Song Pro DM-i (híbrido plug-in) · telemetria já coletada pelo poller (`byd_client.py`)
> **Núcleo de predição:** validado com cenários de teste (§11) · Python puro, sem dependências
> **REV. 2 (28/10/2026, dono):** (1) cálculo 1×/dia, não online; (2) previsão NUNCA bloqueada
> por registro — sem evento, os marcos são os múltiplos do intervalo no odômetro
> (29.393 km → próxima em 36.000, "caminha de 12.000 em 12.000"); o registro passa a
> ser correção fina/histórico e vence a grade quando existe. N_TRIALS 3000→1000 e cone
> sob demanda após medir 14,3 s na armv7.

---

## 0. TL;DR

O odômetro do carro **já está sendo coletado** (`byd_snapshots.odo_km`, a cada 60 s em
movimento / 15 min parado). Faltam três coisas: **âncora** (km da última manutenção,
registrada manualmente — nunca inferida), **modelo** de uso diário e **painel** na aba Carro.

O modelo escolhido é uma **série de uso diário zero-inflated lognormal com fator por dia
da semana + Monte Carlo** (3.000 simulações): cada simulação sorteia o nível de uso, os
fatores semanais e cada dia futuro, e a distribuição do dia em que o odômetro cruza
11.000/13.000 km pós-manutenção produz **de uma vez só** a data provável (P10/P50/P90),
as probabilidades de 7/15/30 dias e o risco de estouro. Sem Prophet, sem LSTM, sem numpy —
roda na cubie2 em ~0,5 s (armv7 tem talvez 3–5× esse tempo; com cache de 60 s, irrelevante).

**Suposições declaradas** (se alguma estiver errada, é só ajustar config):
1. Intervalo preventivo do DM-i = 12.000 km, tolerância ±1.000 km — ambos configuráveis em `data/byd.json`.
2. O odômetro nunca regride (nem manutenção zera odômetro) — rebaixadas são glitch/sentinela.
3. Histórico aproveitável começa em 18/10/2026 (deploy da fase 1 BYD) — as primeiras semanas
   rodam com confiança "baixa" por design honesto, melhorando sozinhas.
4. O alvo de integração é o dashboard existente (FastAPI + `index.html`, aba Carro) — não um
   app separado.

---

## 1. Resumo da solução

| Bloco | O que faz |
|---|---|
| **Ingestão** (já existe) | `byd_client.py` grava `odo_km` em `byd_snapshots` e `start_odo/end_odo` em `byd_trips` |
| **Série diária** (novo) | Limpeza (sentinela 0, rebaixadas, duplicadas) → 1 ponto/dia → km/dia com vãos espalhados |
| **Âncora** (novo) | Tabela `manutencao_eventos`, registro manual no painel. `km_desde_ultima = odo_atual − km_evento` |
| **Modelo** (novo) | Nível (média ponderada, meia-vida 21 d) × fator por dia da semana (shrinkage) × zeros (Laplace) × volatilidade (MAD de log-km) → Monte Carlo |
| **API** (novo) | `GET /api/byd/manutencao` (cache 60 s) + `POST /api/byd/manutencao/evento` |
| **Painel** (novo) | Card na aba Carro: status, barra do ciclo, data prevista com intervalo, probabilidades, cone de projeção no gráfico, histórico |
| **Aprendizado** | Recalcular é barato e sem estado: cada recálculo re-aprende do banco inteiro. Auditoria de acurácia em `manutencao_previsoes` |

Princípio central (lição da saga da âncora de SOC): **o sistema nunca inventa ponto de
partida**. Sem manutenção registrada, o painel mostra o convite para registrar — não uma
previsão chutada.

## 2. Arquitetura do sistema

```
byd_client.py (poller, já roda)
      │  grava
      ▼
SQLite data/tuya_history.db ──► manutencao_previsao.py (novo, puro)
      │   byd_snapshots (odo_km)      │  limpeza → modelo → Monte Carlo
      │   byd_trips (cross-check)     ▼
      │   manutencao_eventos ◄──── dashboard.py (FastAPI)
      │   manutencao_previsoes          │  GET /api/byd/manutencao  (cache 60 s)
      │                                 │  POST /api/byd/manutencao/evento
      ▼                                 ▼
  registro manual ◄─────────────  index.html · aba Carro (card novo)
                                    Chart.js: histórico + cone P10–P90
```

- **Mesmo banco** (`data/tuya_history.db`), mesmo processo FastAPI, mesma aba. Zero infra nova.
- O cálculo é **sem estado**: a cada chamada o módulo re-lê a série, re-ajusta e re-simula
  com semente fixa (`SEED_PADRAO = 20261028`) → determinístico (mesmo dado ⇒ mesma resposta)
  e imune a bugs de estado persistido.
- Cache em memória de 60 s no endpoint (a placa não precisa recalcular a cada refresh da aba).

## 3. Fluxo do enxame de agentes

Os nove agentes trabalharam em sequência com revisão cruzada; o que segue é a ata
consolidada — inclusive as falhas que o QA pegou e o Corretor consertou.

| # | Agente | Entrega / descoberta principal |
|---|---|---|
| 1 | **Requisitos** | Formalizou os 4 status com fronteira numérica (✅ <9.000 · ⚠️ 9.000–11.000 · 🟠 11.000–13.000 · 🚨 >13.000) e a **semântica exata das probabilidades**: "entrar na janela" = odômetro cruza `última + 11.000`; "estourar tolerância" = cruza `última + 13.000`. Critérios de aceite: determinismo, <2 s na placa, cold start honesto, sobreviver à sentinela `odo=0` |
| 2 | **Dados** | Fonte primária `byd_snapshots.odo_km` (cumulativo — robusto à cadência adaptativa do poller); `byd_trips` como cross-check. **Achou o bug pendente conhecido na prática**: `odo=0` é sentinela (carro dormindo) e há rebaixadas — a limpeza descarta ambas antes de qualquer diff |
| 3 | **Analista** | Padrão dominante é sazonalidade semanal (commute); zeros diários são **informação real** (carro parado), não faltantes; uso muda com férias/época → peso exponencial com meia-vida de 21 dias + detector de mudança (7 d vs 21 d anteriores) |
| 4 | **Modelagem** | Rejeitou Prophet/ARIMA/LSTM (§7) → **zero-inflated lognormal + fator por dia da semana + incerteza de parâmetro bayesiana + Monte Carlo**. Argumento: MC produz de uma vez quantis, P(7/15/30 d) e risco de estouro; modelos clássicos produziriam só a média |
| 5 | **Implementação** | Módulo puro `manutencao_previsao.py` (~380 linhas, stdlib), endpoint com cache, DDL. **Mediu: 466 ms por cálculo** em x86; margem de 4× para a placa |
| 6 | **UX** | Um card, não uma aba nova: 4 selos no status-bar (status · próximo marco · data P50 · confiança), barra do ciclo com marcas 11k/12k/13k, cone de projeção, chips de probabilidade, **linguagem sem jargão** ("intervalo provável", não "percentil") |
| 7 | **QA** | 27 cenários (§11). **Pegou 2 falhas reais do modelo**: confiança não caía em cold start de 5 dias nem com 20 dias parado. Outras 2 falhas eram do próprio teste (P(7 d)=0 é o resultado correto quando faltam 15 dias de uso) — spec corrigida, não o modelo |
| 8 | **Corretor** | Cap de confiança para n<10 dias (≤0,44 → "baixa"); penalidade **graduada** por fração de dias zerados (0,35/0,60); fix do ciclo estourado com **módulo** (carro com >2 ciclos atrasado não quebra); guard de série vazia |
| 9 | **Orquestrador** | Consolidou, definiu as 3 fases (§12) e os riscos remanescentes (§13) |

## 4. Schema de dados

Novas tabelas no mesmo `data/tuya_history.db` (padrão `CREATE TABLE IF NOT EXISTS` +
verificação de colunas já usado no projeto):

```sql
-- Âncora e histórico (entrada manual, confirmada pelo dono)
CREATE TABLE IF NOT EXISTS manutencao_eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,          -- data local da manutenção (YYYY-MM-DD)
    km REAL NOT NULL,            -- odômetro no momento da manutenção
    tipo TEXT NOT NULL DEFAULT 'preventiva',  -- preventiva|corretiva|revisao
    obs TEXT
);

-- Auditoria de previsões (1 linha/dia no máximo) → acurácia futura
CREATE TABLE IF NOT EXISTS manutencao_previsoes (
    ts INTEGER PRIMARY KEY,      -- epoch UTC do cálculo
    odo_km REAL, data_p50 TEXT, data_p10 TEXT, data_p90 TEXT,
    nivel_km_dia REAL, confianca REAL, status TEXT
);
```

**Config opcional** em `data/byd.json` (entra na validação do enxame já deployado —
faixa aceita: intervalo 3.000–30.000 km, tolerância 0–3.000):

```json
"manutencao": { "intervalo_km": 12000, "tolerancia_km": 1000 }
```

### Tratamento de dados (cada caso do enunciado)

| Caso | Tratamento |
|---|---|
| **Sentinela `odo=0` / rebaixada** (bug conhecido e pendente) | Descartados em `montar_serie_diaria()`: `odo<=0` ou `odo < último_válido×0,9`. Odômetro é monotônico por definição |
| **Várias leituras/dia** | Fica o MAIOR odo válido do dia (ponto cumulativo) |
| **Dias sem leitura** (carro dorme, guerra de sessão) | Δodo do vão espalhado igualmente pelos N dias: o km é real, só a atribuição horária é incerta — nenhum dia ganha pico falso |
| **Dias parados observados** | odo igual ao dia anterior ⇒ km=0 real (entra no modelo como zeros, não como buraco) |
| **Primeira inicialização** | n<3 dias ⇒ `historico_insuficiente` (status/marco absolutos aparecem já; probabilidades nascem com 3 dias, confiança "baixa") |
| **Sem última manutenção registrada** | **(rev. 2)** não bloqueia nada: âncora = grade de múltiplos do intervalo no odômetro (`ancora_de_odometro`: 29.393 → ciclo 24.000, marco 36.000). O registro manual continua disponível e vence a grade quando existe |
| **Inserção manual** | Só via `POST /api/byd/manutencao/evento` (km é validado: ≤ odo atual, > km do evento anterior) — não há outra porta de entrada |
| **Manutenção fora do intervalo** | O modelo não julga: a âncora nova simplesmente reinicia o ciclo; o histórico mostra que atrasou (🚨 ficará visível no retrospecto) |
| **Mudança brusca de padrão** | Detector 7 d vs 21 d ⇒ flag `mudanca_padrao` no JSON + badge no painel + confiança −0,20 |
| **Previsão de baixa confiança** | Score 0–1 com motivos legíveis no painel; intervalo P10–P90 já nasce largo porque a incerteza de parâmetro é sorteada em cada simulação |

## 5. Estratégia de previsão detalhada

**Passo 1 — série.** `byd_snapshots → [(dia, odo)]` limpos → `[(dia, km_dia)]` com vãos
espalhados. Janela de ajuste: últimos 180 dias.

**Passo 2 — ajuste (por que cada peça existe).**

- **Nível** `ln μ`: média de `ln(km_dia)` dos dias rodados com peso exponencial
  (meia-vida 21 d) — o dado de hoje vale 2× o de há 3 semanas. É o que faz a previsão
  "mover" quando o padrão muda (férias, novo trabalho).
- **Fator por dia da semana** `w_d`: multiplicativo, com **shrinkage bayesiano**
  (pseudo-contagem de 2 dias em direção a 1,0) — segunda-feira com 1 observação não
  domina a previsão.
- **Zero-inflação** `p0`: fração de dias parados, estimador de Laplace `(z+1)/(n+2)` —
  um carro que fica 3 dias parado por semana não pode ser modelado por gaussiana.
- **Volatilidade** `σ`: MAD×1,4826 dos residuais de log — robusto a um dia de viagem
  incomum; piso 0,35 (não fingir precisão), teto 1,5.
- **Incerteza de parâmetro** `σ_μ = σ/√n_eff` (clamp 0,10–0,70): a incerteza sobre o
  *nível* cai à medida que os dados chegam — é ela que faz o intervalo encolher com o
  tempo. É o ingrediente "bayesiano" sem biblioteca nenhuma.

**Passo 3 — Monte Carlo (3.000 simulações, semente fixa).** Cada simulação:
1. sorteia `ln μ ~ N(ln μ̂, σ_μ)` e os 7 fatores `w_d` (incerteza de parâmetro);
2. caminha dia a dia: com prob. `p0` o dia rende 0, senão
   `km_d ~ LogNormal(ln μ + ln w_d − σ²/2, σ)` (média diária = μ·w_d exata);
3. soma sobre `km_desde_ultima` e registra em que dia cruza 11.000 e 13.000.

**Passo 4 — saídas.** A distribuição dos dias de cruzamento responde tudo:
P10/P50/P90 → datas; fração com cruzamento ≤7/15/30 → probabilidades pedidas;
cruzamento de 13.000 ≤30 d → risco de estouro. Confiança = score com motivos
(§4, última linha).

## 6. Modelo escolhido

**Série de uso diária zero-inflated lognormal (nível × sazonalidade semanal × volatilidade)
com incerteza de parâmetro, projetada por Monte Carlo.**

Nomes consagrados mais próximos: é um **modelo de estado bayesiano simplificado**
(nível com incerteza encolhendo = filtro de Kalman do nível, em espaço log) combinado com
**simulação de trajetórias** (o que Prophet faria internamente com `uncertainty_samples`,
mas sem a dependência e com a sazonalidade certa para o problema — semanal, não anual).

## 7. Justificativa da escolha

| Alternativa | Veredito | Motivo |
|---|---|---|
| **Prophet** | ❌ | Não instala na cubie2 (armv7, sem roda pystan/cmdstan); exige ~1 ano para sazonalidade anual, que aqui não importa; pesado para rodar a cada dia |
| **ARIMA/SARIMA** | ❌ | Modela a *série* de km/dia como estacionária — mas o objetivo é cruzamento de limiar acumulado; precisaria de pipeline extra e ainda daria só ponto+IC assintótico, sem as probabilidades pedidas |
| **LSTM/RNN** | ❌ | Ridículo para <60 observações; sem PyTorch/TF na placa; caixa-preta impossível de auditar num domínio onde o dono quer entender o número |
| **Regressão linear simples** | ⚠️ (é o fallback do fallback) | Ignora zeros, sazonalidade e heterocedasticidade; CI fake |
| **Filtro de Kalman puro** | ⚠️ | Resolve o nível, mas gaussiano: km/dia tem massa em 0 (zero-inflação) e cauda direita — o Kalman subestimaria o dia em que se roda 300 km |
| **Escolhido: zero-inflated lognormal + MC** | ✅ | (1) monta-se em ~100 linhas de stdlib; (2) cada requisito de saída cai pronto da distribuição de cruzamento; (3) parâmetros audíveis e interpretáveis (μ, w_d, p0, σ); (4) incerteza honesta: encolhe com dados, alarga em mudança de padrão |

## 8. Código funcional (validado — 27/27 cenários)

O módulo completo, exatamente como testado em §11, é o bloco a seguir. Na fase 1 vira
`src/manutencao_previsao.py` (sem uma linha alterada).
```python
"""
Previsão de manutenção do BYD — núcleo de modelagem.
Especificação completa em docs/ESTUDO_PREVISAO_MANUTENCAO.md.

Modelo: série diária de uso (zero-inflated lognormal com fator por dia da
semana) + Monte Carlo da data em que o odômetro cruza os marcos.
Python puro (sem numpy) — roda na cubie2 armv7.
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

INTERVALO_PADRAO = 12_000.0   # km entre manutenções preventivas
TOLERANCIA_PADRAO = 1_000.0   # janela ideal = [intervalo - tol, intervalo + tol]

N_TRIALS = 1000               # simulações Monte Carlo por cálculo
                              # (calibrado na armv7: 3.000 dava ~10 s; 1.000
                              # mantém quantis estáveis a ±1.6%)
SEED_PADRAO = 20261028        # semente fixa: mesmo dado => mesma previsão

# A placa roda em UTC; o dia local do sistema é BRT fixo (padrão do repo,
# ver _brt_today() no dashboard.py)
_BRT = timezone(timedelta(hours=-3))


# ----------------------------------------------------------------------
# 1) Limpeza e série diária
# ----------------------------------------------------------------------

def montar_serie_diaria(snaps):
    """Converte snapshots brutos de odômetro em [(date, odo_km)] confiável.

    snaps: iterável de (ts, odo_km) — ts epoch UTC, qualquer ordem.
    Regras (enxame Dados/QA):
      - odo None/negativo/0 => sentinela conhecida do poller (carro dormindo),
        descartado;
      - rebaixada de odômetro (>10% abaixo do último válido) => glitch,
        descartado (odômetro não regride: nem manutenção zera odômetro);
      - 1 ponto por dia local: o MAIOR odo válido do dia.
    """
    por_dia = {}
    ultimo_valido = None
    ordenado = sorted((s for s in snaps if s[0] is not None), key=lambda s: s[0])
    for ts, odo in ordenado:
        if odo is None:
            continue
        odo = float(odo)
        if odo <= 0:
            continue  # sentinela 0 (carro dormindo)
        if ultimo_valido is not None and odo < ultimo_valido * 0.9:
            continue  # glitch: rebaixada além de ruído
        ultimo_valido = odo
        dia = datetime.fromtimestamp(ts, tz=_BRT).date()  # dia BRT fixo, como _brt_today()
        if odo > por_dia.get(dia, float("-inf")):
            por_dia[dia] = odo
    return sorted(por_dia.items())


def serie_km_por_dia(pontos):
    """[(date, odo)] => [(date, km_dia)] com vãos espalhados.

    Vão de N dias com Δodo vira N dias com Δodo/N (o km é real; só a
    atribuição horária é incerta). Dias observados diretamente com odo
    idêntico ao dia anterior viram 0 (carro parado — informação real).
    """
    if len(pontos) < 2:
        return []
    saida = []
    for (d0, o0), (d1, o1) in zip(pontos, pontos[1:]):
        n_dias = (d1 - d0).days
        delta = max(0.0, o1 - o0)  # odo é monotônico após limpeza
        per_day = delta / n_dias if n_dias > 0 else delta
        for i in range(n_dias):
            saida.append((d0 + timedelta(days=i + 1), per_day))
    return saida


# ----------------------------------------------------------------------
# 2) Ajuste do modelo (nível, sazonalidade semanal, volatilidade)
# ----------------------------------------------------------------------

_MEIA_VIDA_DIAS = 21.0  # decaimento do peso: dado de hoje vale 2x o de há 21 dias


def ajustar_modelo(serie):
    """[(date, km_dia)] => parâmetros do modelo.

    - nivel ln_mu: média ponderada exponencialmente (meia-vida 21 d)
    - fator por dia da semana: multiplicativo com shrinkage bayesiano
      (pseudo-contagem), multiplica o nível
    - p0: probabilidade de dia zerado (Laplace)
    - sigma: dispersão de log-km nos dias rodados (MAD, robusto a viagem rara)
    """
    hoje = serie[-1][0]
    alpha = math.log(2.0) / _MEIA_VIDA_DIAS

    ln_mu_num = 0.0
    ln_mu_den = 0.0
    n_obs = 0
    for dia, km in serie:
        idade = (hoje - dia).days
        peso = math.exp(-alpha * idade)
        if km > 0:
            ln_mu_num += peso * math.log(km)
            ln_mu_den += peso
            n_obs += 1
    if ln_mu_den <= 0:
        return None  # nunca rodou: nada a prever
    ln_mu = ln_mu_num / ln_mu_den

    # tamanho de amostra efetivo dos pesos (para incerteza do nível)
    n_eff = (ln_mu_den ** 2) / max(1e-9, sum(math.exp(-2 * alpha * (hoje - d).days)
                                             for d, km in serie if km > 0))

    # fatores por dia da semana (dias rodados), shrinkage para 1.0
    soma_dow = [0.0] * 7
    n_dow = [0] * 7
    residuos = []
    for dia, km in serie:
        if km > 0:
            w_hat = math.exp(math.log(km) - ln_mu)
            soma_dow[dia.weekday()] += w_hat
            n_dow[dia.weekday()] += 1
    fatores = []
    for d in range(7):
        n_d = n_dow[d]
        # pseudo-contagem de 2 dias: com pouco dado, fator encosta em 1.0
        fatores.append((2.0 * 1.0 + soma_dow[d]) / (2.0 + n_d))

    # volatilidade: residuais de log nos dias rodados (com fator aplicado)
    for dia, km in serie:
        if km > 0:
            residuos.append(math.log(km) - (ln_mu + math.log(fatores[dia.weekday()])))
    sigma = _mad_sigma(residuos)
    sigma = min(1.5, max(0.35, sigma))

    dias_zero = sum(1 for _, km in serie if km <= 0)
    p0 = (dias_zero + 1.0) / (len(serie) + 2.0)  # Laplace
    sigma_p0 = math.sqrt(max(p0 * (1 - p0), 0.01) / max(1.0, len(serie)))

    # incerteza do nível: encolhe com n_eff; piso alto em cold start
    sigma_ln_mu = min(0.7, max(0.10, sigma / math.sqrt(max(n_eff, 1.0))))

    # detecção de mudança de padrão: média 7d vs média anterior ponderada
    mudanca = _mudanca_padrao(serie, ln_mu, fatores)

    return {
        "ln_mu": ln_mu,
        "sigma_ln_mu": sigma_ln_mu,
        "fatores": fatores,
        "n_dow": n_dow,
        "sigma": sigma,
        "p0": p0,
        "sigma_p0": sigma_p0,
        "n_obs": n_obs,
        "n_total": len(serie),
        "mudanca_padrao": mudanca,
        "ultimo_dia_obs": hoje,
    }


def _mad_sigma(vals):
    if len(vals) < 4:
        return 0.8  # pouco dado: assume dispersão alta
    mediana = sorted(vals)[len(vals) // 2]
    desv = sorted(abs(v - mediana) for v in vals)
    mad = desv[len(desv) // 2]
    return 1.4826 * mad


def _mudanca_padrao(serie, ln_mu, fatores):
    """True se a média dos últimos 7 dias difere >50% da média anterior."""
    if len(serie) < 14:
        return False
    ult = [km for _, km in serie[-7:]]
    ant = [km for _, km in serie[-28:-7]]
    m_ult = sum(ult) / len(ult)
    m_ant = sum(ant) / len(ant) if ant else 0.0
    if m_ant < 1.0:
        return m_ult > 5.0
    return abs(m_ult - m_ant) / max(m_ant, 1.0) > 0.5


# ----------------------------------------------------------------------
# 3) Monte Carlo: distribuição da data de cruzamento dos marcos
# ----------------------------------------------------------------------

def simular(mdl, km_desde_ultima, intervalo_km, tolerancia_km, rng,
            n_trials=N_TRIALS):
    """Retorna distribuição de dias até cruzar [janela, limite].

    Cada trial sorteia o nível (incerteza de parâmetro), os fatores semanais
    e cada dia futuro (ruído de observação). O odômetro projetado soma sobre
    km_desde_ultima.
    """
    alvo_janela = intervalo_km - tolerancia_km
    alvo_limite = intervalo_km + tolerancia_km
    if km_desde_ultima >= alvo_limite:
        # já estourado: projeta a janela do PRÓXIMO ciclo (módulo cuida de
        # quantos ciclos inteiros foram pulados)
        offset = km_desde_ultima % intervalo_km
        km_desde_ultima = offset if offset < alvo_limite else offset - intervalo_km

    dist_janela = max(1.0, alvo_janela - km_desde_ultima)
    dist_limite = max(1.0, alvo_limite - km_desde_ultima)
    media_dia = math.exp(mdl["ln_mu"])
    horizonte = min(500, int(3 * max(dist_limite, dist_janela) / max(media_dia, 1.0)) + 30)

    res_janela = []
    res_limite = []
    km_30d = []
    sigma = mdl["sigma"]
    for _ in range(n_trials):
        ln_mu = rng.gauss(mdl["ln_mu"], mdl["sigma_ln_mu"])
        # log dos fatores 1x por trial (log() por dia custa caro na armv7)
        ln_fat = [
            math.log(max(0.05, rng.gauss(w, 0.15 + 0.5 / max(1, n))))
            for w, n in zip(mdl["fatores"], mdl["n_dow"])
        ]
        p0 = min(0.95, max(0.0, rng.gauss(mdl["p0"], mdl["sigma_p0"])))
        adj = -0.5 * sigma * sigma

        cum = km_desde_ultima
        t_j = None
        t_l = None
        for i in range(1, horizonte + 1):
            dia = mdl["ultimo_dia_obs"] + timedelta(days=i)
            if rng.random() < p0:
                km = 0.0
            else:
                km = rng.lognormvariate(ln_mu + ln_fat[dia.weekday()] + adj, sigma)
            cum += km
            if t_j is None and cum >= alvo_janela:
                t_j = i
            if t_l is None and cum >= alvo_limite:
                t_l = i
                break
        if t_j is None:
            t_j = horizonte + 1
        if t_l is None:
            t_l = horizonte + 1
        res_janela.append(t_j)
        res_limite.append(t_l)

    return {
        "horizonte": horizonte,
        "dias_janela": sorted(res_janela),
        "dias_limite": sorted(res_limite),
    }


# ----------------------------------------------------------------------
# 4) Saída consolidada (o que a API devolve)
# ----------------------------------------------------------------------

def _quantil_dias(dias_ordenados, q, horizonte):
    idx = min(len(dias_ordenados) - 1, int(q * len(dias_ordenados)))
    d = dias_ordenados[idx]
    return None if d > horizonte else d


def _p_ate(dias_ordenados, nd):
    return sum(1 for d in dias_ordenados if d <= nd) / len(dias_ordenados)


def _confianca(mdl, dias_sem_obs):
    """Nível de confiança 0..1 com motivos legíveis (exibido no painel)."""
    motivos = []
    score = 1.0
    n = mdl["n_total"]
    if n < 10:
        # <1 semana e meia: fatores semanais praticamente não aprendidos
        score = min(score, 0.44)
        motivos.append(f"pouco histórico ({n} dias)")
    if dias_sem_obs >= 3:
        score -= 0.25
        motivos.append(f"último dado há {dias_sem_obs} dias")
    cv = mdl["sigma"]
    if cv > 1.0:
        score -= 0.2
        motivos.append("uso muito irregular")
    frac_zero = mdl["p0"]
    if frac_zero > 0.35:
        score -= 0.15
        motivos.append("muitos dias parado")
    if frac_zero > 0.6:
        score -= 0.1
        motivos.append("maior parte do tempo sem uso")
    if mdl["mudanca_padrao"]:
        score -= 0.2
        motivos.append("padrão de uso mudou recentemente")
    score = max(0.05, min(1.0, score))
    nivel = "alta" if score >= 0.7 else ("media" if score >= 0.45 else "baixa")
    return {"score": round(score, 2), "nivel": nivel, "motivos": motivos}


def ancora_de_odometro(odo_atual, intervalo_km=INTERVALO_PADRAO):
    """Âncora sem registro: o ciclo é a grade de múltiplos do intervalo no
    próprio odômetro. Ex. do dono: odo 29.393 → ciclo começou em 24.000,
    km_desde = 5.393, próximo marco 36.000 ('sempre caminha de 12.000 em
    12.000'). Nesse modo o status nunca vira estourado: a grade avança sola.
    """
    if odo_atual is None or odo_atual <= 0:
        return None
    return odo_atual - math.floor(odo_atual / intervalo_km) * intervalo_km


def prever(serie_km, km_desde_ultima, intervalo_km=INTERVALO_PADRAO,
           tolerancia_km=TOLERANCIA_PADRAO, hoje=None, seed=SEED_PADRAO,
           com_projecao=True, odo_atual=None):
    """Ponto de entrada. serie_km: [(date, km_dia)]. Devolve dict da API.

    com_projecao=False pula o cone do gráfico (fase 1 não tem gráfico; o
    cone é ~1/4 do custo e só faz sentido com o painel visual da fase 2).
    odo_atual (quando informado) habilita os marcos ABSOLUTOS no payload
    (proximo_marco_km = ex. 36.000) — a grade que o dono enxerga.

    Status e marcos são matemática instantânea e voltam SEMPRE que há
    âncora, mesmo com histórico curto; só a parte probabilística exige
    série (min. 3 dias) — o painel mostra o estado, não inventa número.
    """
    hoje_dia = serie_km[-1][0] if serie_km else date.today()
    out = {
        "hoje": hoje_dia.isoformat(),
        "intervalo_km": intervalo_km,
        "tolerancia_km": tolerancia_km,
        "n_dias_obs": len(serie_km),
        "media_km_dia": round(sum(km for _, km in serie_km) / max(1, len(serie_km)), 1),
        "previsao": None,
        "motivo_sem_previsao": None,
    }
    if km_desde_ultima is None:
        out["motivo_sem_previsao"] = "sem_ancora"
        return out
    hoje = hoje_dia

    # matemática instantânea: status e posição no ciclo (independe da série)
    status, km_resta_j, km_resta_l = _status(km_desde_ultima, intervalo_km, tolerancia_km)
    out.update({
        "km_desde_ultima": round(km_desde_ultima, 1),
        "status": status,
        "janela_inicio_km": intervalo_km - tolerancia_km,
        "janela_fim_km": intervalo_km + tolerancia_km,
        "km_restantes_janela": round(max(0.0, km_resta_j), 1),
        "km_restantes_limite": round(max(0.0, km_resta_l), 1),
    })
    if odo_atual is not None:
        base = odo_atual - km_desde_ultima  # início do ciclo (registro ou múltiplo)
        marco = base + intervalo_km
        while odo_atual >= marco:           # ciclos pulados (modo registro atrasado)
            marco += intervalo_km
        out["proximo_marco_km"] = round(marco, 1)
        out["janela_inicio_abs"] = round(marco - tolerancia_km, 1)
        out["janela_fim_abs"] = round(marco + tolerancia_km, 1)

    if len(serie_km) < 3:
        out["motivo_sem_previsao"] = f"historico_insuficiente ({len(serie_km)} dias; minimo 3)"
        return out

    mdl = ajustar_modelo(serie_km)
    if mdl is None:
        out["motivo_sem_previsao"] = "nenhum dia com km rodados"
        return out

    dias_sem_obs = (hoje - mdl["ultimo_dia_obs"]).days
    rng = random.Random(seed)
    sim = simular(mdl, km_desde_ultima, intervalo_km, tolerancia_km, rng)

    d10 = _quantil_dias(sim["dias_janela"], 0.10, sim["horizonte"])
    d50 = _quantil_dias(sim["dias_janela"], 0.50, sim["horizonte"])
    d90 = _quantil_dias(sim["dias_janela"], 0.90, sim["horizonte"])

    def data_de(d):
        return (hoje + timedelta(days=d)).isoformat() if d is not None else None

    out.update({
        "nivel_km_dia_recente": round(math.exp(mdl["ln_mu"]), 1),
        "mudanca_padrao": mdl["mudanca_padrao"],
        "confianca": _confianca(mdl, dias_sem_obs),
        "previsao": {
            "dias_p10": d10, "dias_p50": d50, "dias_p90": d90,
            "data_p10": data_de(d10), "data_p50": data_de(d50), "data_p90": data_de(d90),
            "p_janela_7d": round(_p_ate(sim["dias_janela"], 7), 3),
            "p_janela_15d": round(_p_ate(sim["dias_janela"], 15), 3),
            "p_janela_30d": round(_p_ate(sim["dias_janela"], 30), 3),
            "p_estouro_30d": round(_p_ate(sim["dias_limite"], 30), 3),
        },
    })
    if com_projecao:
        out["projecao"] = _projecao_para_grafico(mdl, km_desde_ultima,
                                                 intervalo_km, tolerancia_km,
                                                 hoje, rng)
    return out


def _status(km_desde, intervalo_km, tolerancia_km):
    ini = intervalo_km - tolerancia_km
    fim = intervalo_km + tolerancia_km
    if km_desde >= fim:
        return "estourado", fim - km_desde, fim - km_desde
    if km_desde >= ini:
        return "janela", ini - km_desde, fim - km_desde
    if km_desde >= ini * 0.8:  # 9.000 km com padrão 12.000/1.000
        return "aproximando", ini - km_desde, fim - km_desde
    return "normal", ini - km_desde, fim - km_desde


def _projecao_para_grafico(mdl, km_desde_ultima, intervalo_km, tolerancia_km,
                           hoje, rng):
    """Série para o cone de projeção do gráfico: P10/P50/P90 de odo por dia."""
    alvo_limite = intervalo_km + tolerancia_km
    dist = max(1.0, alvo_limite - km_desde_ultima)
    media_dia = math.exp(mdl["ln_mu"])
    horizonte = min(240, int(1.5 * dist / max(media_dia, 1.0)) + 14)
    sigma = mdl["sigma"]

    paths = []
    for _ in range(400):  # amostra menor: só para o cone visual
        ln_mu = rng.gauss(mdl["ln_mu"], mdl["sigma_ln_mu"])
        fat = mdl["fatores"]  # fator médio no cone visual (ruído já vem de sigma)
        p0 = mdl["p0"]
        cum = km_desde_ultima
        path = [cum]
        for i in range(1, horizonte + 1):
            dia = mdl["ultimo_dia_obs"] + timedelta(days=i)
            km = 0.0 if rng.random() < p0 else rng.lognormvariate(
                ln_mu + math.log(max(fat[dia.weekday()], 0.05)) - 0.5 * sigma * sigma, sigma)
            cum += km
            path.append(cum)
        paths.append(path)

    cone = {"dias": list(range(horizonte + 1)), "p50": [], "p10": [], "p90": []}
    for i in range(horizonte + 1):
        col = sorted(p[i] for p in paths)
        cone["p10"].append(round(col[int(0.10 * len(col))], 1))
        cone["p50"].append(round(col[int(0.50 * len(col))], 1))
        cone["p90"].append(round(col[int(0.90 * len(col))], 1))
    return {"horizonte": horizonte, **cone}
```

**Exemplo de saída real do módulo** (rev. 2: modo múltiplos com odo 29.393 — o caso do
dono; 30 dias de série sintética):

```json
{
  "hoje": "2026-10-30",
  "intervalo_km": 12000.0,
  "tolerancia_km": 1000.0,
  "n_dias_obs": 30,
  "media_km_dia": 40.8,
  "previsao": {
    "dias_p10": 125,
    "dias_p50": 150,
    "dias_p90": 185,
    "data_p10": "2027-03-04",
    "data_p50": "2027-03-29",
    "data_p90": "2027-05-03",
    "p_janela_7d": 0.0,
    "p_janela_15d": 0.0,
    "p_janela_30d": 0.0,
    "p_estouro_30d": 0.0
  },
  "motivo_sem_previsao": null,
  "km_desde_ultima": 5393.0,
  "status": "normal",
  "janela_inicio_km": 11000.0,
  "janela_fim_km": 13000.0,
  "km_restantes_janela": 5607.0,
  "km_restantes_limite": 7607.0,
  "proximo_marco_km": 36000.0,
  "janela_inicio_abs": 35000.0,
  "janela_fim_abs": 37000.0,
  "nivel_km_dia_recente": 37.2,
  "mudanca_padrao": false,
  "confianca": {
    "score": 1.0,
    "nivel": "alta",
    "motivos": []
  },
  "modo_ancora": "multiplos",
  "eventos": []
}
```

### Integração (esqueleto do que entra no `dashboard.py`)

```python
import manutencao_previsao as mp

_manut_cache = {"ts": 0.0, "payload": None}

@app.get("/api/byd/manutencao")
def api_byd_manutencao():
    if time.time() - _manut_cache["ts"] < 60:
        return JSONResponse(_manut_cache["payload"])
    conn = sqlite3.connect(DB_FILE)
    try:
        snaps = conn.execute(
            "SELECT ts, odo_km FROM byd_snapshots "
            "WHERE ts >= ? ORDER BY ts",
            (int(time.time()) - 180 * 86400,),
        ).fetchall()
        eventos = conn.execute(
            "SELECT data, km, tipo, obs FROM manutencao_eventos "
            "ORDER BY data DESC, id DESC LIMIT 24"
        ).fetchall()
    finally:
        conn.close()

    pontos = mp.montar_serie_diaria(snaps)
    serie = mp.serie_km_por_dia(pontos)
    odo_atual = pontos[-1][1] if pontos else None

    out = mp.prever(serie, km_desde_ultima=None)  # base
    if eventos and odo_atual is not None:
        km_desde = odo_atual - eventos[0][1]       # âncora = evento mais recente
        out = mp.prever(serie, km_desde_ultima=km_desde)

    out["odo_atual"] = odo_atual
    out["eventos"] = [
        {"data": e[0], "km": e[1], "tipo": e[2], "obs": e[3]} for e in eventos
    ]
    _auditar(conn_nova=True, out=out)              # 1 linha/dia em manutencao_previsoes
    _manut_cache.update(ts=time.time(), payload=out)
    return JSONResponse(out)


@app.post("/api/byd/manutencao/evento")
def api_byd_manutencao_evento(body: dict):
    # valida: data ISO, km numérico ≤ odo atual do dia, > km do evento anterior;
    # 400 (nunca 500) em entrada ruim — mesmo padrão do enxame de validação
    ...
```

## 9. Exemplo de dashboard

Card único na **aba Carro**, entre o card do modelo/odômetro e o gráfico de SOC.
Mockup interativo e navegável: **`~/.zcode/v2/visualizations/.../manutencao_byd_mockup.html`**
(renderizado inline nesta conversa). Elementos:

1. **Status-bar** com 4 selos — Status (badge colorido por estado) · Próximo marco (km) ·
   Data prevista (P50 + faixa P10–P90) · Confiança (chip com tooltip dos motivos).
2. **Barra do ciclo** — progresso 0 → 13.000 km com marcações em 11.000 (janela) e 13.000
   (limite) e a posição atual; a faixa da janela fica pintada em âmbar.
3. **Chips de probabilidade** — "entrar na janela em 7/15/30 dias" e "risco de estouro em
   30 dias", em % arredondado; risco ≥50% pinta de vermelho.
4. **Gráfico** (Chart.js, já carregado na página) — odômetro histórico (linha sólida) +
   cone P10–P90 (área translúcida) + P50 (tracejada) + linhas horizontais nos marcos
   11k/12k/13k. Sem o CDN, degrada sem gráfico (bug pendente conhecido do CDN).
5. **Histórico de manutenções** — tabela dos eventos + botão "Registrar manutenção"
   (modal: data, km pré-preenchido com o odo atual, tipo, obs).
6. **Acurácia** (fase 2) — MAE das previsões já realizadas, quando houver.

Cores seguem as variáveis existentes (`--accent`, `--warn`, `--dim`); status:
✅ verde · ⚠️ âmbar · 🟠 âmbar forte pulsante · 🚨 vermelho.

## 10. Estratégia de aprendizado contínuo

- **Cada cálculo re-aprende do zero** sobre o banco inteiro (sem estado persistido):
  novas leituras entram automaticamente no nível, nos fatores semanais, em `p0` e em `σ`.
  Não existe "treinar" — existe "recalcular" (barato: <1 s na placa, com cache de 60 s).
- **Mudança de padrão** é detectada, sinalizada e **incorporada** pelo peso exponencial
  (meia-vida 21 d): férias de 3 semanas derrubam o nível sozinhas sem intervenção.
- **Carro parado**: dias zerados observados alimentam `p0` — a previsão desliza para fora
  honestamente; a flag `mudanca_padrao` avisa no painel.
- **Recalibragem manual** = registrar/corrigir a manutenção no painel (âncora nova) ou
  ajustar `intervalo_km`/`tolerancia_km` no `byd.json`. Não há outro parâmetro mágico.
- **Acurácia das previsões anteriores**: `manutencao_previsoes` guarda 1 previsão/dia;
  quando a janela é de fato cruzada (ou manutenção registrada), o MAE dos P50 anteriores
  vira o indicador de acurácia do painel (fase 2).

## 11. Testes — resultados reais da validação

Suite: `/tmp/previsao_test/test_cenarios.py` (vai para `tests/test_manutencao.py` na fase 1).
Resultado final **27/27 ✅** após as correções do Agente Corretor (2 falhas reais corrigidas
no modelo, 2 specs de teste corrigidas):

| Cenário | Resultado-chave |
|---|---|
| Comutador regular 60 d (45 km útil / 12 km fds) | P50 = 320 d (teoria ≈ 309) ✅ · confiança alta · P10<P50<P90 |
| Perto da janela (faltam 550 km) | P50 = 16 d, P(15 d)=0,44, P(7 d)=0 — coerente com ~37 km/dia |
| Dentro da janela, perto do limite | risco de estouro 30 d = 0,93 (limite a ~24 d) ✅ |
| Carro parado 20 dias | `mudanca_padrao=True`, confiança cai, P50 empurrado p/ 312 d |
| Sentinela `odo=0` + rebaixada 30% | filtrados; total preservado (±3%); nenhum km negativo |
| Vão de 5 dias sem dados | 150 km espalhados como 30/dia, sem pico |
| Cold start | <3 dias: sem previsão · 5 dias: prevê com confiança baixa · sem âncora: `sem_ancora` |
| Manutenção atrasada (13.500 e 38.000 km) | status 🚨; projeção do próximo ciclo; módulo sobrevive a ciclos pulados |
| Determinismo | mesma entrada ⇒ saída idêntica (semente fixa) |
| **Performance** | 466 ms/cálculo (x86); orçamento de 2 s na cubie2 com folga |

Testes recomendados ao implementar: rodar a suite no CI (pytest, padrão do repo) e uma
verificação na placa com os dados reais de `byd_snapshots` (deve nascer com
`historico_insuficiente` ou confiança baixa — **é o comportamento correto** hoje, com
~10 dias de histórico).

## 12. Plano de implementação por etapas

- **Fase 1 — núcleo no ar (1 deploy):** `src/manutencao_previsao.py` (como está) + DDL das
  duas tabelas + `GET /api/byd/manutencao` com cache + `POST /api/byd/manutencao/evento`
  (validação 400) + card na aba Carro **sem gráfico** (status, barra, chips, registro de
  manutenção) + suite `tests/test_manutencao.py`. Commit → deploy_cubie.sh → registrar a
  manutenção passada (âncora) no primeiro acesso.
- **Fase 2 — gráfico e memória:** cone P10–P90 no Chart.js + tabela de auditoria
  alimentada (1/dia) + indicador MAE quando existir previsão realizada + badge de
  `mudanca_padrao`.
- **Fase 3 — cross-checks e alerta:** validação cruzada de km (Δodo vs kWh carregados ×
  consumo médio do carro, usando dados que o sistema já tem) e vs soma de `byd_trips`;
  alerta Telegram (reaproveitando o canal do `vigia-energia.sh`) quando P(janela ≤7 d)
  ≥ 50% ou status vira 🟠/🚨.

## 13. Melhorias futuras

1. **Cross-check por energia como fallback de odômetro**: se o odômetro sumir (guerra de
   sessão), estimar km por kWh carregados ÷ consumo médio histórico do carro.
2. **Padrão por tipo de rota**: cruza com `byd_trips` (geocerca casa) para separar
   commuting de viagem — melhora o fator semanal.
3. **Manutenção por tempo também** (12 meses): o DM-i tem limite temporal além do km;
   o modelo prevê o que vier primeiro.
4. **Calibração empírica**: com 6+ meses de auditoria, recalibrar σ e o cap de confiança
   pelos erros reais observados (as datas previstas já estarão guardadas).
5. **Multi-veículo**: as tabelas já aceitam uma coluna `veiculo` quando houver.

---

*Riscos remanescentes conscientes: (a) o odômetro do app BYD é cumulativo e confiável,
mas a cobertura depende da guerra de sessão do app MY BYD — vãos viram espalhamento,
nunca dado falso; (b) a acurácia real da P50 só fica mensurável com meses de auditoria —
por isso a tabela de previsões existe desde a fase 1; (c) CDN do Chart.js sem fallback é
bug pendente já conhecido do projeto, não deste estudo.*
