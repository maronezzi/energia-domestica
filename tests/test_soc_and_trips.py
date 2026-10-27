#!/usr/bin/env python3
"""
Regressão dos dois P2 quantificados pelo enxame de validação (28/10/2026):

1. Corrida no fechamento de viagem (byd_client._update_trip): o trecho
   dirigido entre o 1º e o 2º ciclo parado (nenhum poll em movimento —
   cadência idle de 15 min) não atualizava end_odo, e a âncora
   _last_still_odo era reescrita no mesmo ciclo → o km escapava da viagem
   E da reconstrução (7 de 60 km em 25-27/10). Fix: estender o fim da
   viagem até o odômetro do ciclo de fechamento.

2. Âncora de SOC envenenada (estimate_car_soc_start): a BYD manda 0 como
   sentinela (junto de odo=0) e o finalize semeava car_current_soc=100 sem
   marca — a seed de um fim de carga pleno virou soc_start de sessões que
   entraram a ~24% (sessões "100→100" de 21-22/10; a 0→77,56 de 26/10).
   Fix: _byd_anchor_soc rejeita 0/100 como âncora (2-98); seed marcada
   (car_current_soc_src='seed') e perde para leitura BYD fresca e para o
   soc_start reconciliado; input do dono ('owner') continua vencendo tudo.

Run with:  python3 -m pytest tests/test_soc_and_trips.py -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

_tmpdir = tempfile.mkdtemp(prefix="energia_p2_test_")
os.makedirs(f"{_tmpdir}/data", exist_ok=True)

import byd_client as bc  # noqa: E402


class TripTestBase(unittest.TestCase):
    def setUp(self):
        self._orig_db = bc.DB_FILE
        bc.DB_FILE = Path(_tmpdir) / "data" / "byd_test.db"
        if bc.DB_FILE.exists():
            bc.DB_FILE.unlink()
        bc.init_db()
        # estado global da máquina de viagens
        self._g = {}
        for g in ("_trip", "_trip_still", "_last_still_odo", "_last_still_ts", "_last_still_soc"):
            self._g[g] = getattr(bc, g)
            setattr(bc, g, None)
        bc._trip_still = 0

    def tearDown(self):
        bc.DB_FILE = self._orig_db
        for g, v in self._g.items():
            setattr(bc, g, v)

    @staticmethod
    def _snap(odo, ts, soc=50.0):
        return {"odo_km": odo, "ts": ts, "soc": soc}

    def _trips(self):
        conn = sqlite3.connect(bc.DB_FILE)
        try:
            return conn.execute(
                "SELECT start_odo, end_odo, soc_end, end_ts FROM byd_trips "
                "ORDER BY start_ts"
            ).fetchall()
        finally:
            conn.close()


class TestTripClosingRace(TripTestBase):
    def test_tail_between_still_cycles_joins_trip(self):
        # anda (100→105), para, e entre o 1º e o 2º ciclo parado dirige mais
        # 5 km que NENHUM poll pegou em movimento (a corrida real de 26/10)
        bc._update_trip(self._snap(100.0, 1000), None, True)
        bc._update_trip(self._snap(105.0, 1060), None, True)
        bc._update_trip(self._snap(105.0, 1120), None, False)   # 1º still
        bc._update_trip(self._snap(110.0, 1300), None, False)   # 2º still: fecha
        trips = self._trips()
        self.assertEqual(len(trips), 1)
        start_odo, end_odo, soc_end, end_ts = trips[0]
        self.assertEqual(start_odo, 100.0)
        self.assertEqual(end_odo, 110.0, "trecho do fechamento tem que entrar na viagem")
        self.assertEqual(end_ts, 1300)

    def test_normal_close_unchanged(self):
        bc._update_trip(self._snap(100.0, 1000), None, True)
        bc._update_trip(self._snap(105.0, 1060), None, True)
        bc._update_trip(self._snap(105.0, 1120), None, False)
        bc._update_trip(self._snap(105.0, 1300), None, False)
        trips = self._trips()
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0][1], 105.0, "sem avanço de odo, nada muda")

    def test_tail_below_threshold_not_joined(self):
        # 0,1 km entre os ciclos = ruído de arredondamento (odômetro é inteiro)
        bc._update_trip(self._snap(100.0, 1000), None, True)
        bc._update_trip(self._snap(104.0, 1060), None, True)
        bc._update_trip(self._snap(104.0, 1120), None, False)
        bc._update_trip(self._snap(104.1, 1300), None, False)
        trips = self._trips()
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0][1], 104.0)

    def test_reconstruction_without_trip_unchanged(self):
        # sem viagem aberta: avanço de odo entre ciclos parados continua
        # virando viagem reconstruída (fix ce874f1 intacto)
        bc._update_trip(self._snap(100.0, 1000), None, False)
        bc._update_trip(self._snap(105.0, 1300), None, False)
        trips = self._trips()
        self.assertEqual(len(trips), 1)
        self.assertEqual((trips[0][0], trips[0][1]), (100.0, 105.0))


class TestBydAnchorSoc(unittest.TestCase):
    def setUp(self):
        import dashboard
        self.dash = dashboard
        self._orig_db = dashboard.DB_FILE
        self._orig_cfg = dashboard.CONFIG_FILE
        self._orig_fresh = bc.fresh_soc
        dashboard.DB_FILE = Path(_tmpdir) / "data" / "soc_test.db"
        dashboard.CONFIG_FILE = Path(_tmpdir) / "data" / "tuya_config.json"
        for f in (dashboard.DB_FILE, dashboard.CONFIG_FILE):
            if f.exists():
                f.unlink()
        dashboard.init_db()
        dashboard._cfg_cache = None
        dashboard._ttl_cache.clear()

    def tearDown(self):
        self.dash.DB_FILE = self._orig_db
        self.dash.CONFIG_FILE = self._orig_cfg
        bc.fresh_soc = self._orig_fresh
        self.dash._cfg_cache = None

    def _seed_session(self, status="completed", soc_start=50.0, soc_end=80.0,
                      end_reason="manual", hours_ago=2.0):
        conn = sqlite3.connect(self.dash.DB_FILE)
        try:
            end_dt = datetime.now() - timedelta(hours=hours_ago)
            conn.execute(
                "INSERT INTO charge_sessions (session_uuid, start_time, end_time, "
                "status, soc_start, soc_end, end_reason, energy_delivered_kwh) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"u-{end_dt.timestamp()}", end_dt.isoformat(), end_dt.isoformat(),
                    status, soc_start, soc_end, end_reason, 5.0,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _cfg(self, soc=None, ts=None, src=None):
        c = self.dash.load_config()
        if soc is not None:
            c["car_current_soc"] = soc
        if ts is not None:
            c["car_current_soc_ts"] = ts
        if src is not None:
            c["car_current_soc_src"] = src
        self.dash.save_config(c)
        self.dash._cfg_cache = None
        return c

    def test_byd_sentinel_zero_not_anchor(self):
        # veneno A: BYD mandou 0 (sentinela) → cai no último SOC conhecido
        self._seed_session(soc_end=80.0)
        bc.fresh_soc = lambda: 0.0
        soc, est = self.dash.estimate_car_soc_start(self.dash.load_config())
        self.assertEqual((soc, est), (80, True))

    def test_byd_full_100_falls_to_reconciled_start(self):
        # veneno B com BYD saturada: 100 não é partida; vale o reconciliado
        self._seed_session(status="auto_stopped", soc_start=76.0, soc_end=100.0,
                           end_reason="auto")
        bc.fresh_soc = lambda: 100.0
        soc, est = self.dash.estimate_car_soc_start(self.dash.load_config())
        self.assertEqual((soc, est), (76, True))

    def test_seed_100_loses_to_reconciled_start(self):
        # veneno B sem BYD: seed do finalize (100, sem marca seria pior) perde
        # para o soc_start reconciliado da sessão anterior
        self._seed_session(status="auto_stopped", soc_start=76.0, soc_end=100.0,
                           end_reason="auto")
        self._cfg(soc=100, ts=datetime.now().isoformat(), src="seed")
        bc.fresh_soc = lambda: None
        soc, est = self.dash.estimate_car_soc_start(self.dash.load_config())
        self.assertEqual((soc, est), (76, True))

    def test_fresh_byd_beats_stale_seed(self):
        # plug com leitura BYD fresca (24%) vence seed velha de fim de carga
        self._seed_session(soc_end=80.0)
        self._cfg(soc=100, ts=datetime.now().isoformat(), src="seed")
        bc.fresh_soc = lambda: 24.0
        soc, est = self.dash.estimate_car_soc_start(self.dash.load_config())
        self.assertEqual((soc, est), (24, True))

    def test_owner_input_wins_over_fresh_byd(self):
        # regra preservada: input explícito do dono vence a leitura BYD
        self._seed_session(soc_end=80.0, hours_ago=10.0)
        self._cfg(soc=52, ts=datetime.now().isoformat(), src="owner")
        bc.fresh_soc = lambda: 35.0
        soc, est = self.dash.estimate_car_soc_start(self.dash.load_config())
        self.assertEqual((soc, est), (52, False))

    def test_seed_implausible_falls_to_last_known_soc(self):
        self._seed_session(soc_start=60.0, soc_end=80.0)
        self._cfg(soc=100, ts=datetime.now().isoformat(), src="seed")
        bc.fresh_soc = lambda: None
        soc, est = self.dash.estimate_car_soc_start(self.dash.load_config())
        self.assertEqual((soc, est), (80, True))

    def test_no_history_no_byd_uses_owner_config(self):
        self._cfg(soc=66, ts=datetime.now().isoformat(), src="owner")
        bc.fresh_soc = lambda: None
        soc, est = self.dash.estimate_car_soc_start(self.dash.load_config())
        self.assertEqual((soc, est), (66, False))

    def test_anchor_helper_gates_sentinels(self):
        cases = {0.0: None, 1.0: None, 100.0: None, 99.0: None, 35.0: 35.0, 2.0: 2.0}
        for byd_val, expected in cases.items():
            bc.fresh_soc = lambda v=byd_val: v
            self.assertEqual(
                self.dash._byd_anchor_soc(), expected, f"fresh_soc={byd_val}"
            )
        bc.fresh_soc = lambda: None
        self.assertIsNone(self.dash._byd_anchor_soc())


if __name__ == "__main__":
    unittest.main()
