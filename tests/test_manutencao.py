#!/usr/bin/env python3
"""
Regressão da previsão de manutenção (fase 1, 28/10/2026):

- Modelo: cenários do estudo (comutador regular, perto da janela, carro
  parado, sentinela odo=0 + glitch, vão de dados, cold start, ciclo
  estourado, determinismo). docs/ESTUDO_PREVISAO_MANUTENCAO.md §11.
- API: GET /api/byd/manutencao (sem dados, com âncora, auditoria 1/dia) e
  POST /api/byd/manutencao/evento (validação dura: 400, nunca 500).

Run with:  python3 -m pytest tests/test_manutencao.py -v
"""
import random
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import manutencao_previsao as mp  # noqa: E402

_tmpdir = tempfile.mkdtemp(prefix="energia_mnt_test_")


def _serie_comutador(dias, mu_util=45.0, mu_fds=12.0, seed=1, parado_depois=None):
    """Seg–seg: 45 km útil, 12 km fim de semana; parado_depois zera o resto."""
    rng = random.Random(seed)
    d0 = date(2026, 10, 1)  # quinta
    out = []
    for i in range(dias):
        d = d0 + timedelta(days=i)
        if parado_depois is not None and d > parado_depois:
            km = 0.0
        else:
            km = mu_util if d.weekday() < 5 else mu_fds
            km = max(0.0, rng.gauss(km, 0.25 * km))
        out.append((d, km))
    return out


class TestModeloPrevisao(unittest.TestCase):
    def test_comutador_regular_ordem_de_grandeza(self):
        serie = _serie_comutador(60)
        res = mp.prever(serie, km_desde_ultima=0.0)
        p = res["previsao"]
        self.assertTrue(30 <= res["media_km_dia"] <= 40)
        self.assertTrue(250 <= p["dias_p50"] <= 380)
        self.assertLess(p["dias_p10"], p["dias_p50"])
        self.assertLess(p["dias_p50"], p["dias_p90"])
        self.assertLess(p["p_janela_30d"], 0.05)
        self.assertEqual(res["confianca"]["nivel"], "alta")

    def test_perto_da_janela_probabilidades(self):
        serie = _serie_comutador(45, mu_util=50.0)  # ~37 km/dia
        res = mp.prever(serie, km_desde_ultima=10450.0)
        p = res["previsao"]
        self.assertEqual(res["status"], "aproximando")
        self.assertTrue(8 <= p["dias_p50"] <= 24)
        # faltam ~15 dias de uso: P(7d)~0 e P(15d) bem maior
        self.assertGreater(p["p_janela_15d"], p["p_janela_7d"])
        self.assertLess(p["p_janela_7d"], 0.05)
        self.assertLess(p["p_estouro_30d"], 0.05)

    def test_dentro_da_janela_risco_real_de_estouro(self):
        res = mp.prever(_serie_comutador(45, mu_util=50.0), km_desde_ultima=12100.0)
        self.assertEqual(res["status"], "janela")
        self.assertGreater(res["previsao"]["p_estouro_30d"], 0.2)

    def test_veiculo_parado_20_dias(self):
        res = mp.prever(
            _serie_comutador(40, parado_depois=date(2026, 10, 20)),
            km_desde_ultima=6000.0,
        )
        self.assertIsNotNone(res["previsao"])
        self.assertTrue(res["mudanca_padrao"])
        self.assertIn(res["confianca"]["nivel"], ("baixa", "media"))
        self.assertGreater(res["previsao"]["dias_p50"], 200)

    def test_sentinela_zero_e_rebaixada_filtrados(self):
        rng = random.Random(2)
        serie = _serie_comutador(30, seed=2)
        snaps = []
        odo = 40000.0
        ts = datetime(2026, 10, 1, 6, 0).timestamp()
        snaps.append((ts, odo))
        for _, km in serie:
            odo += km
            snaps += [(ts, odo), (ts + 3600, odo)]
            ts += 86400
        snaps.append((ts, 0.0))            # sentinela: carro dormindo
        snaps.append((ts + 60, odo * 0.3))  # glitch de rebaixada
        snaps.append((ts + 120, odo))
        pontos = mp.montar_serie_diaria(snaps)
        km_dia = mp.serie_km_por_dia(pontos)
        self.assertTrue(all(km >= 0 for _, km in km_dia))
        self.assertTrue(all(km <= 100 for _, km in km_dia))
        total_odo = pontos[-1][1] - pontos[0][1]
        self.assertAlmostEqual(
            sum(km for _, km in km_dia), total_odo, delta=total_odo * 0.03
        )

    def test_vao_de_5_dias_e_espalhado_sem_pico(self):
        serie = _serie_comutador(20)
        cum = 40000.0
        pontos = []
        for d, km in serie:
            cum += km
            pontos.append((d, cum))
        com_vao = [p for i, p in enumerate(pontos) if i not in (8, 9, 10, 11, 12)]
        km_vao = dict(mp.serie_km_por_dia(com_vao))
        faltantes = [pontos[8][0] + timedelta(days=j) for j in range(6)]
        for d in faltantes:
            self.assertIn(d, km_vao)
            self.assertLess(km_vao[d], 100)

    def test_cold_start_e_sem_ancora(self):
        res = mp.prever(_serie_comutador(2), km_desde_ultima=5000.0)
        self.assertIsNone(res["previsao"])
        self.assertIn("insuficiente", res["motivo_sem_previsao"])
        res5 = mp.prever(_serie_comutador(5), km_desde_ultima=5000.0)
        self.assertIsNotNone(res5["previsao"])
        self.assertEqual(res5["confianca"]["nivel"], "baixa")
        res_sem = mp.prever(_serie_comutador(30), km_desde_ultima=None)
        self.assertEqual(res_sem["motivo_sem_previsao"], "sem_ancora")

    def test_ciclo_estourado_nao_quebra(self):
        res = mp.prever(_serie_comutador(30), km_desde_ultima=13500.0)
        self.assertEqual(res["status"], "estourado")
        self.assertEqual(res["km_restantes_limite"], 0.0)
        self.assertIsNotNone(res["previsao"])  # projeção do próximo ciclo
        res2 = mp.prever(_serie_comutador(30), km_desde_ultima=38000.0)
        self.assertEqual(res2["status"], "estourado")
        self.assertIsNotNone(res2["previsao"])

    def test_determinismo(self):
        a = mp.prever(_serie_comutador(45, seed=7), km_desde_ultima=10450.0)
        b = mp.prever(_serie_comutador(45, seed=7), km_desde_ultima=10450.0)
        self.assertEqual(a["previsao"]["dias_p50"], b["previsao"]["dias_p50"])

    def test_dia_brt_fixo(self):
        # 02:00 UTC = 23:00 do dia anterior em BRT: o bucket tem que cair no
        # dia BRT (a placa roda em UTC; padrão do repo é BRT fixo -3).
        # Epoch construído como UTC explícito: independe da tz da máquina.
        ts = datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc).timestamp()
        pontos = mp.montar_serie_diaria([(ts, 1234.5)])
        self.assertEqual(pontos[0][0].isoformat(), "2026-10-27")


