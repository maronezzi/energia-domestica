#!/usr/bin/env python3
"""
Unit tests for the grid analyzer: apagões (boot-gap detection), eventos de
tensão ao vivo, backfill histórico e as APIs /api/outages / /api/power_quality.

Regra central: a placa morre JUNTO com a rede — reboot da placa depois da
última leitura é prova de apagão; lacuna de leitura com a placa de pé é
wifi/medidor e NÃO vira apagão.

Run with:  python3 -m pytest tests/ -v
or:        python3 tests/test_rede.py
"""
import sys
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

_tmpdir = tempfile.mkdtemp(prefix="energia_rede_test_")
os.makedirs(f"{_tmpdir}/data", exist_ok=True)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


class TestVoltageBands(unittest.TestCase):
    """Faixas ANEEL por tensão nominal."""

    def test_aneel_127v(self):
        import dashboard
        self.assertEqual(dashboard._voltage_bands(127), (117, 133, 103, 135))

    def test_aneel_220v(self):
        import dashboard
        self.assertEqual(dashboard._voltage_bands(220), (202, 231, 188, 233))

    def test_fallback_escala_127(self):
        import dashboard
        lo, hi, plo, phi = dashboard._voltage_bands(254)
        self.assertAlmostEqual(lo, 117 * 2, delta=0.6)
        self.assertAlmostEqual(hi, 133 * 2, delta=0.6)

    def test_zones_127v(self):
        import dashboard
        z = dashboard.voltage_zone
        self.assertEqual(z(102.9, 127), "critica")
        self.assertEqual(z(103, 127), "precaria")
        self.assertEqual(z(110, 127), "precaria")
        self.assertEqual(z(116.9, 127), "precaria")
        self.assertEqual(z(117, 127), "adequada")
        self.assertEqual(z(125.7, 127), "adequada")
        self.assertEqual(z(133, 127), "adequada")
        self.assertEqual(z(134, 127), "precaria")
        self.assertEqual(z(135, 127), "precaria")
        self.assertEqual(z(135.1, 127), "critica")


class RedeTestCase(unittest.TestCase):
    """Base: DB e config descartáveis."""

    def setUp(self):
        import dashboard
        self.d = dashboard
        self._orig = (dashboard.DB_FILE, dashboard.CONFIG_FILE)
        dashboard.DB_FILE = Path(_tmpdir) / "data" / f"db_{id(self)}.db"
        dashboard.CONFIG_FILE = Path(_tmpdir) / "data" / f"cfg_{id(self)}.json"
        for p in (dashboard.DB_FILE, dashboard.CONFIG_FILE):
            if p.exists():
                p.unlink()
        dashboard._cfg_cache = None
        dashboard.init_db()
        dashboard._ttl_cache.clear()

    def tearDown(self):
        import dashboard
        dashboard.DB_FILE, dashboard.CONFIG_FILE = self._orig
        dashboard._cfg_cache = None

    def _seed_reading(self, ts):
        conn = self.d.get_db()
        try:
            conn.execute(
                "INSERT INTO readings (timestamp, device, voltage, power) VALUES (?,?,?,?)",
                (_iso(ts), "fase1", 125.0, 100.0),
            )
            conn.commit()
        finally:
            conn.close()


class TestInsertOutage(RedeTestCase):
    def test_insert_e_campos(self):
        start = datetime.now() - timedelta(minutes=10)
        end = start + timedelta(minutes=5)
        conn = self.d.get_db()
        try:
            self.d._insert_outage(conn, "apagao", start, end, source="boot")
            row = conn.execute(
                "SELECT kind, start_time, end_time, duration_s, source FROM outages"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "apagao")
        self.assertEqual(row[3], 300)
        self.assertEqual(row[4], "boot")

    def test_fusao_eventos_colidados_mesmo_tipo(self):
        t0 = datetime.now() - timedelta(minutes=30)
        conn = self.d.get_db()
        try:
            self.d._insert_outage(conn, "subtensao", t0, t0 + timedelta(minutes=5),
                                  min_voltage=110.0)
            # novo evento começa 60s depois do fim do anterior → funde
            self.d._insert_outage(conn, "subtensao",
                                  t0 + timedelta(minutes=6), t0 + timedelta(minutes=9),
                                  min_voltage=108.0)
            rows = conn.execute("SELECT duration_s, min_voltage FROM outages").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], (9 - 0) * 60)
        self.assertEqual(rows[0][1], 108.0)  # mín das duas

    def test_sem_fusao_tipos_diferentes(self):
        t0 = datetime.now() - timedelta(minutes=30)
        conn = self.d.get_db()
        try:
            self.d._insert_outage(conn, "apagao", t0, t0 + timedelta(minutes=5))
            self.d._insert_outage(conn, "subtensao",
                                  t0 + timedelta(minutes=5, seconds=30),
                                  t0 + timedelta(minutes=8))
            rows = conn.execute("SELECT kind FROM outages").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)

    def test_sem_fusao_se_gap_grande(self):
        t0 = datetime.now() - timedelta(hours=3)
        conn = self.d.get_db()
        try:
            self.d._insert_outage(conn, "apagao", t0, t0 + timedelta(minutes=5))
            self.d._insert_outage(conn, "apagao",
                                  t0 + timedelta(minutes=30), t0 + timedelta(minutes=40))
            rows = conn.execute("SELECT id FROM outages").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)


