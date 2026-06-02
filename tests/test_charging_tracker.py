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
        # Delivered 1.0 kWh => +7.75% SOC (1/12.9 * 100)
        t.update(current_energy_kwh=11.0, current_power_w=2000,
                 idle_power_w=15, idle_seconds_needed=120)
        self.assertAlmostEqual(t.effective_soc, 57.75, places=2)

    def test_target_reached_but_still_drawing_keeps_charging(self):
        t = self._new_tracker()
        t.start(50, 80, 12.9, 10.0)
        # Delivered 4.0 kWh => 50 + 31% = 81% (target reached)
        t.update(current_energy_kwh=14.0, current_power_w=2000,
                 idle_power_w=15, idle_seconds_needed=120)
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


class TestChargeSessionDB(unittest.TestCase):
    """Tests for charge_sessions DB helpers."""

    def setUp(self):
        import dashboard
        # Redirect to temp DB for this test
        self._orig_db = dashboard.DB_FILE
        dashboard.DB_FILE = Path(_tmpdir) / "data" / "tuya_history.db"
        # Start with a clean DB for each test
        if dashboard.DB_FILE.exists():
            dashboard.DB_FILE.unlink()
        dashboard.init_db()

    def tearDown(self):
        import dashboard
        dashboard.DB_FILE = self._orig_db

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
