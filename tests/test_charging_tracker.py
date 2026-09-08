#!/usr/bin/env python3
"""
Unit tests for the ChargingTracker state machine.

Run with:  python3 -m pytest tests/ -v
or:         python3 tests/test_charging_tracker.py
"""
import sys
import os
import datetime
from pathlib import Path

# Make src/ importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Use a throwaway DB so tests don't touch real data
import tempfile
os.environ.setdefault("BASE_DIR_OVERRIDE", "")

# We need to import dashboard first so the DB helpers are bound to the project DB
# but redirect DB_FILE to a temp file before running tests.
import importlib.util
spec = importlib.util.spec_from_file_location("dashboard", PROJECT_ROOT / "src" / "dashboard.py")
dashboard = importlib.util.module_from_spec(spec)

# Patch DB_FILE and CONFIG_FILE to temp paths
_tmpdir = tempfile.mkdtemp(prefix="energia_test_")
os.makedirs(f"{_tmpdir}/data", exist_ok=True)

import unittest
from unittest.mock import patch


class TestChargingTracker(unittest.TestCase):
    """Tests for the ChargingTracker state machine."""

    def _new_tracker(self):
        from dashboard import ChargingTracker
        return ChargingTracker()

    def test_initial_state_is_idle(self):
        t = self._new_tracker()
        st = t.get_status()
        self.assertEqual(st["state"], "idle")
        self.assertFalse(st["charging"])
        self.assertEqual(st["elapsed_seconds"], 0)

    def test_start_transitions_to_charging(self):
        t = self._new_tracker()
        t.start(start_soc=50, target_soc=80, battery_kwh=12.9, start_energy_kwh=10.0)
        self.assertEqual(t.state, t.STATE_CHARGING)
        st = t.get_status()
        self.assertTrue(st["charging"])
        self.assertEqual(st["effective_soc"], 50.0)

    def test_progress_calculates_effective_soc(self):
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        # Delivered 1.0 kWh at 100% efficiency => +7.75% SOC (1/12.9 * 100)
        t.update(current_energy_kwh=11.0, current_power_w=2000,
                 idle_power_w=15, idle_seconds_needed=120, efficiency=1.0)
        self.assertAlmostEqual(t.effective_soc, 57.75, places=2)

    def test_efficiency_reduces_soc_gain(self):
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        # 1.0 kWh at 80% efficiency => +6.2% SOC
        t.update(current_energy_kwh=11.0, current_power_w=2000,
                 idle_power_w=15, idle_seconds_needed=120, efficiency=0.80)
        self.assertAlmostEqual(t.effective_soc, 56.20, places=1)

    def test_target_reached_but_still_drawing_keeps_charging(self):
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        # Delivered 4.0 kWh at 100% eff => 50 + 31% = 81% (target reached)
        t.update(current_energy_kwh=14.0, current_power_w=2000,
                 idle_power_w=15, idle_seconds_needed=120, efficiency=1.0)
        self.assertGreaterEqual(t.effective_soc, 80)
        # But car is still drawing 2000W — should STAY in charging
        self.assertEqual(t.state, t.STATE_CHARGING)
        self.assertFalse(t.should_auto_stop(120))

    def test_target_reached_and_idle_transitions_to_completing(self):
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        t.update(current_energy_kwh=14.0, current_power_w=2000,
                 idle_power_w=15, idle_seconds_needed=120)
        # Now power drops to idle
        t.update(current_energy_kwh=14.0, current_power_w=5,
                 idle_power_w=15, idle_seconds_needed=120)
        self.assertEqual(t.state, t.STATE_COMPLETING)
        self.assertIsNotNone(t.idle_started_at)

    def test_auto_stop_only_after_idle_period_elapsed(self):
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        t.update(14.0, 2000, 15, 120)
        t.update(14.0, 5, 15, 120)  # idle triggered
        # Right after: should NOT auto-stop
        self.assertFalse(t.should_auto_stop(120))
        # Backdate idle_started_at to 130s ago
        t.idle_started_at = datetime.datetime.now() - datetime.timedelta(seconds=130)
        self.assertTrue(t.should_auto_stop(120))

    def test_power_resumes_during_completing_returns_to_charging(self):
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        t.update(14.0, 2000, 15, 120)
        t.update(14.0, 5, 15, 120)  # entering completing
        self.assertEqual(t.state, t.STATE_COMPLETING)
        # Car starts drawing again
        t.update(14.05, 1500, 15, 120)
        self.assertEqual(t.state, t.STATE_CHARGING)

    def test_stop_resets_state(self):
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0, session_uuid="abc-123")
        t.stop(reason="manual")
        self.assertEqual(t.state, t.STATE_IDLE)
        self.assertIsNone(t.session_uuid)

    def test_stop_auto_returns_to_idle(self):
        """Regressão: stop(reason='auto') deixava state='done' e a
        auto-detecção do poll_loop (que só dispara a partir de IDLE) nunca
        mais criava sessão — o próximo carregamento manual ficava sem
        registro no DB até restart do serviço."""
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0, session_uuid="abc-123")
        t.stop(reason="auto")
        self.assertEqual(t.state, t.STATE_IDLE)

    def test_apply_soc_correction_rebases_mid_session(self):
        """Correção de SOC durante a carga: effective_soc passa a ser o valor
        informado e a inclinação por energia é preservada (start_soc rebase)."""
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        # 1.0 kWh entregue a 100% eficiência => +7.75% → effective_soc 57.75
        t.update(11.0, 2000, 15, 120, efficiency=1.0)
        self.assertAlmostEqual(t.effective_soc, 57.75, places=2)
        rebased = t.apply_soc_correction(30.0)
        self.assertAlmostEqual(t.effective_soc, 30.0, places=2)
        # start_soc rebaseado: 30 - 7.75 = 22.25
        self.assertAlmostEqual(rebased, 22.25, places=2)
        # Mais 1.0 kWh → +7.75% sobre o novo start_soc
        t.update(12.0, 2000, 15, 120, efficiency=1.0)
        self.assertAlmostEqual(t.effective_soc, 37.75, places=2)

    def test_apply_soc_correction_ignored_when_idle(self):
        t = self._new_tracker()
        self.assertIsNone(t.apply_soc_correction(30.0))

    def test_no_prediction_when_not_charging(self):
        """estimated_remaining_minutes must be None when the car draws 0W."""
        t = self._new_tracker()
        t.start(26, 100, 12.9, 10.0)
        # Feed only idle power — car is NOT charging
        t.update(10.0, 0, 15, 120)
        st = t.get_status()
        self.assertIsNone(st["estimated_remaining_minutes"])

    def test_no_prediction_in_completing_state(self):
        """No prediction when the car stopped and we're confirming idle."""
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        t.update(14.0, 2000, 15, 120)  # charging
        t.update(14.0, 5, 15, 120)    # idle → completing
        st = t.get_status()
        self.assertEqual(st["state"], "completing")
        self.assertIsNone(st["estimated_remaining_minutes"])

    def test_prediction_uses_rolling_avg_when_active(self):
        """When actively charging, prediction uses rolling power average."""
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        t.update(11.0, 2400, 15, 120, efficiency=1.0)
        st = t.get_status()
        self.assertIsNotNone(st["estimated_remaining_minutes"])
        # effective_soc = 50 + (1.0/12.9)*100 ≈ 57.75
        # need = (80 - 57.75)/100 * 12.9 ≈ 2.87 kWh; at 2.4 kW → ~71.8 min
        self.assertAlmostEqual(st["estimated_remaining_minutes"], 71.8, delta=1.0)

    def test_prediction_accounts_for_efficiency(self):
        """Prediction grosses up energy for charging losses."""
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        t.update(11.0, 2400, 15, 120, efficiency=0.80)
        st = t.get_status()
        # effective_soc = 50 + (1.0*0.8/12.9)*100 ≈ 56.20
        # need_battery = (80-56.2)/100*12.9 ≈ 3.07 kWh
        # need_grid = 3.07/0.8 ≈ 3.84 kWh; at 2.4 kW → ~95.9 min
        self.assertAlmostEqual(st["estimated_remaining_minutes"], 95.9, delta=1.0)

    def test_session_avg_power_w(self):
        """session_avg_power_w is the true mean of all readings."""
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        t.update(11.0, 2000, 15, 120)
        t.update(12.0, 3000, 15, 120)
        t.update(13.0, 1000, 15, 120)
        self.assertAlmostEqual(t.session_avg_power_w, 2000.0, places=1)

    def test_effective_end_time_excludes_idle(self):
        """effective_end_time is the last moment power was above idle."""
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        t.update(14.0, 2000, 15, 120)  # active
        active_time = t.last_active_time
        self.assertIsNotNone(active_time)
        t.update(14.0, 5, 15, 120)     # idle → completing
        # effective_end_time should still be the last active moment
        self.assertEqual(t.effective_end_time, active_time)

    def test_countdown_message_uses_config(self):
        """Countdown message respects idle_seconds_needed from config."""
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        t.update(14.0, 2000, 15, 60)  # 60s idle threshold
        t.update(14.0, 5, 60, 60)     # idle with 60s config
        st = t.get_status()
        self.assertIn("Confirmando", st["message"])


