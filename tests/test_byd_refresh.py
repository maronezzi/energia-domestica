#!/usr/bin/env python3
"""
Coleta sob demanda do BYD ("Atualizar") + POST /api/refresh.

- _sleep_or_force: o sleep do poller é interrompido pelo pedido (Event do
  próprio loop, sinalizado via call_soon_threadsafe — sem executor).
- request_poll_now: só quando o poller está VIVO; sem loop/não rodando →
  False honesto (a UI mostra o porquê no last_error da aba Carro).
- POST /api/refresh: re-lê os dois medidores Tuya, pede o poll BYD e NÃO
  limpa _ttl_cache (agregados caros — power_quality custa ~10s na placa;
  TTLs próprios cuidam da frescor).

Run with:  python3 -m pytest tests/test_byd_refresh.py -v
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

_tmpdir = tempfile.mkdtemp(prefix="energia_refresh_test_")
os.makedirs(f"{_tmpdir}/data", exist_ok=True)


class TestSleepOrForce(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import byd_client
        self.b = byd_client
        self._orig = (byd_client._force_poll, byd_client._byd_loop,
                      byd_client._st)
        self.b._st = {**self.b._st, "running": True}
        # Event/loop precisam existir DENTRO do loop do teste (como no
        # _run_loop real, que os cria no próprio loop dele)
        self.b._byd_loop = asyncio.get_running_loop()
        self.b._force_poll = asyncio.Event()

    async def asyncTearDown(self):
        self.b._force_poll, self.b._byd_loop, self.b._st = self._orig

    async def test_forca_interrompe_o_sleep(self):
        async def force_later():
            await asyncio.sleep(0.2)
            self.assertTrue(self.b.request_poll_now())
        task = asyncio.create_task(force_later())
        t0 = time.monotonic()
        forced = await self.b._sleep_or_force(30)
        await task
        self.assertTrue(forced)
        self.assertLess(time.monotonic() - t0, 5)

    async def test_sem_forca_espera_o_intervalo(self):
        t0 = time.monotonic()
        forced = await self.b._sleep_or_force(0.6)
        self.assertFalse(forced)
        self.assertGreaterEqual(time.monotonic() - t0, 0.55)

    async def test_evento_antes_do_sleep_vira_coleta_imediata(self):
        self.assertTrue(self.b.request_poll_now())
        t0 = time.monotonic()
        forced = await self.b._sleep_or_force(30)
        self.assertTrue(forced)
        self.assertLess(time.monotonic() - t0, 5)


class TestRequestPollNow(unittest.TestCase):
    def setUp(self):
        import byd_client
        self.b = byd_client
        self._orig = (byd_client._force_poll, byd_client._byd_loop,
                      byd_client._st)

    def tearDown(self):
        self.b._force_poll, self.b._byd_loop, self.b._st = self._orig

    def test_sem_loop_nao_faz_nada(self):
        self.b._force_poll = None
        self.b._byd_loop = None
        self.assertFalse(self.b.request_poll_now())

    def test_poller_morto_nao_faz_nada(self):
        self.b._st = {**self.b._st, "running": False}

        async def scenario():
            self.b._byd_loop = asyncio.get_running_loop()
            self.b._force_poll = asyncio.Event()
        asyncio.run(scenario())
        self.assertFalse(self.b.request_poll_now())


class TestApiRefresh(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        import dashboard
        self.d = dashboard
        self._orig = (dashboard.DB_FILE, dashboard.CONFIG_FILE)
        dashboard.DB_FILE = Path(_tmpdir) / "data" / f"db_{id(self)}.db"
        dashboard.CONFIG_FILE = Path(_tmpdir) / "data" / f"cfg_{id(self)}.json"
        for f in (dashboard.DB_FILE, dashboard.CONFIG_FILE):
            if f.exists():
                f.unlink()
        dashboard.init_db()
        dashboard._cfg_cache = None
        dashboard._ttl_cache.clear()
        self.client = TestClient(dashboard.app)

    def tearDown(self):
        self.d.DB_FILE, self.d.CONFIG_FILE = self._orig
        self.d._cfg_cache = None

    def test_refresh_rele_e_medidores_e_pede_byd(self):
        fake_dev = MagicMock()
        with patch.object(self.d, "connect_device", return_value=fake_dev), \
             patch.object(self.d, "read_fase1",
                          return_value={"voltage": 125.0, "power": 90.0}), \
             patch.object(self.d, "read_breaker",
                          return_value={"switch": False, "voltage_v": 219.0}), \
             patch.object(self.d.byd_client, "request_poll_now",
                          return_value=True) as req:
            r = self.client.post("/api/refresh")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["detail"]["fase1"], "ok")
        self.assertEqual(body["detail"]["breaker"], "ok")
        self.assertEqual(body["detail"]["byd"], "solicitado")
        req.assert_called_once()
        # as leituras caíram no state (o próximo /api/status já as vê)
        with self.d.state.lock:
            self.assertEqual(self.d.state.latest["fase1"]["voltage"], 125.0)
            self.assertEqual(self.d.state.latest["breaker"]["voltage_v"], 219.0)

    def test_refresh_sobrevive_a_medidor_fora_do_ar(self):
        with patch.object(self.d, "connect_device",
                          side_effect=Exception("timeout")), \
             patch.object(self.d.byd_client, "request_poll_now",
                          return_value=False):
            r = self.client.post("/api/refresh")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["success"])
        self.assertIn("erro", body["detail"]["fase1"])
        self.assertEqual(body["detail"]["byd"], "poller indisponível")


if __name__ == "__main__":
    unittest.main(verbosity=2)