class TestDetectOnStartup(RedeTestCase):
    """Reinício da placa depois da última leitura = apagão."""

    def test_boot_apos_ultima_leitura_registra_apagao(self):
        now = datetime.now()
        self._seed_reading(now - timedelta(minutes=10))
        boot = (now - timedelta(minutes=8)).timestamp()
        with patch.object(self.d, "board_boot_time", return_value=boot):
            self.d.detect_outage_on_startup()
        conn = self.d.get_db()
        try:
            rows = conn.execute(
                "SELECT kind, start_time, end_time, source FROM outages"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "apagao")
        self.assertEqual(rows[0][3], "boot")
        # fim = boot da placa (sem microssegundos), não agora
        self.assertEqual(
            datetime.fromisoformat(rows[0][2]).timestamp(),
            int(boot),
        )

    def test_placa_viva_nao_registra_nada(self):
        now = datetime.now()
        self._seed_reading(now - timedelta(minutes=10))  # gap, placa de pé
        boot = (now - timedelta(days=5)).timestamp()  # uptime velho (deploy/vigia)
        with patch.object(self.d, "board_boot_time", return_value=boot):
            self.d.detect_outage_on_startup()
        conn = self.d.get_db()
        try:
            rows = conn.execute("SELECT id FROM outages").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [])

    def test_evento_vivo_aberto_vira_apagao_quando_placa_morre(self):
        now = datetime.now()
        ev_start = now - timedelta(minutes=20)
        last = now - timedelta(minutes=8)  # placa morreu ~8min atrás
        boot = (now - timedelta(minutes=7)).timestamp()
        conn = self.d.get_db()
        try:
            conn.execute(
                "INSERT INTO outages (kind, start_time, end_time, source) "
                "VALUES ('subtensao', ?, NULL, 'live')",
                (_iso(ev_start),),
            )
            conn.commit()
        finally:
            conn.close()
        self._seed_reading(last)
        with patch.object(self.d, "board_boot_time", return_value=boot):
            self.d.detect_outage_on_startup()
        conn = self.d.get_db()
        try:
            rows = conn.execute(
                "SELECT kind, start_time, end_time FROM outages"
            ).fetchall()
        finally:
            conn.close()
        # promove a apagão, reancora o início na última leitura, fecha no
        # boot e NÃO cria segundo registro (lacuna já coberta)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "apagao")
        self.assertEqual(_iso(datetime.fromisoformat(rows[0][1])), _iso(last))
        self.assertEqual(
            datetime.fromisoformat(rows[0][2]).timestamp(), int(boot)
        )

    def test_evento_vivo_com_placa_de_pé_fecha_sem_promover(self):
        now = datetime.now()
        ev_start = now - timedelta(minutes=20)
        last = now - timedelta(minutes=1)  # serviço caiu 1min; placa de pé
        boot = (now - timedelta(days=30)).timestamp()
        conn = self.d.get_db()
        try:
            conn.execute(
                "INSERT INTO outages (kind, start_time, end_time, source) "
                "VALUES ('subtensao', ?, NULL, 'live')",
                (_iso(ev_start),),
            )
            conn.commit()
        finally:
            conn.close()
        self._seed_reading(last)
        with patch.object(self.d, "board_boot_time", return_value=boot):
            self.d.detect_outage_on_startup()
        conn = self.d.get_db()
        try:
            rows = conn.execute(
                "SELECT kind, start_time, end_time FROM outages"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "subtensao")  # não promove
        self.assertEqual(_iso(datetime.fromisoformat(rows[0][2])), _iso(last))