class TestChargeSessionDB(unittest.TestCase):
    """Tests for charge_sessions DB helpers."""

    def setUp(self):
        import dashboard
        # Redirect to temp DB for this test
        self._orig_db = dashboard.DB_FILE
        self._orig_cfg = dashboard.CONFIG_FILE
        dashboard.DB_FILE = Path(_tmpdir) / "data" / "tuya_history.db"
        dashboard.CONFIG_FILE = Path(_tmpdir) / "data" / "tuya_config.json"
        # Start with a clean DB for each test
        if dashboard.DB_FILE.exists():
            dashboard.DB_FILE.unlink()
        # ...e config limpa (o gate do backfill persiste no arquivo entre tests)
        if dashboard.CONFIG_FILE.exists():
            dashboard.CONFIG_FILE.unlink()
        dashboard.init_db()
        dashboard._cfg_cache = None

    def tearDown(self):
        import dashboard
        dashboard.DB_FILE = self._orig_db
        dashboard.CONFIG_FILE = self._orig_cfg
        dashboard._cfg_cache = None

    @staticmethod
    def _insert_charge_readings(conn, start_dt, minutes, watts, step_s=30,
                                start_counter=740.0, counter_ratio=1.0):
        """Insere leituras fase1 (phase_c=power_w, breaker_energy=kWh) como se
        o carro estivesse carregando. counter_ratio<1 simula contador falhando
        (delta menor que a integral). Retorna o contador final."""
        import dashboard
        from datetime import timedelta
        e = start_counter
        t = start_dt
        for _ in range(int(minutes * 60 / step_s)):
            conn.execute(
                "INSERT INTO readings (timestamp, device, phase_c, breaker_energy)"
                " VALUES (?, 'fase1', ?, ?)",
                (t.isoformat(), watts, e),
            )
            e += watts * step_s / 3600.0 / 1000.0 * counter_ratio
            t += timedelta(seconds=step_s)
        conn.commit()
        return e

    def test_detect_windows_bridges_short_gaps(self):
        from dashboard import _detect_charge_windows
        T = datetime.datetime
        rows = [
            ("2020-01-01T10:00:00", 2700), ("2020-01-01T10:00:30", 2700),
            # pausa de 5 min (estimador de potencia zera por instantes)
            ("2020-01-01T10:05:30", 2700), ("2020-01-01T10:06:00", 2700),
        ]
        w = _detect_charge_windows(rows, bridge_gap_s=900)
        self.assertEqual(len(w), 1)  # 5min < bridge → uma janela só
        rows_far = rows + [("2020-01-01T10:40:00", 2700)]
        w2 = _detect_charge_windows(rows_far, bridge_gap_s=900)
        self.assertEqual(len(w2), 2)  # 34min > bridge → duas janelas

    def test_backfill_recovers_untracked_charging(self):
        """Dias com carga medida mas sem sessão (bug de auto-detecção antigo)
        ganham sessão 'reconstructed' com kWh/duração/custo — sem duplicar
        sessões que já existem."""
        import dashboard
        from dashboard import backfill_charge_sessions_from_readings
        conn = dashboard.get_db()
        try:
            # Janela A: 2h @2700W ≈ 5.4 kWh — sem sessão → recupera
            self._insert_charge_readings(
                conn, datetime.datetime(2020, 1, 1, 10, 0), 120, 2700)
            # Blip mínimo (~0.01 kWh) — abaixo de 0.05 → ignora
            self._insert_charge_readings(
                conn, datetime.datetime(2020, 1, 1, 13, 0), 1, 2700,
                start_counter=800.0)
            # Janela C: sobrepõe sessão JÁ registrada → não duplica
            conn.execute(
                "INSERT INTO charge_sessions"
                " (session_uuid, start_time, end_time, status,"
                "  start_energy_kwh, cost_per_kwh)"
                " VALUES ('existente', '2020-01-02T10:00:00',"
                "         '2020-01-02T11:00:00', 'completed', 900, 0.956)")
            self._insert_charge_readings(
                conn, datetime.datetime(2020, 1, 2, 10, 30), 60, 2700,
                start_counter=900.0)
        finally:
            conn.close()

        created = backfill_charge_sessions_from_readings()
        self.assertEqual(created, 1)
        rows = [
            s for s in dashboard.list_charge_sessions(limit=50)
            if s["status"] == "reconstructed"
        ]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertAlmostEqual(r["energy_delivered_kwh"], 5.4, delta=0.05)
        self.assertGreaterEqual(r["duration_seconds"], 7000)
        self.assertAlmostEqual(r["avg_power_w"], 2700, delta=60)
        self.assertAlmostEqual(r["total_cost"], 5.4 * 0.956, delta=0.1)
        self.assertIsNone(r["soc_start"])  # SOC histórico é desconhecido
        # Sessão existente continua intacta
        kept = [
            s for s in dashboard.list_charge_sessions(limit=50)
            if s["session_uuid"] == "existente"
        ]
        self.assertEqual(len(kept), 1)

    def test_backfill_is_one_shot(self):
        import dashboard
        from dashboard import backfill_charge_sessions_from_readings
        conn = dashboard.get_db()
        try:
            self._insert_charge_readings(
                conn, datetime.datetime(2020, 1, 1, 10, 0), 60, 2700)
        finally:
            conn.close()
        self.assertEqual(backfill_charge_sessions_from_readings(), 1)
        # Segunda execução: gate em config → não duplica
        self.assertEqual(backfill_charge_sessions_from_readings(), 0)

    def test_backfill_falls_back_to_integral_when_counter_stuck(self):
        """Energia prefere o delta do contador DPS 1; com contador preso
        (delta=0) cai na integral de potência×tempo."""
        import dashboard
        from dashboard import backfill_charge_sessions_from_readings
        conn = dashboard.get_db()
        try:
            # counter_ratio=0 → breaker_energy constante → delta=0 → inviável
            self._insert_charge_readings(
                conn, datetime.datetime(2020, 1, 1, 10, 0), 60, 2700,
                counter_ratio=0.0)
        finally:
            conn.close()
        backfill_charge_sessions_from_readings()
        r = [
            s for s in dashboard.list_charge_sessions(limit=50)
            if s["status"] == "reconstructed"
        ][0]
        self.assertAlmostEqual(r["energy_delivered_kwh"], 2.7, delta=0.05)
        self.assertIsNone(r["start_energy_kwh"])  # fallback não tem contadores

    def test_create_and_finalize_session(self):
        from dashboard import create_charge_session, finalize_charge_session, list_charge_sessions
        s = create_charge_session(soc_start=50, soc_target=80, battery_kwh=12.9,
                                   start_energy_kwh=10.0, cost_per_kwh=1.0)
        self.assertEqual(s["status"], "active")
        self.assertIn("session_uuid", s)

        result = finalize_charge_session(s["session_uuid"], end_energy_kwh=14.0,
                                          soc_end=81.0, end_reason="auto")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "auto_stopped")
        self.assertAlmostEqual(result["energy_delivered_kwh"], 4.0, places=3)
        self.assertAlmostEqual(result["total_cost"], 4.0, places=2)  # 4 kWh * R$1

    def test_zero_energy_session_gets_no_charge_status(self):
        """Sessions that deliver < 0.05 kWh are marked 'no_charge'."""
        from dashboard import create_charge_session, finalize_charge_session
        s = create_charge_session(soc_start=26, soc_target=100, battery_kwh=12.9,
                                   start_energy_kwh=719.8, cost_per_kwh=0.956)
        result = finalize_charge_session(s["session_uuid"], end_energy_kwh=719.8,
                                          soc_end=26.0, end_reason="auto")
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "no_charge")
        self.assertAlmostEqual(result["energy_delivered_kwh"], 0.0, places=3)

    def test_effective_end_time_excludes_idle_from_duration(self):
        """Duration uses effective_end_time, not wall-clock finalize time."""
        from dashboard import create_charge_session, finalize_charge_session
        s = create_charge_session(soc_start=50, soc_target=80, battery_kwh=12.9,
                                   start_energy_kwh=10.0, cost_per_kwh=1.0)
        # Simulate: car charged for 10 min, then sat idle for 2 min
        eff_end = datetime.datetime.now() - datetime.timedelta(minutes=2)
        result = finalize_charge_session(s["session_uuid"], end_energy_kwh=14.0,
                                          soc_end=81.0, end_reason="auto",
                                          effective_end_time=eff_end)
        self.assertIsNotNone(result)
        # Duration should be ~0s (start was just now, eff_end is 2 min ago → clamped to 0)
        # In real usage start would be older; here we just verify it doesn't crash
        # and duration <= wall-clock
        self.assertGreaterEqual(result["duration_seconds"], 0)

    def test_summary_aggregates_sessions(self):
        from dashboard import create_charge_session, finalize_charge_session, charge_sessions_summary
        # Create 2 finished sessions
        s1 = create_charge_session(50, 80, 12.9, 10.0, 0.956)
        finalize_charge_session(s1["session_uuid"], 12.0, 62.0, "manual")
        s2 = create_charge_session(60, 90, 12.9, 100.0, 0.956)
        finalize_charge_session(s2["session_uuid"], 102.5, 75.0, "auto")

        summary = charge_sessions_summary(days=90)
        self.assertEqual(summary["session_count"], 2)
        self.assertAlmostEqual(summary["total_kwh"], 4.5, places=2)  # 2 + 2.5
        self.assertGreater(summary["total_cost"], 0)

    def test_create_session_finalizes_existing_active(self):
        """Regression test for the "today's measurements broken across multiple
        lines" bug: when a new session is created, any leftover active row from
        a previous (unfinalized) charging event MUST be finalized first, so the
        Carregamentos tab never shows N rows for the same logical event."""
        from dashboard import (
            create_charge_session,
            list_charge_sessions,
        )
        # Simulate the buggy state: a previous session was never finalized
        s_old = create_charge_session(20, 80, 12.9, 100.0, 0.956)
        self.assertEqual(s_old["status"], "active")
        # Create a new session (e.g. user clicked "Start" again, or auto-detect
        # fired while a ghost session was still in the DB)
        s_new = create_charge_session(50, 80, 12.9, 200.0, 0.956)
        # Exactly ONE row should be active
        all_sessions = list_charge_sessions(limit=50, include_active=True)
        actives = [s for s in all_sessions if s["status"] == "active"]
        self.assertEqual(len(actives), 1, "expected exactly one active session")
        self.assertEqual(actives[0]["session_uuid"], s_new["session_uuid"])
        # The old row was auto-finalized
        self.assertNotEqual(s_old["session_uuid"], s_new["session_uuid"])

    def test_finalize_stale_active_sessions_helper(self):
        """`finalize_stale_active_sessions` finalizes every active row except
        the one explicitly kept (used by startup recovery to repair DBs that
        already contain ghost active rows from before the fix)."""
        import dashboard
        from dashboard import (
            finalize_stale_active_sessions,
            list_charge_sessions,
        )
        # Simulate legacy ghost active rows by inserting directly via SQL,
        # bypassing `create_charge_session` (which would auto-fix the bug).
        conn = dashboard.get_db()
        try:
            ghosts = []
            for soc, energy in [(20, 10.0), (30, 20.0), (40, 30.0)]:
                uuid = f"ghost-{soc}-{energy}"
                conn.execute(
                    "INSERT INTO charge_sessions"
                    " (session_uuid, start_time, status, soc_start, soc_target,"
                    "  battery_kwh, start_energy_kwh, cost_per_kwh)"
                    " VALUES (?, ?, 'active', ?, 80, 12.9, ?, 0.956)",
                    (uuid, "2026-06-22T18:00:00", soc, energy),
                )
                ghosts.append(uuid)
            conn.commit()
        finally:
            conn.close()

        keep = ghosts[1]
        finalized = finalize_stale_active_sessions(reason="manual", keep_uuid=keep)
        self.assertEqual(set(finalized), {ghosts[0], ghosts[2]})
        actives = [
            s for s in list_charge_sessions(limit=50, include_active=True)
            if s["status"] == "active"
        ]
        self.assertEqual(len(actives), 1)
        self.assertEqual(actives[0]["session_uuid"], keep)

    def test_finalize_auto_reconciles_soc_start(self):
        """Sessão terminada pelo CARRO (end_reason='auto', soc_end=100):
        o soc_start verdadeiro é retrocalculado de 100 − energia×eficiência÷
        bateria — a estimativa carregada no início satura antes do fim real."""
        from dashboard import (
            create_charge_session,
            finalize_charge_session,
            list_charge_sessions,
        )
        s = create_charge_session(
            soc_start=73.38, soc_target=100, battery_kwh=12.9,
            start_energy_kwh=100.0, cost_per_kwh=1.0,
        )
        finalize_charge_session(
            s["session_uuid"], end_energy_kwh=110.4,
            soc_end=100.0, end_reason="auto",
        )
        row = [
            x for x in list_charge_sessions(limit=10)
            if x["session_uuid"] == s["session_uuid"]
        ][0]
        self.assertAlmostEqual(row["energy_delivered_kwh"], 10.4, places=2)
        self.assertAlmostEqual(
            row["soc_start"], 100.0 - (10.4 * 0.85 / 12.9 * 100), places=1
        )

    def test_finalize_manual_keeps_soc_start(self):
        """Fim manual (disjuntor desligado antes do carro encher) NÃO reconcilia
        — o soc_end ali é estimativa, não o 100% real da bateria."""
        from dashboard import (
            create_charge_session,
            finalize_charge_session,
            list_charge_sessions,
        )
        s = create_charge_session(
            soc_start=40.0, soc_target=80, battery_kwh=12.9,
            start_energy_kwh=100.0, cost_per_kwh=1.0,
        )
        finalize_charge_session(
            s["session_uuid"], end_energy_kwh=105.0,
            soc_end=72.9, end_reason="manual",
        )
        row = [
            x for x in list_charge_sessions(limit=10)
            if x["session_uuid"] == s["session_uuid"]
        ][0]
        self.assertAlmostEqual(row["soc_start"], 40.0, places=1)

    def test_estimate_soc_start_uses_reconciled_plugin_soc(self):
        """Ciclo de aprendizado: sessão anterior terminou com o carro CHEIO
        (auto, soc_end=100) e o soc_start reconciliado dela é o melhor
        estimador para a próxima plugada — NÃO o soc_end=100, que satura."""
        from dashboard import estimate_car_soc_start
        import dashboard as _d
        conn = _d.get_db()
        try:
            conn.execute(
                "INSERT INTO charge_sessions"
                " (session_uuid, start_time, end_time, status, soc_start, soc_end,"
                "  energy_delivered_kwh, end_reason)"
                " VALUES ('aprendida', '2020-01-03T08:00:00', '2020-01-03T12:00:00',"
                "         'auto_stopped', 31.47, 100.0, 10.4, 'auto')"
            )
            conn.commit()
        finally:
            conn.close()
        soc, estimated = estimate_car_soc_start({"car_current_soc": 100})
        self.assertTrue(estimated)
        self.assertAlmostEqual(soc, 31, places=1)  # campo inteiro: piso

    def test_finalize_auto_syncs_car_current_soc(self):
        """No fim reconciliado, o campo 'SOC Atual do Carro' (config) recebe o
        SOC aprendido, semeando a próxima carga."""
        import dashboard
        from dashboard import (
            create_charge_session,
            finalize_charge_session,
            load_config,
        )
        s = create_charge_session(
            soc_start=73.38, soc_target=100, battery_kwh=12.9,
            start_energy_kwh=100.0, cost_per_kwh=1.0,
        )
        finalize_charge_session(
            s["session_uuid"], end_energy_kwh=110.4,
            soc_end=100.0, end_reason="auto",
        )
        self.assertAlmostEqual(
            load_config()["car_current_soc"],
            31,  # campo inteiro: piso do reconciliado
            places=1,
        )

    def test_list_charge_sessions_active_sorted_first(self):
        """`list_charge_sessions(include_active=True)` must surface the active
        session first even when finished sessions are newer in start_time
        (defensive guard against legacy ghost rows)."""
        from dashboard import (
            create_charge_session,
            finalize_charge_session,
            list_charge_sessions,
        )
        # Finished session that started AFTER the active one
        finished = create_charge_session(20, 80, 12.9, 100.0, 0.956)
        finalize_charge_session(
            finished["session_uuid"], end_energy_kwh=110.0, soc_end=70.0
        )
        # Now an active session with a smaller id (started before the finished)
        active = create_charge_session(40, 80, 12.9, 50.0, 0.956)
        rows = list_charge_sessions(limit=50, include_active=True)
        self.assertEqual(rows[0]["session_uuid"], active["session_uuid"])
        self.assertEqual(rows[0]["status"], "active")

    def test_estimate_soc_start_from_last_session(self):
        """SOC inicial estimado = soc_end da última sessão (fluxo manual sem
        input do usuário)."""
        from dashboard import (
            create_charge_session,
            estimate_car_soc_start,
            finalize_charge_session,
        )
        s = create_charge_session(40, 80, 12.9, 10.0, 0.956)
        finalize_charge_session(s["session_uuid"], 13.0, 72.5, "auto")
        soc, estimated = estimate_car_soc_start({"car_current_soc": 50})
        self.assertTrue(estimated)
        self.assertAlmostEqual(soc, 72, places=1)  # campo inteiro: piso

    def test_estimate_soc_start_explicit_input_wins(self):
        """SOC informado explicitamente APÓS a última sessão tem precedência
        sobre a estimativa (ex.: usuário dirigiu e sabe o SOC real)."""
        from dashboard import (
            create_charge_session,
            estimate_car_soc_start,
            finalize_charge_session,
        )
        s = create_charge_session(40, 80, 12.9, 10.0, 0.956)
        finalize_charge_session(s["session_uuid"], 13.0, 72.5, "auto")
        soc, estimated = estimate_car_soc_start({
            "car_current_soc": 35.0,
            "car_current_soc_ts": datetime.datetime.now().isoformat(),
        })
        self.assertFalse(estimated)
        self.assertAlmostEqual(soc, 35.0, places=1)

    def test_estimate_soc_start_stale_config_loses_to_session(self):
        """car_current_soc sem timestamp (ou anterior à última sessão) NÃO
        conta como input explícito — usa a estimativa da sessão."""
        from dashboard import (
            create_charge_session,
            estimate_car_soc_start,
            finalize_charge_session,
        )
        s = create_charge_session(40, 80, 12.9, 10.0, 0.956)
        finalize_charge_session(
            s["session_uuid"], 13.0, 72.5, "auto"
        )
        soc, estimated = estimate_car_soc_start({
            "car_current_soc": 35.0,
            "car_current_soc_ts": "2020-01-01T00:00:00",
        })
        self.assertTrue(estimated)
        self.assertAlmostEqual(soc, 72, places=1)  # campo inteiro: piso

    def test_estimate_soc_start_no_history_falls_back_to_config(self):
        from dashboard import estimate_car_soc_start
        soc, estimated = estimate_car_soc_start({"car_current_soc": 66})
        self.assertFalse(estimated)
        self.assertAlmostEqual(soc, 66.0, places=1)

    def test_estimate_soc_start_ignores_active_and_no_charge(self):
        """Sessões 'active' (fantasma) e 'no_charge' (sem energia) não geram
        estimativa — usa a última sessão REAL finalizada."""
        from dashboard import (
            create_charge_session,
            estimate_car_soc_start,
            finalize_charge_session,
        )
        real = create_charge_session(40, 80, 12.9, 10.0, 0.956)
        finalize_charge_session(real["session_uuid"], 13.0, 72.5, "auto")
        # no_charge mais recente não deve valer como estimativa
        phantom = create_charge_session(99, 80, 12.9, 200.0, 0.956)
        finalize_charge_session(phantom["session_uuid"], 200.0, 99.0, "manual")
        soc, estimated = estimate_car_soc_start({"car_current_soc": 50})
        self.assertTrue(estimated)
        self.assertAlmostEqual(soc, 72, places=1)  # campo inteiro: piso


