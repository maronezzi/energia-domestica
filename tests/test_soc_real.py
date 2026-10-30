#!/usr/bin/env python3
"""
SOC real do BYD no /api/charge/state (exibição no Status do Carregamento).

Regras:
- Coleta fresca + SOC plausível (>1) → byd_soc presente (vence a estimativa
  η apenas na EXIBIÇÃO; auto-stop e âncora do dono continuam da estimativa).
- SOC 0 = sentinela da BYD (carro dormindo) → NUNCA exibido.
- Sem coleta fresca → sem byd_soc; o frontend cai na estimativa de sempre.

Run with:  python3 -m pytest tests/test_soc_real.py -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

_tmpdir = tempfile.mkdtemp(prefix="energia_socreal_test_")
os.makedirs(f"{_tmpdir}/data", exist_ok=True)


def _byd_status(soc, fresh=True, data_age_s=60):
    return {
        "soc_fresh": fresh,
        "snapshot": {"soc": soc, "odo_km": 29350.0 if soc else None},
        "data_age_s": data_age_s,
        "poll_age_s": 30 if fresh else None,
    }


class TestBydSocDisplay(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import dashboard
        self.dash = dashboard
        self._orig = (dashboard.DB_FILE, dashboard.CONFIG_FILE)
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
        self.dash.DB_FILE, self.dash.CONFIG_FILE = self._orig
        self.dash._cfg_cache = None

    def _state(self):
        r = self.client.get("/api/charge/state")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_soc_real_fresco_exposto(self):
        with patch.object(self.dash.byd_client, "status",
                          return_value=_byd_status(55.4)):
            st = self._state()
        self.assertEqual(st["byd_soc"], 55.4)
        self.assertEqual(st["byd_soc_age_min"], 1)

    def test_soc_100_real_exposto(self):
        """100 é valor real (carro cheio) — só a ÂNCORA rejeita, não a exibição."""
        with patch.object(self.dash.byd_client, "status",
                          return_value=_byd_status(100.0)):
            st = self._state()
        self.assertEqual(st["byd_soc"], 100.0)

    def test_sentinela_zero_nunca_exposto(self):
        """BYD manda SOC 0 com carro dormindo (provado em 21-22/10)."""
        with patch.object(self.dash.byd_client, "status",
                          return_value=_byd_status(0.0)):
            st = self._state()
        self.assertNotIn("byd_soc", st)

    def test_sem_coleta_fresca_nao_exposto(self):
        with patch.object(self.dash.byd_client, "status",
                          return_value=_byd_status(55.4, fresh=False)):
            st = self._state()
        self.assertNotIn("byd_soc", st)

    def test_soc_none_nao_exposto(self):
        with patch.object(self.dash.byd_client, "status",
                          return_value=_byd_status(None)):
            st = self._state()
        self.assertNotIn("byd_soc", st)


if __name__ == "__main__":
    unittest.main(verbosity=2)
