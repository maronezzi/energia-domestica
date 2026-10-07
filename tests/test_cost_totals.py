#!/usr/bin/env python3
"""
Unit tests for the house cost totals: Custo Hoje / Custo Mês (Visão Geral),
Custo Total (Histórico) e Custo (Hora a Hora) somam fase1 + disjuntor do carro.

A casa e o carro são circuitos SEPARADOS (fase1 não inclui o carro), então
todo custo total da casa precisa cobrir os dois canais.

Run with:  python3 -m pytest tests/ -v
or:        python3 tests/test_cost_totals.py
"""
import sys
import os
import datetime
import tempfile
from pathlib import Path
from datetime import timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import unittest

_tmpdir = tempfile.mkdtemp(prefix="energia_cost_test_")
os.makedirs(f"{_tmpdir}/data", exist_ok=True)
TARIFF = 0.956  # default de kwh_cost no config


class TestCostTotals(unittest.TestCase):
    """db_today_stats / db_monthly_stats / db_hourly com DB descartável."""

    def setUp(self):
        import dashboard
        self._orig_db = dashboard.DB_FILE
        self._orig_cfg = dashboard.CONFIG_FILE
        dashboard.DB_FILE = Path(_tmpdir) / "data" / "tuya_history.db"
        dashboard.CONFIG_FILE = Path(_tmpdir) / "data" / "tuya_config.json"
        if dashboard.DB_FILE.exists():
            dashboard.DB_FILE.unlink()
        if dashboard.CONFIG_FILE.exists():
            dashboard.CONFIG_FILE.unlink()
        dashboard.init_db()
        dashboard._cfg_cache = None
        # caches TTL de mês entre testes (chave = ("month_kwh*", mês corrente))
        dashboard._ttl_cache.clear()

    def tearDown(self):
        import dashboard
        dashboard.DB_FILE = self._orig_db
        dashboard.CONFIG_FILE = self._orig_cfg
        dashboard._cfg_cache = None
        dashboard._ttl_cache.clear()

    @staticmethod
    def _insert_readings(conn, start_dt, minutes, power_w, breaker_w, step_s=30):
        """Série de leituras 'fase1' com power (casa) e phase_c (carro)."""
        t = start_dt
        for _ in range(int(minutes * 60 / step_s)):
            conn.execute(
                "INSERT INTO readings (timestamp, device, power, phase_c)"
                " VALUES (?, 'fase1', ?, ?)",
                (t.isoformat(), power_w, breaker_w),
            )
            t += timedelta(seconds=step_s)
        conn.commit()

    def test_hourly_cost_sums_house_and_car(self):
        """Custo do Hora a Hora = (kWh casa + kWh carro) × tarifa."""
        import dashboard
        day = datetime.datetime(2026, 9, 15, 0, 0, 0)
        conn = dashboard.get_db()
        try:
            # 2h: casa 1000W contínuo (2 kWh) + carro 2000W na 2ª hora (2 kWh)
            self._insert_readings(conn, day, 60, 1000, 0)
            self._insert_readings(conn, day + timedelta(hours=1), 60, 1000, 2000)
        finally:
            conn.close()
        d = dashboard.db_hourly("2026-09-15")
        self.assertAlmostEqual(d["total_kwh"], 2.0, delta=0.05)
        self.assertAlmostEqual(d["breaker_kwh"], 2.0, delta=0.05)
        self.assertAlmostEqual(
            d["total_cost"], (d["total_kwh"] + d["breaker_kwh"]) * TARIFF, delta=0.01
        )
        self.assertAlmostEqual(d["total_cost"], 4.0 * TARIFF, delta=0.1)

    def test_hourly_cost_zero_day(self):
        """Dia sem leitura nenhuma: custo 0, sem exceção."""
        import dashboard
        d = dashboard.db_hourly("2026-09-16")
        self.assertEqual(d["total_kwh"], 0)
        self.assertEqual(d["total_cost"], 0)

    def test_monthly_cost_sums_house_and_car(self):
        """Custo Total do Histórico = (fase1 + breaker) × tarifa via snapshots."""
        import dashboard
        now = datetime.datetime.now()
        prev = now.replace(day=1, hour=12) - timedelta(days=1)  # mês anterior
        snap_day = f"{prev.year:04d}-{prev.month:02d}-10"
        conn = dashboard.get_db()
        try:
            conn.execute(
                "INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, avg_power_w, created_at)"
                " VALUES (?, 'fase1', 10.0, 416.7, '2026-01-01T00:00:00')",
                (snap_day,),
            )
            conn.execute(
                "INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, created_at)"
                " VALUES (?, 'breaker', 5.0, '2026-01-01T00:00:00')",
                (snap_day,),
            )
            conn.commit()
        finally:
            conn.close()
        d = dashboard.db_monthly_stats(prev.year, prev.month)
        self.assertAlmostEqual(d["total_kwh"], 10.0, delta=0.01)
        self.assertAlmostEqual(d["breaker_kwh"], 5.0, delta=0.01)
        # 15.0 × 0.956 = 14.34 — se o carro ficasse de fora seria 9.56
        self.assertAlmostEqual(d["total_cost"], 14.34, delta=0.01)

    def test_today_and_month_cost_sums_house_and_car(self):
        """Custo Hoje / Custo Mês do Visão Geral incluem o disjuntor do carro."""
        import dashboard
        now = datetime.datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = now.replace(second=0, microsecond=0) - timedelta(minutes=90)
        # Madrugada (00:00–01:30): ancora em hoje 00:01 — as leituras podem
        # "ficar no futuro" alguns minutos, o range do dia não liga pra now.
        if start < midnight + timedelta(minutes=1):
            start = midnight + timedelta(minutes=1)
        conn = dashboard.get_db()
        try:
            # 1ª hora: só casa 1000W (1 kWh); meia hora: só carro 2000W (1 kWh)
            self._insert_readings(conn, start, 60, 1000, 0)
            self._insert_readings(
                conn, start + timedelta(minutes=60), 30, 0, 2000)
        finally:
            conn.close()
        td = dashboard.db_today_stats()
        self.assertAlmostEqual(td["today_kwh"], 1.0, delta=0.05)
        self.assertAlmostEqual(td["breaker_kwh"], 1.0, delta=0.05)
        self.assertAlmostEqual(
            td["today_cost"], (td["today_kwh"] + td["breaker_kwh"]) * TARIFF, delta=0.01
        )
        self.assertAlmostEqual(td["today_cost"], 2.0 * TARIFF, delta=0.1)
        # Mês corrente sem snapshots: integral ao vivo = mesmo total de hoje
        self.assertAlmostEqual(
            td["month_cost"], (td["month_kwh"] + td["month_breaker_kwh"]) * TARIFF,
            delta=0.01,
        )
        self.assertAlmostEqual(td["month_cost"], td["today_cost"], delta=0.15)

    def test_month_cost_without_readings_uses_snapshots_only(self):
        """Mês com só snapshots (sem leituras): custo soma breaker via snapshots."""
        import dashboard
        now = datetime.datetime.now()
        snap_day = now.strftime("%Y-%m-01")
        conn = dashboard.get_db()
        try:
            conn.execute(
                "INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, created_at)"
                " VALUES (?, 'fase1', 4.0, '2026-01-01T00:00:00')",
                (snap_day,),
            )
            conn.execute(
                "INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, created_at)"
                " VALUES (?, 'breaker', 6.0, '2026-01-01T00:00:00')",
                (snap_day,),
            )
            conn.commit()
        finally:
            conn.close()
        td = dashboard.db_today_stats()
        self.assertAlmostEqual(td["month_kwh"], 4.0, delta=0.01)
        self.assertAlmostEqual(td["month_breaker_kwh"], 6.0, delta=0.01)
        self.assertAlmostEqual(td["month_cost"], 10.0 * TARIFF, delta=0.01)


if __name__ == "__main__":
    unittest.main(verbosity=2)
