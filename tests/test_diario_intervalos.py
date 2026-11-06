#!/usr/bin/env python3
"""
Filtro do Diário de Bordo por intervalo (06/11/2026):

O diário só navegava dia a dia (◀ ▶ Hoje). Agora os três endpoints
(/api/byd/positions, /api/byd/trips, /api/byd/daily) aceitam intervalo
(start/end BRT inclusive) além do dia único, e o frontend ganha presets
(7/30 dias, mês corrente/anterior e intervalo livre). Regressões cobertas:

1. positions_for_range/trips_for_range/daily_trips_between pegam os DOIS
   dias das pontas (bounds inclusivos) e nada fora deles.
2. /api/byd/positions decima janelas longas sem perder a 1ª e a última
   posição (marcadores de início/fim do trajeto no mapa).
3. Validação: 400 em start sem end, formato ruim, intervalo invertido e
   janela maior que 92 dias.
4. Compatibilidade: date= de um dia só e /daily?days=N seguem inteiros.

Run with:  python3 -m pytest tests/test_diario_intervalos.py -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

_tmpdir = tempfile.mkdtemp(prefix="energia_diario_test_")
os.makedirs(f"{_tmpdir}/data", exist_ok=True)

import byd_client as bc  # noqa: E402

DIA_A, DIA_B, DIA_C = "2026-11-04", "2026-11-05", "2026-11-06"


def _brt_epoch(date_str: str, hour: int = 12) -> int:
    start, _ = bc._brt_day_bounds(date_str)
    return start + hour * 3600


class DiarioTestBase(unittest.TestCase):
    def setUp(self):
        self._orig_db = bc.DB_FILE
        bc.DB_FILE = Path(_tmpdir) / "data" / f"byd_{id(self)}.db"
        if bc.DB_FILE.exists():
            bc.DB_FILE.unlink()
        bc.init_db()

    def tearDown(self):
        bc.DB_FILE = self._orig_db

    def _conn(self):
        return sqlite3.connect(bc.DB_FILE)

    def _pos(self, ts, lat=-23.44, lon=-47.50, odo=100.0):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO byd_positions (ts, gps_ts, lat, lon, speed, "
                "heading, odo_km) VALUES (?,?,?,?,?,?,?)",
                (ts, ts, lat, lon, 0.0, 0.0, odo),
            )
            conn.commit()
        finally:
            conn.close()

    def _trip(self, date_str, km=10.0, soc=50.0):
        ts = _brt_epoch(date_str, 10)
        conn = self._conn()
        try:
            bc._insert_trip(conn, {
                "start_ts": ts, "end_ts": ts + 1800,
                "start_odo": 1000.0, "end_odo": 1000.0 + km,
                "soc_start": soc, "soc_end": soc - 5.0,
                "start_point": {"lat": -23.44250, "lon": -47.50091},
                "end_point": {"lat": -23.60, "lon": -47.60},
            })
            conn.commit()
        finally:
            conn.close()

    def _snap(self, ts, odo, soc):
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO byd_snapshots (ts, data_ts, soc, odo_km) "
                "VALUES (?,?,?,?)",
                (ts, ts, soc, odo),
            )
            conn.commit()
        finally:
            conn.close()


class TestRangeQueries(DiarioTestBase):
    def test_positions_for_range_covers_both_ends(self):
        for dia in (DIA_A, DIA_B, DIA_C):
            self._pos(_brt_epoch(dia))
        got = bc.positions_for_range(DIA_A, DIA_B)
        self.assertEqual([p["ts"] for p in got],
                         [_brt_epoch(DIA_A), _brt_epoch(DIA_B)])
        self.assertEqual(len(bc.positions_for_day(DIA_B)), 1)

    def test_trips_for_range_covers_both_ends(self):
        self._trip(DIA_A)
        self._trip(DIA_C, km=8.0)
        got = bc.trips_for_range(DIA_A, DIA_C)
        self.assertEqual(len(got), 2)
        self.assertEqual([t["km"] for t in got], [10.0, 8.0])
        self.assertEqual(bc.trips_for_range(DIA_B, DIA_B), [])

    def test_daily_trips_between_sums_odometer_per_day(self):
        # dia A: odo 100→110 e SOC 80→70 · dia B: odo 110→115 e SOC 70→60
        a1, a2 = _brt_epoch(DIA_A, 9), _brt_epoch(DIA_A, 18)
        b1, b2 = _brt_epoch(DIA_B, 9), _brt_epoch(DIA_B, 18)
        self._snap(a1, 100.0, 80.0)
        self._snap(a1 + 60, 105.0, 75.0)
        self._snap(a2, 110.0, 70.0)
        self._snap(b1, 110.0, 70.0)   # retomada no dia seguinte (sem avanço)
        self._snap(b2, 115.0, 60.0)
        out = bc.daily_trips_between(DIA_A, DIA_B)
        self.assertEqual(out[DIA_A]["km"], 10.0)
        self.assertEqual(out[DIA_B]["km"], 5.0)
        self.assertAlmostEqual(out[DIA_A]["kwh_ev"], 1.29, delta=0.01)
        self.assertAlmostEqual(out[DIA_B]["kwh_ev"], 1.29, delta=0.01)
        # dia fora da janela nem entra; dreno (SOC caindo parado) não vira km
        out_a = bc.daily_trips_between(DIA_A, DIA_A)
        self.assertNotIn(DIA_B, out_a)
        self.assertEqual(out_a[DIA_A]["km"], 10.0)

    def test_daily_trips_days_wrapper_unchanged_shape(self):
        self._snap(_brt_epoch(DIA_A), 100.0, 80.0)
        out = bc.daily_trips(days=7)
        self.assertIsInstance(out, dict)
        self.assertTrue(all(
            set(v) >= {"km", "kwh_ev", "trips", "seconds",
                       "kwh_per_100km", "hours"}
            for v in out.values()
        ))


class TestDiarioEndpoints(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import dashboard
        self.d = dashboard
        self._orig = (dashboard.DB_FILE, dashboard.CONFIG_FILE, bc.DB_FILE)
        dashboard.DB_FILE = Path(_tmpdir) / "data" / f"dash_{id(self)}.db"
        dashboard.CONFIG_FILE = Path(_tmpdir) / "data" / f"cfg_{id(self)}.json"
        bc.DB_FILE = Path(_tmpdir) / "data" / f"byd_{id(self)}.db"
        for f in (dashboard.DB_FILE, dashboard.CONFIG_FILE, bc.DB_FILE):
            if f.exists():
                f.unlink()
        dashboard.init_db()
        bc.init_db()
        dashboard._cfg_cache = None
        dashboard._ttl_cache.clear()
        # sementes: uma viagem em A e outra em C + posições nos dois dias
        conn = sqlite3.connect(bc.DB_FILE)
        try:
            for dia, km in ((DIA_A, 10.0), (DIA_C, 8.0)):
                ts = _brt_epoch(dia)
                bc._insert_trip(conn, {
                    "start_ts": ts, "end_ts": ts + 1800,
                    "start_odo": 1000.0, "end_odo": 1000.0 + km,
                    "soc_start": 50.0, "soc_end": 45.0,
                    "start_point": {"lat": -23.44250, "lon": -47.50091},
                    "end_point": {"lat": -23.60, "lon": -47.60},
                })
                conn.execute(
                    "INSERT INTO byd_positions (ts, gps_ts, lat, lon, speed, "
                    "heading, odo_km) VALUES (?,?,?,?,?,?,?)",
                    (ts, ts, -23.44, -47.50, 0.0, 0.0, 1000.0),
                )
            conn.commit()
        finally:
            conn.close()
        # sessão de tomada no dia A (alimenta kwh_home do /daily)
        conn = sqlite3.connect(dashboard.DB_FILE)
        try:
            conn.execute(
                "INSERT INTO charge_sessions (session_uuid, start_time, "
                "end_time, status, energy_delivered_kwh, total_cost) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("u-diario", f"{DIA_A}T10:00:00", f"{DIA_A}T12:00:00",
                 "completed", 3.5, 3.34),
            )
            conn.commit()
        finally:
            conn.close()
        # enriquecimento de custo: nuvem BYD fora do caminho nos testes
        self._patches = [
            patch.object(bc, "status", return_value={"snapshot": {}}),
            patch.object(bc, "energy_graph", return_value=None),
            patch.object(bc, "fuel_events", return_value=[]),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        self.client = TestClient(dashboard.app)

    def tearDown(self):
        self.d.DB_FILE, self.d.CONFIG_FILE, bc.DB_FILE = self._orig
        self.d._cfg_cache = None

    def test_trips_endpoint_interval_and_day(self):
        r = self.client.get(f"/api/byd/trips?start={DIA_A}&end={DIA_C}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["start"], body["end"]), (DIA_A, DIA_C))
        self.assertEqual(len(body["trips"]), 2)
        for t in body["trips"]:
            self.assertIn("cost_ev", t)
            self.assertIn("cost_fuel", t)
        r = self.client.get(f"/api/byd/trips?date={DIA_A}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["trips"]), 1)  # dia único segue de pé

    def test_positions_endpoint_interval_and_decimation(self):
        r = self.client.get(f"/api/byd/positions?start={DIA_A}&end={DIA_C}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["positions"]), 2)
        # janela longa decima preservando 1ª e última posição
        ts0 = _brt_epoch(DIA_A, 8)
        conn = sqlite3.connect(bc.DB_FILE)
        try:
            for i in range(25):
                conn.execute(
                    "INSERT INTO byd_positions (ts, gps_ts, lat, lon, speed, "
                    "heading, odo_km) VALUES (?,?,?,?,?,?,?)",
                    (ts0 + i * 60, ts0 + i * 60, -23.44, -47.50, 0.0, 0.0, 1.0),
                )
            conn.commit()
        finally:
            conn.close()
        old_cap = self.d._POS_MAP_CAP
        self.d._POS_MAP_CAP = 10
        try:
            r = self.client.get(f"/api/byd/positions?start={DIA_A}&end={DIA_C}")
            pos = r.json()["positions"]
            self.assertEqual(len(pos), 10)
            self.assertEqual(pos[0]["ts"], ts0)                  # 1ª fica
            self.assertEqual(pos[-1]["ts"], _brt_epoch(DIA_C))   # última fica
        finally:
            self.d._POS_MAP_CAP = old_cap

    def test_daily_endpoint_interval_rows_and_home_kwh(self):
        r = self.client.get(f"/api/byd/daily?start={DIA_A}&end={DIA_C}")
        self.assertEqual(r.status_code, 200)
        days = r.json()["days"]
        self.assertEqual([x["date"] for x in days], [DIA_A, DIA_B, DIA_C])
        self.assertEqual(days[0]["kwh_home"], 3.5)
        self.assertAlmostEqual(days[0]["cost_home"], 3.34, delta=0.01)
        self.assertEqual(days[1]["kwh_home"], 0.0)  # dia do meio vazio

    def test_daily_endpoint_days_default_still_works(self):
        r = self.client.get("/api/byd/daily?days=3")
        self.assertEqual(r.status_code, 200)
        days = r.json()["days"]
        self.assertEqual(len(days), 3)
        brt_today = (
            datetime.utcnow() - timedelta(hours=3)
        ).strftime("%Y-%m-%d")
        self.assertEqual(days[-1]["date"], brt_today)

    def test_validation_400s(self):
        cases = [
            f"/api/byd/trips?start={DIA_A}",                     # end faltando
            f"/api/byd/positions?start=04-11-2026&end={DIA_C}",  # formato
            f"/api/byd/trips?start={DIA_C}&end={DIA_A}",         # invertido
            "/api/byd/daily?start=2026-01-01&end=2026-04-30",    # > 92 dias
        ]
        for url in cases:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 400)


if __name__ == "__main__":
    unittest.main()
