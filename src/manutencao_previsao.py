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

N_TRIALS = 3000               # simulações Monte Carlo por cálculo
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
        fat = [max(0.05, rng.gauss(w, 0.15 + 0.5 / max(1, n)))
               for w, n in zip(mdl["fatores"], mdl["n_dow"])]
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
                km = rng.lognormvariate(ln_mu + math.log(fat[dia.weekday()]) + adj, sigma)
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


def prever(serie_km, km_desde_ultima, intervalo_km=INTERVALO_PADRAO,
           tolerancia_km=TOLERANCIA_PADRAO, hoje=None, seed=SEED_PADRAO):
    """Ponto de entrada. serie_km: [(date, km_dia)]. Devolve dict da API.

    Se km_desde_ultima é None (sem âncora) ou série insuficiente, devolve
    previsão None com motivo — o painel mostra o estado, não inventa número.
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
        # primeiro o bloqueador real do painel: sem âncora, o convite é
        # registrar a manutenção — mesmo com a série ainda vazia
        out["motivo_sem_previsao"] = "sem_ancora"
        return out
    if len(serie_km) < 3:
        out["motivo_sem_previsao"] = f"historico_insuficiente ({len(serie_km)} dias; minimo 3)"
        return out
    hoje = hoje_dia

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

    status, km_resta_j, km_resta_l = _status(km_desde_ultima, intervalo_km, tolerancia_km)

    out.update({
        "km_desde_ultima": round(km_desde_ultima, 1),
        "status": status,
        "janela_inicio_km": intervalo_km - tolerancia_km,
        "janela_fim_km": intervalo_km + tolerancia_km,
        "km_restantes_janela": round(max(0.0, km_resta_j), 1),
        "km_restantes_limite": round(max(0.0, km_resta_l), 1),
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
        "projecao": _projecao_para_grafico(mdl, km_desde_ultima, intervalo_km,
                                           tolerancia_km, hoje, rng),
    })
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