class TestBackfill(RedeTestCase):
    def _seed_gap(self, gap_minutes=20, end_offset_minutes=5):
        now = datetime.now()
        before = now - timedelta(minutes=gap_minutes + end_offset_minutes)
        after = now - timedelta(minutes=end_offset_minutes)
        self._seed_reading(before)
        self._seed_reading(after)
        return before, after

    def test_lacuna_com_boot_vira_apagao(self):
        before, after = self._seed_gap(20)
        boot = (before + timedelta(minutes=10)).timestamp()
        with patch.object(self.d, "_reboot_times", return_value=[datetime.fromtimestamp(boot)]):
            self.d.backfill_outages_from_readings()
        conn = self.d.get_db()
        try:
            rows = conn.execute(
                "SELECT kind, source, start_time, end_time FROM outages"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "apagao")
        self.assertEqual(rows[0][1], "backfill")
        self.assertEqual(
            datetime.fromisoformat(rows[0][3]).timestamp(), int(boot)
        )

    def test_lacuna_sem_boot_nao_vira_apagao(self):
        self._seed_gap(20)  # wifi caiu 20min, placa de pé
        with patch.object(self.d, "_reboot_times", return_value=[]):
            self.d.backfill_outages_from_readings()
        conn = self.d.get_db()
        try:
            rows = conn.execute("SELECT id FROM outages").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [])

    def test_sem_wtmp_vira_lacuna_e_nao_apagao(self):
        """Alpine não tem wtmp de boot: lacuna sem prova de reboot NUNCA é
        apagão (o dono flagrou dois falsos — crash-loop de deploy 24/10 e
        manobra de log 23/10). Fica como 'lacuna' e fora do resumo."""
        before, after = self._seed_gap(20)
        with patch.object(self.d, "_reboot_times", return_value=None):
            self.d.backfill_outages_from_readings()
        conn = self.d.get_db()
        try:
            rows = conn.execute("SELECT kind, end_time FROM outages").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "lacuna")
        self.assertEqual(_iso(datetime.fromisoformat(rows[0][1])), _iso(after))
        out = self.d._outages_payload(7)
        self.assertEqual(out["apagao_count"], 0)
        self.assertEqual(out["total_without_power_s"], 0)
        self.assertEqual(out["events"][0]["kind"], "lacuna")

    def test_lacuna_grande_demais_ignorada(self):
        self._seed_gap(60 * 24 * 5)  # 5 dias = sistema desligado, não apagão
        with patch.object(self.d, "_reboot_times", return_value=None):
            self.d.backfill_outages_from_readings()
        conn = self.d.get_db()
        try:
            rows = conn.execute("SELECT id FROM outages").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [])

    def test_gate_rodando_de_novo_nao_duplica(self):
        self._seed_gap(20)
        with patch.object(self.d, "_reboot_times", return_value=None):
            self.d.backfill_outages_from_readings()
            self.d._cfg_cache = None
            self.d.backfill_outages_from_readings()
        conn = self.d.get_db()
        try:
            rows = conn.execute("SELECT id FROM outages").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)


class TestApis(RedeTestCase):
    def test_outages_payload_resumo(self):
        now = datetime.now()
        conn = self.d.get_db()
        try:
            self.d._insert_outage(conn, "apagao",
                                  now - timedelta(hours=5), now - timedelta(hours=4, minutes=50))
            self.d._insert_outage(conn, "apagao",
                                  now - timedelta(hours=2), now - timedelta(hours=1, minutes=40))
            self.d._insert_outage(conn, "subtensao",
                                  now - timedelta(hours=1), now - timedelta(minutes=58),
                                  min_voltage=108.0)
            conn.commit()
        finally:
            conn.close()
        self._seed_reading(now - timedelta(days=3))
        out = self.d._outages_payload(30)
        self.assertEqual(out["apagao_count"], 2)
        self.assertEqual(out["total_without_power_s"], 600 + 1200)
        self.assertEqual(out["longest_apagao_s"], 1200)
        self.assertEqual(out["last_apagao"]["kind"], "apagao")
        self.assertEqual(len(out["events"]), 3)
        self.assertEqual(out["monitoring_since"], _iso(now - timedelta(days=3)))

    def test_evento_em_curto_conta_no_resumo(self):
        now = datetime.now()
        conn = self.d.get_db()
        try:
            conn.execute(
                "INSERT INTO outages (kind, start_time, end_time, duration_s, source) "
                "VALUES ('apagao', ?, NULL, NULL, 'boot')",
                (_iso(now - timedelta(minutes=3)),),
            )
            conn.commit()
        finally:
            conn.close()
        out = self.d._outages_payload(7)
        self.assertEqual(out["apagao_count"], 1)
        self.assertGreater(out["total_without_power_s"], 150)
        self.assertEqual(out["events"][0]["end_time"], None)

    def test_power_quality_faixas(self):
        now = datetime.now()
        conn = self.d.get_db()
        try:
            for v in (125.0, 130.0, 110.0, 100.0, 118.0):
                conn.execute(
                    "INSERT INTO readings (timestamp, device, voltage, power) VALUES (?,?,?,?)",
                    (_iso(now - timedelta(seconds=1)), "fase1", v, 0.0),
                )
            conn.commit()
        finally:
            conn.close()
        out = self.d._power_quality_payload(7)
        self.assertEqual(out["nominal_v"], 127)
        day = out["days"][0]
        self.assertEqual(day["samples"], 5)
        self.assertEqual(day["min_v"], 100.0)
        self.assertEqual(day["max_v"], 130.0)
        # 118, 125, 130 adequadas (3); 110 precária (1); 100 crítica (1)
        self.assertEqual(day["pct_adequada"], 60.0)
        self.assertEqual(day["pct_precaria"], 20.0)
        self.assertEqual(day["pct_critica"], 20.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