class TestApiManutencao(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import dashboard
        self.dash = dashboard
        self._orig_db = dashboard.DB_FILE
        dashboard.DB_FILE = Path(_tmpdir) / "data" / "tuya_history.db"
        dashboard.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        if dashboard.DB_FILE.exists():
            dashboard.DB_FILE.unlink()
        dashboard.init_db()
        dashboard._ttl_cache.clear()
        self.client = TestClient(dashboard.app)

    def tearDown(self):
        self.dash.DB_FILE = self._orig_db
        self.dash._ttl_cache.clear()

    def _seed_odo(self, dias=30, odo0=42000.0, km_dia=40.0):
        conn = sqlite3.connect(self.dash.DB_FILE)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS byd_snapshots ("
            "ts INTEGER PRIMARY KEY, odo_km REAL)"
        )
        base = datetime(2026, 10, 1, 12, 0).timestamp()
        odo = odo0
        rows = []
        for i in range(dias):
            odo += km_dia
            rows.append((int(base + i * 86400), odo))
        conn.executemany("INSERT OR REPLACE INTO byd_snapshots VALUES (?,?)", rows)
        conn.commit()
        conn.close()
        return rows[-1][1]

    def test_get_sem_dados_alguns(self):
        r = self.client.get("/api/byd/manutencao")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIsNone(d["previsao"])
        # sem série E sem âncora: o bloqueador exibido é a âncora (CTA),
        # não o histórico — o dono precisa registrar antes de qualquer coisa
        self.assertEqual(d["motivo_sem_previsao"], "sem_ancora")
        self.assertIsNone(d["odo_atual"])

    def test_post_evento_validacao_400(self):
        for qs in (
            "data=28/10/2026&km=50000",          # data fora do formato
            "data=2026-10-28&km=50000&tipo=qualquer",  # tipo inválido
            "data=2026-10-28&km=0",               # km <= 0
            "data=2026-10-28&km=-5",
        ):
            r = self.client.post(f"/api/byd/manutencao/evento?{qs}")
            self.assertEqual(r.status_code, 400, qs)

    def test_post_evento_km_acima_do_odometro(self):
        odo_fim = self._seed_odo()
        r = self.client.post(
            f"/api/byd/manutencao/evento?data=2026-10-28&km={odo_fim + 500}"
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("odômetro", r.json()["detail"])

    def test_ciclo_completo_ancora_previsao_e_auditoria(self):
        odo_fim = self._seed_odo()  # ~42000 + 30*40 = 43200
        r = self.client.post(
            "/api/byd/manutencao/evento?data=2026-04-15&km=42000&tipo=preventiva&obs=teste"
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])

        self.dash._ttl_cache.clear()  # POST já limpa; garantia de recálculo
        r = self.client.get("/api/byd/manutencao")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIsNone(d["motivo_sem_previsao"])
        self.assertIsNotNone(d["previsao"])
        self.assertAlmostEqual(d["km_desde_ultima"], odo_fim - 42000, delta=1.0)
        self.assertEqual(len(d["eventos"]), 1)

        # auditoria: 1 linha em manutencao_previsoes
        conn = sqlite3.connect(self.dash.DB_FILE)
        n = conn.execute("SELECT COUNT(*) FROM manutencao_previsoes").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_post_sem_odometro_aceita_ancora_retroativa(self):
        # sem byd_snapshots (BYD nunca coletou): valida km ≤ odo é impossível,
        # então o registro retroativo da âncora tem que passar (cold start real)
        r = self.client.post("/api/byd/manutencao/evento?data=2026-04-15&km=42000")
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/byd/manutencao")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["motivo_sem_previsao"], "sem_ancora")  # sem série ainda
        self.assertEqual(len(d["eventos"]), 1)

    def test_segundo_evento_km_deve_subir(self):
        self.client.post("/api/byd/manutencao/evento?data=2026-04-15&km=42000")
        r = self.client.post("/api/byd/manutencao/evento?data=2026-05-15&km=41000")
        self.assertEqual(r.status_code, 400)
        self.assertIn("última manutenção", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