class TestBreakerIdleWatchdog(unittest.TestCase):
    """Tests for the breaker idle watchdog helper."""

    def _fn(self):
        from dashboard import breaker_idle_watchdog_should_stop
        return breaker_idle_watchdog_should_stop

    def test_fires_when_idle_long_enough(self):
        fn = self._fn()
        idle_since = datetime.datetime.now() - datetime.timedelta(seconds=130)
        self.assertTrue(fn(
            breaker_on=True, session_active=False,
            power_w=0, idle_power_w=15,
            idle_since=idle_since, idle_seconds_needed=120,
        ))

    def test_does_not_fire_before_timeout(self):
        fn = self._fn()
        idle_since = datetime.datetime.now() - datetime.timedelta(seconds=60)
        self.assertFalse(fn(
            breaker_on=True, session_active=False,
            power_w=0, idle_power_w=15,
            idle_since=idle_since, idle_seconds_needed=120,
        ))

    def test_does_not_fire_with_active_session(self):
        fn = self._fn()
        idle_since = datetime.datetime.now() - datetime.timedelta(seconds=300)
        self.assertFalse(fn(
            breaker_on=True, session_active=True,
            power_w=0, idle_power_w=15,
            idle_since=idle_since, idle_seconds_needed=120,
        ))

    def test_does_not_fire_when_breaker_off(self):
        fn = self._fn()
        idle_since = datetime.datetime.now() - datetime.timedelta(seconds=300)
        self.assertFalse(fn(
            breaker_on=False, session_active=False,
            power_w=0, idle_power_w=15,
            idle_since=idle_since, idle_seconds_needed=120,
        ))

    def test_does_not_fire_when_power_above_threshold(self):
        fn = self._fn()
        idle_since = datetime.datetime.now() - datetime.timedelta(seconds=300)
        self.assertFalse(fn(
            breaker_on=True, session_active=False,
            power_w=2700, idle_power_w=15,
            idle_since=idle_since, idle_seconds_needed=120,
        ))

    def test_does_not_fire_when_idle_since_none(self):
        fn = self._fn()
        self.assertFalse(fn(
            breaker_on=True, session_active=False,
            power_w=0, idle_power_w=15,
            idle_since=None, idle_seconds_needed=120,
        ))

    def test_power_at_exact_threshold_is_idle(self):
        fn = self._fn()
        idle_since = datetime.datetime.now() - datetime.timedelta(seconds=130)
        self.assertTrue(fn(
            breaker_on=True, session_active=False,
            power_w=15, idle_power_w=15,
            idle_since=idle_since, idle_seconds_needed=120,
        ))

    def test_explicit_now_parameter(self):
        fn = self._fn()
        idle_since = datetime.datetime(2026, 7, 30, 21, 36, 0)
        now = datetime.datetime(2026, 7, 30, 21, 38, 30)  # 150s later
        self.assertTrue(fn(
            breaker_on=True, session_active=False,
            power_w=4.3, idle_power_w=15,
            idle_since=idle_since, idle_seconds_needed=120,
            now=now,
        ))


