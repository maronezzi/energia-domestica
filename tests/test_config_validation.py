#!/usr/bin/env python3
"""
Regressão dos achados do enxame de validação (27/10/2026):

- POST /api/config aceitava qualquer tipo/valor: kwh_cost=1e999 (JSON válido,
  vira Infinity) derrubava /api/today e /api/status com 500 PERMANENTE até
  correção manual do tuya_config.json. Agora: 400, nada é salvo.
- /api/monthly e /api/daily-history com month=13/0 derrubavam com 500;
  mês futuro no daily-history dava IndexError (500) com a tabela mostrando
  o mês anterior. Agora: 400 nos inválidos, days=[] no mês futuro.
- /api/hourly com data inválida dava 500. Agora: 400.
- A trava da migração UTC→BRT vivia só no tuya_config.json: perdendo o
  arquivo, cada restart re-deslocava o histórico -3h de novo (erro composto).
  Agora há trava dupla dentro do próprio SQLite (app_meta).

Run with:  python3 -m pytest tests/test_config_validation.py -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

_tmpdir = tempfile.mkdtemp(prefix="energia_cfg_test_")
os.makedirs(f"{_tmpdir}/data", exist_ok=True)


def _cfg_on_disk(dash):
    """Config como está no ARQUIVO (load_config devolve defaults mesclados)."""
    if not dash.CONFIG_FILE.exists():
        return {}
    return json.loads(dash.CONFIG_FILE.read_text())


class TestConfigValidation(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import dashboard
        self.dash = dashboard
        self._orig_db = dashboard.DB_FILE
        self._orig_cfg = dashboard.CONFIG_FILE
        dashboard.DB_FILE = Path(_tmpdir) / "data" / "tuya_history.db"
        dashboard.CONFIG_FILE = Path(_tmpdir) / "data" / "tuya_config.json"
        for f in (dashboard.DB_FILE, dashboard.CONFIG_FILE):
            if f.exists():
                f.unlink()
        dashboard.init_db()
        dashboard._cfg_cache = None
        dashboard._ttl_cache.clear()
        self.client = TestClient(dashboard.app)

    def tearDown(self):
        self.dash.DB_FILE = self._orig_db
        self.dash.CONFIG_FILE = self._orig_cfg
        self.dash._cfg_cache = None
        self.dash._ttl_cache.clear()

    # ── POST /api/config ─────────────────────────────────────────

    def test_infinity_cost_rejected_and_nothing_saved(self):
        # corpo cru como o ataque real (httpx recusa serializar inf no cliente)
        r = self.client.post(
            "/api/config",
            content='{"kwh_cost": 1e999}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)
        # save_config grava os defaults mesclados: o que não pode acontecer
        # é o valor ruim chegar ao disco
        self.assertEqual(_cfg_on_disk(self.dash).get("kwh_cost"), 0.956)
        # o dashboard inteiro segue vivo (antes: 500 permanente)
        self.assertEqual(self.client.get("/api/today").status_code, 200)
        self.assertEqual(self.client.get("/api/status").status_code, 200)

    def test_nan_null_negative_wrongtype_cost_rejected(self):
        r = self.client.post(
            "/api/config",
            content='{"kwh_cost": NaN}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 400)
        for bad in (None, "caro", -5.0):
            r = self.client.post("/api/config", json={"kwh_cost": bad})
            self.assertEqual(r.status_code, 400, f"kwh_cost={bad!r} deveria ser 400")
        self.assertEqual(_cfg_on_disk(self.dash).get("kwh_cost"), 0.956)

    def test_valid_cost_accepted(self):
        r = self.client.post("/api/config", json={"kwh_cost": 0.956})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])
        self.assertAlmostEqual(self.dash.load_config()["kwh_cost"], 0.956)

    def test_tank_and_soc_bounds(self):
        self.assertEqual(
            self.client.post("/api/config", json={"car_tank_l": -48}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/config", json={"car_tank_l": 10000}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/config", json={"car_target_soc": 200}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/config", json={"car_tank_l": 48}).status_code, 200
        )

    def test_all_or_nothing(self):
        # um campo válido + um inválido: nada é salvo
        r = self.client.post(
            "/api/config", json={"kwh_cost": 0.9, "car_tank_l": -1}
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(_cfg_on_disk(self.dash).get("kwh_cost"), 0.956)

    # ── /api/monthly e /api/daily-history ────────────────────────

    def test_monthly_bad_month_400(self):
        for m in (0, 13, -3):
            r = self.client.get(f"/api/monthly?year=2026&month={m}")
            self.assertEqual(r.status_code, 400, f"month={m} deveria ser 400")

    def test_monthly_bad_year_400(self):
        self.assertEqual(
            self.client.get("/api/monthly?year=9999&month=12").status_code, 400
        )

    def test_daily_history_year_without_month_400(self):
        self.assertEqual(
            self.client.get("/api/daily-history?year=2026").status_code, 400
        )

    def test_daily_history_future_month_empty_not_500(self):
        r = self.client.get("/api/daily-history?year=2027&month=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["days"], [])

    def test_daily_history_valid_month_ok(self):
        r = self.client.get("/api/daily-history?year=2026&month=10")
        self.assertEqual(r.status_code, 200)

    # ── /api/hourly ──────────────────────────────────────────────

    def test_hourly_bad_date_400(self):
        for bad in ("2026-02-30", "lixo", "2027-02-29", "2026-2-3"):
            r = self.client.get(f"/api/hourly?date={bad}")
            self.assertEqual(r.status_code, 400, f"date={bad!r} deveria ser 400")
        # sem data segue = hoje
        self.assertEqual(self.client.get("/api/hourly").status_code, 200)

    # ── trava tz UTC→BRT sobrevive à perda do config ─────────────

    def test_tz_lock_survives_config_loss(self):
        conn = sqlite3.connect(self.dash.DB_FILE)
        try:
            conn.execute(
                "INSERT INTO readings (timestamp, device, power) "
                "VALUES ('2026-01-01T12:00:00', 'fase1', 1000.0)"
            )
            conn.commit()
            # perda do tuya_config.json: flag some, cache some
            self.dash.CONFIG_FILE.unlink()
            self.dash._cfg_cache = None
            self.dash._migrate_utc_to_brt(conn)
            ts = conn.execute(
                "SELECT timestamp FROM readings WHERE device='fase1'"
            ).fetchone()[0]
            self.assertEqual(
                ts, "2026-01-01T12:00:00",
                "leitura deslocada -3h de novo após perda do config",
            )
            # e o flag é re-backfillado no config recuperado
            self.assertTrue(self.dash.load_config().get("tz_brt_migrated"))
        finally:
            conn.close()

    def test_app_meta_lock_created(self):
        conn = sqlite3.connect(self.dash.DB_FILE)
        try:
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key='tz_brt_migrated'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "1")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()


class TestSessionCleanupOnAuthFailure(unittest.TestCase):
    """Sessão salva envenenada é removida; falha de rede a preserva."""

    def setUp(self):
        import byd_client as bc
        self.bc = bc
        self._orig = bc.SESSION_FILE
        bc.SESSION_FILE = Path(_tmpdir) / "data" / "byd_session.json"
        bc.SESSION_FILE.write_text("{}")

    def tearDown(self):
        self.bc.SESSION_FILE = self._orig
        if self.bc.SESSION_FILE.name == "byd_session.json" and self.bc.SESSION_FILE.exists() \
                and str(self.bc.SESSION_FILE).startswith(_tmpdir):
            self.bc.SESSION_FILE.unlink()

    def _exc(self, name):
        return type(name, (Exception,), {})(name)

    def test_crypto_failure_removes_file(self):
        # token salvo que falha na descriptografia → arquivo fora
        self.assertTrue(self.bc._session_cleanup_on_auth_failure(
            self._exc("BydCryptoError"), used_saved=True, proven=False))
        self.assertFalse(self.bc.SESSION_FILE.exists())

    def test_auth_failure_removes_file(self):
        self.assertTrue(self.bc._session_cleanup_on_auth_failure(
            self._exc("BydAuthenticationError"), used_saved=True, proven=False))
        self.assertFalse(self.bc.SESSION_FILE.exists())

    def test_transport_failure_keeps_file(self):
        # rede caiu: o token continua válido, nada é apagado
        self.assertFalse(self.bc._session_cleanup_on_auth_failure(
            self._exc("BydTransportError"), used_saved=True, proven=False))
        self.assertTrue(self.bc.SESSION_FILE.exists())

    def test_proven_session_keeps_file(self):
        # sessão já completou coletas: erro depois não é culpa do arquivo
        self.assertFalse(self.bc._session_cleanup_on_auth_failure(
            self._exc("BydCryptoError"), used_saved=True, proven=True))
        self.assertTrue(self.bc.SESSION_FILE.exists())

    def test_fresh_login_failure_keeps_file(self):
        # falha SEM sessão restaurada não tem arquivo para apagar
        self.assertFalse(self.bc._session_cleanup_on_auth_failure(
            self._exc("BydAuthenticationError"), used_saved=False, proven=False))
        self.assertTrue(self.bc.SESSION_FILE.exists())

    def test_missing_file_is_noop(self):
        self.bc.SESSION_FILE.unlink()
        self.assertFalse(self.bc._session_cleanup_on_auth_failure(
            self._exc("BydCryptoError"), used_saved=True, proven=False))