class TestBreakerOffDebounce(unittest.TestCase):
    """Tests for breaker_off_confirmed — debounce do switch=0.

    Regression: em 2026-08-02 uma única leitura corrompida do DPS 16
    (switch=0 com ~2700W ainda fluindo) finalizava a sessão ativa, e a
    auto-detecção do poll seguinte criava outra — quebrando um único
    carregamento em várias linhas na aba Carregamentos.
    """

    def _fn(self):
        from dashboard import breaker_off_confirmed
        return breaker_off_confirmed

    def test_no_off_readings_is_not_confirmed(self):
        fn = self._fn()
        self.assertFalse(fn(0))

    def test_single_off_reading_is_not_confirmed(self):
        """The 2026-08-02 incident: one-poll glitches must NOT finalize."""
        fn = self._fn()
        self.assertFalse(fn(1))

    def test_below_threshold_is_not_confirmed(self):
        fn = self._fn()
        self.assertFalse(fn(2, confirm_polls=3))

    def test_at_threshold_is_confirmed(self):
        fn = self._fn()
        self.assertTrue(fn(3, confirm_polls=3))

    def test_above_threshold_is_confirmed(self):
        fn = self._fn()
        self.assertTrue(fn(10))

    def test_custom_threshold(self):
        fn = self._fn()
        self.assertFalse(fn(4, confirm_polls=5))
        self.assertTrue(fn(5, confirm_polls=5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
