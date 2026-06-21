#!/usr/bin/env python3
"""
Tuya Energy Dashboard — Monitoramento de energia residencial
Roda em: http://localhost:8050

LOCAL-FIRST: Coleta local ativa, cloud opcional sob demanda do usuário.
"""

import json
import sqlite3
import threading
import time
import base64
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
import os

import tinytuya
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# ─── Config ────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent  # Project root (one level above src/)
DB_FILE = BASE_DIR / "data" / "tuya_history.db"
CONFIG_FILE = BASE_DIR / "data" / "tuya_config.json"

print(
    f"📁 Database: {DB_FILE} ({os.path.getsize(DB_FILE) if DB_FILE.exists() else 0} bytes)"
)


def _load_devices():
    """Load device credentials from data/devices.json. Falls back to placeholder."""
    devices_path = BASE_DIR / "data" / "devices.json"
    example_path = BASE_DIR / "src" / "devices.example.json"
    if devices_path.exists():
        with open(devices_path) as f:
            return json.load(f)
    if example_path.exists():
        print(
            f"⚠️  No data/devices.json found. Copy src/devices.example.json → data/devices.json and fill in your credentials."
        )
    return {}


DEVICES = _load_devices()

# DPS used to control the breaker (DPS 16 = circuit breaker switch, the real one).
# Note: tinytuya.turn_on() defaults to DPS 1, which on this breaker is the
# total_forward_energy_kwh counter — it does NOT toggle the relay.
# Reference: make-all/tuya-local#536 (Taxnele meter — same DPS layout)
#   DPS 11 = switch_prepayment (Prepay mode toggle — turns ON prepayment)
#   DPS 16 = switch (Circuit breaker — the actual relay)
#   DPS 13 = balance_energy (kWh balance, read-only)
#   DPS 9  = fault_code bitfield (65536 = no_balance alarm)
BREAKER_SWITCH_DPS = 16

# Tuya Cloud (OPTIONAL - only used if cloud_enabled in config)
# Credentials should be set in data/tuya_config.json, NOT here.
TUYA_REGION = "us"
TUYA_ACCESS_KEY = ""
TUYA_ACCESS_SECRET = ""

DEFAULT_CONFIG = {
    "kwh_cost": 0.956,
    "kwh_currency": "R$",
    "car_battery_kwh": 12.9,
    "car_charge_power_w": 2400,
    "car_target_soc": 80,
    "car_current_soc": 50,
    "car_charging": False,
    "car_charge_start_kwh": 0,  # Breaker energy counter at charge start
    "car_charge_start_time": None,  # ISO timestamp
    "car_charge_start_soc": None,  # SOC value at charge start
    "car_charge_idle_seconds_to_stop": 120,  # Wait this long with low power before auto-stop
    "car_charge_idle_power_w": 15,  # Power threshold to consider "idle/done"
    "car_charge_start_power_w": 500,  # Power threshold to auto-detect charging started
    "car_charge_auto_stop": True,  # Auto-stop when done
    "cloud_enabled": False,  # Cloud OFF by default - user decides
}
DB_MAX_ROWS = 200000


# ─── State (thread-safe) ────────────────────────────────────────
class State:
    def __init__(self):
        self.latest = {}
        self.lock = threading.Lock()

    def update(self, key, data):
        with self.lock:
            self.latest[key] = data


state = State()


# ─── DB ────────────────────────────────────────────────────────
def get_db():
    return sqlite3.connect(DB_FILE, check_same_thread=False)


# ─── Charge history (for prediction improvement) ──────────────
CHARGE_STATS_FILE = BASE_DIR / "data" / "charge_history.json"


def _save_charge_stats(session_stats: dict):
    """Append a completed charge session to the history file for future predictions."""
    try:
        history = []
        if CHARGE_STATS_FILE.exists():
            with open(CHARGE_STATS_FILE) as f:
                history = json.load(f)
        history.append(session_stats)
        # Keep last 50 sessions
        history = history[-50:]
        with open(CHARGE_STATS_FILE, "w") as f:
            json.dump(history, f, indent=2)
        print(
            f"📊 Sessão salva no histórico: {session_stats['energy_kwh']}kWh em {session_stats['duration_min']}min"
        )
    except Exception as e:
        print(f"⚠️ Erro ao salvar histórico: {e}")


def get_avg_charge_rate() -> float:
    """Return average charge rate (kWh/hour) from historical sessions. Fallback to config."""
    try:
        if CHARGE_STATS_FILE.exists():
            with open(CHARGE_STATS_FILE) as f:
                history = json.load(f)
            if history:
                rates = [
                    s["charge_rate_kwh_per_h"]
                    for s in history
                    if s.get("charge_rate_kwh_per_h", 0) > 0
                ]
                if rates:
                    return sum(rates) / len(rates)
    except Exception:
        pass
    cfg = load_config()
    return cfg.get("car_charge_power_w", 2400) / 1000  # fallback: config power


def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            device TEXT NOT NULL,
            -- fase1 (medidor de energia principal) fields:
            voltage REAL, current REAL, power REAL, energy REAL,
            -- breaker fields:
            breaker_switch INTEGER, breaker_prepay INTEGER,
            breaker_energy REAL, breaker_fault INTEGER,
            breaker_balance_kwh REAL, breaker_temperature REAL,
            -- phase currents (from breaker DPS 101/102/103, in mA)
            phase_a REAL, phase_b REAL, phase_c REAL
        )
    """)
    # Migrations: add new columns if upgrading from old schema
    cur_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(readings)").fetchall()
    }
    migrations = [
        ("breaker_prepay", "INTEGER"),
        ("breaker_fault", "INTEGER"),
        ("breaker_balance_kwh", "REAL"),
        ("breaker_temperature", "REAL"),
    ]
    for col, typ in migrations:
        if col not in cur_cols:
            conn.execute(f"ALTER TABLE readings ADD COLUMN {col} {typ}")
            print(f"DB migration: added column {col}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_time ON readings(device, timestamp)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            device TEXT NOT NULL,
            energy_kwh REAL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshot_date_dev ON daily_snapshots(snapshot_date, device)"
    )
    # ── Charge sessions (each car-charging session) ──
    conn.execute("""
        CREATE TABLE IF NOT EXISTS charge_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_uuid TEXT UNIQUE NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            status TEXT NOT NULL,  -- 'active', 'completed', 'auto_stopped', 'aborted'
            soc_start REAL,
            soc_end REAL,
            soc_target REAL,
            battery_kwh REAL,
            start_energy_kwh REAL,  -- breaker energy counter at start
            end_energy_kwh REAL,
            energy_delivered_kwh REAL,
            duration_seconds INTEGER,
            avg_power_w REAL,
            cost_per_kwh REAL,
            total_cost REAL,
            end_reason TEXT  -- 'manual', 'auto', 'fault', 'user', etc.
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_charge_start ON charge_sessions(start_time DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_charge_status ON charge_sessions(status)"
    )
    conn.commit()
    conn.close()


def prune_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        if count > DB_MAX_ROWS:
            excess = count - DB_MAX_ROWS
            conn.execute(
                f"DELETE FROM readings WHERE id IN (SELECT id FROM readings ORDER BY id LIMIT {excess})"
            )
            conn.commit()
            print(f"DB pruned: removed {excess} rows")
        conn.close()
    except Exception as e:
        print(f"DB prune error: {e}")


init_db()


# ─── Config ────────────────────────────────────────────────────
def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# ─── Tuya Cloud (disabled by default) ─────────────────────────────────
_cached_cloud = None
_cloud_cache = {}


def get_cloud():
    global _cached_cloud
    if _cached_cloud is None:
        _cached_cloud = tinytuya.Cloud(
            apiRegion=TUYA_REGION,
            apiKey=TUYA_ACCESS_KEY,
            apiSecret=TUYA_ACCESS_SECRET,
        )
    return _cached_cloud


def get_cloud_logs(device_id, days=2, use_cache=True):
    """Cloud fetch - only used when cloud_enabled=True"""
    cfg = load_config()
    if not cfg.get("cloud_enabled", False):
        return []

    key = f"{device_id}_{days}"
    now = time.time()
    if use_cache and key in _cloud_cache:
        ts, data = _cloud_cache[key]
        if now - ts < 600:
            return data

    try:
        cloud = get_cloud()
        result = cloud.getdevicelog(device_id, days)
        logs = result.get("result", {}).get("logs", [])
        _cloud_cache[key] = (now, logs)
        print(f"☁️ Cloud: {len(logs)} logs for {device_id} ({days}d)")
        return logs
    except Exception as e:
        print(f"Cloud error: {e}")
        return []


# ─── Device reads ──────────────────────────────────────────────
def connect_device(cfg):
    return tinytuya.Device(
        cfg["id"], address=cfg["ip"], local_key=cfg["key"], version=cfg["version"]
    )


def _to_num(v, default=0):
    """Safely coerce a Tuya DPS value (often int/str/None) to float."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def read_fase1(d):
    """Read fase1 meter with retry + validation.

    Returns dict on success, or None if all 3 retries fail to produce a valid
    voltage (real voltage is 110-130V; <=50V means bad read). Energy (DPS 17)
    is unreliable and often None — treated as optional (returned as None, not
    a skip trigger).
    """
    last_err = None
    for attempt in range(3):
        try:
            dps = d.status().get("dps", {})
            v_raw = _to_num(dps.get("20", 0))
            i_raw = _to_num(dps.get("18", 0))
            p_raw = _to_num(dps.get("19", 0))
            e_raw = dps.get("17")  # may be None — kept optional

            voltage = round(v_raw / 10, 1) if v_raw > 100 else 0
            if voltage > 50:
                power = round(p_raw / 10, 1)
                if power == 0:
                    print(
                        f"⚠️  fase1: voltage OK ({voltage}V) but power=0 "
                        f"— idle state or bad read"
                    )
                return {
                    "voltage": voltage,
                    "current": round(i_raw / 1000, 3),
                    "power": power,
                    "energy": round(_to_num(e_raw) / 1000, 4)
                    if e_raw is not None
                    else None,
                }
            last_err = f"voltage={voltage}V (expected >50V)"
        except Exception as e:
            last_err = str(e)
        if attempt < 2:
            time.sleep(1)
    print(f"⚠️  fase1: skipping reading after 3 attempts — {last_err}")
    return None


_last_valid_energy_wh = 0  # cache for DPS 1 communication errors


def read_breaker(d):
    """
    Read breaker status including voltage, current, power from DPS 6.

    DPS layout (protocol 4, base64-encoded in DPS 6):
      bytes 0-1: voltage in 0.1V (big-endian uint16, divide by 10 for V)
      bytes 3-4: current in mA   (big-endian uint16, divide by 1000 for A)
      bytes 5-6: (not reliable for power — always reads ~10)
      bytes 7-8: (fluctuating — purpose unclear)

    DPS 1: cumulative energy counter in Wh (sometimes returns 0 on comms error)
    DPS 9: fault bitmap
    DPS 11: prepay switch
    DPS 13: balance
    DPS 16: breaker switch
    DPS 101-104: alarm thresholds (overvoltage V, undervoltage V, temp °C, leakage mA)
    """
    import base64 as _b64

    global _last_valid_energy_wh
    dps = d.status().get("dps", {})
    energy_wh = _to_num(dps.get("1", 0))
    # Fallback: DPS 1 sometimes returns 0 on comms error
    if energy_wh > 0:
        _last_valid_energy_wh = energy_wh
    else:
        energy_wh = _last_valid_energy_wh
    # DPS 1 scale=2 per Tuya spec: divide by 100 for kWh
    # (each raw unit = 10 Wh = 0.01 kWh)

    # Read voltage & current from DPS 6 (updatedps returns protocol 4 data)
    voltage_v = 0.0
    current_a = 0.0
    try:
        result = d.updatedps()
        dps6_b64 = result.get("dps", {}).get("6", "")
        if dps6_b64:
            raw = _b64.b64decode(dps6_b64)
            if len(raw) >= 5:
                voltage_v = (raw[0] * 256 + raw[1]) / 10.0
                current_a = (raw[3] * 256 + raw[4]) / 1000.0
    except Exception:
        pass  # fallback: voltage/current stay 0

    power_w = round(voltage_v * current_a, 1)  # V × A = W

    return {
        "switch": bool(dps.get("16", False)),
        "prepayment": bool(dps.get("11", False)),
        "balance_kwh": round(_to_num(dps.get("13", 0)) / 100, 2),
        "energy_kwh": round(energy_wh, 2) if energy_wh else 0,  # raw counter = kWh
        "energy_wh": energy_wh,
        "fault_code": _to_num(dps.get("9", 0)),
        "voltage_v": round(voltage_v, 1),
        "current_a": round(current_a, 3),
        "power_w": power_w,
        # Alarm thresholds (not real-time readings)
        "alarm_overvoltage_v": _to_num(dps.get("101", 0)),
        "alarm_undervoltage_v": _to_num(dps.get("102", 0)),
        "alarm_temperature_c": _to_num(dps.get("103", 0)),
        "alarm_leakage_ma": _to_num(dps.get("104", 0)),
    }


# ─── DB saves ──────────────────────────────────────────────────
def save_reading(f1, br):
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO readings
               (timestamp, device, voltage, current, power, energy,
                breaker_switch, breaker_prepay, breaker_energy, breaker_fault,
                breaker_balance_kwh, breaker_temperature,
                phase_a, phase_b, phase_c)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now().isoformat(),
                "fase1",
                f1.get("voltage"),
                f1.get("current"),
                f1.get("power"),
                f1.get("energy"),
                1 if br.get("switch") else 0,
                1 if br.get("prepayment") else 0,
                br.get("energy_kwh"),
                br.get("fault_code"),
                br.get("balance_kwh"),
                br.get("alarm_temperature_c"),
                br.get("voltage_v"),
                br.get("current_a"),
                br.get("power_w"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _kwh_from_power_integral(rows):
    """Integrate power (W) over time (s) from a list of (timestamp, power_w) rows.
    Sanity-cap gaps at 120s to ignore overnight disconnects."""
    total = 0.0
    for i in range(1, len(rows)):
        t1, p1 = rows[i - 1]
        t2, p2 = rows[i]
        dt_s = (datetime.fromisoformat(t2) - datetime.fromisoformat(t1)).total_seconds()
        if 0 < dt_s < 120:
            avg_w = (p1 + p2) / 2
            if avg_w > 0:
                total += (avg_w / 1000) * (dt_s / 3600)
    return total


def _backfill_snapshots_from_readings():
    """Reconstruct daily_snapshots from existing power readings (fase1 + breaker).
    Idempotent via gate `snapshots_backfilled` in config — runs once per install.
    On first run, replaces legacy placeholders (0.001) and cumulative-counter
    artifacts (>= 100 kWh/day on breaker) with real power×time integrals.
    """
    _cfg = load_config()
    if _cfg.get("snapshots_backfilled"):
        return  # already done
    will_mark_done = True

    conn = get_db()
    try:
        # Aggregate power per day for fase1
        f1_rows = conn.execute(
            "SELECT DATE(timestamp) AS day, timestamp, power FROM readings "
            "WHERE device='fase1' AND power IS NOT NULL ORDER BY timestamp"
        ).fetchall()
        br_counter_rows = conn.execute(
            "SELECT DATE(timestamp) AS day, breaker_energy FROM readings "
            "WHERE device='fase1' AND breaker_energy IS NOT NULL ORDER BY timestamp"
        ).fetchall()

        # Group by day
        from collections import defaultdict

        f1_by_day = defaultdict(list)
        for day, ts, p in f1_rows:
            f1_by_day[day].append((ts, p or 0))
        br_counter_by_day = defaultdict(list)
        for day, val in br_counter_rows:
            if val is not None:
                br_counter_by_day[day].append(val)

        days = sorted(set(f1_by_day.keys()) | set(br_counter_by_day.keys()))
        # Include today too (in-progress day). The poll_loop will close it properly at next-day rollover
        # and overwrite if needed (but we want a real number for the current day's chart bar).
        n_f1, n_br = 0, 0
        for day in days:
            f1_kwh = (
                round(_kwh_from_power_integral(f1_by_day[day]), 4)
                if f1_by_day[day]
                else 0
            )
            vals = br_counter_by_day.get(day, [])
            br_kwh = (
                round(max(vals) - min(vals), 4)
                if vals and len(vals) >= 2
                else 0
            )
            if br_kwh > 50:
                print(f"⚠️ Breaker energy {br_kwh} kWh on {day} exceeds 50 kWh sanity cap — setting to 0")
                br_kwh = 0

            if f1_kwh > 0:
                existing = conn.execute(
                    "SELECT energy_kwh FROM daily_snapshots WHERE snapshot_date=? AND device='fase1'",
                    (day,),
                ).fetchone()
                # Treat existing=0 (or <0.01, the legacy placeholder) as missing
                is_placeholder = existing is None or (
                    existing[0] is not None and existing[0] < 0.01
                )
                if is_placeholder:
                    if existing is None:
                        conn.execute(
                            "INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, created_at) VALUES (?, 'fase1', ?, ?)",
                            (day, f1_kwh, datetime.now().isoformat()),
                        )
                    else:
                        conn.execute(
                            "UPDATE daily_snapshots SET energy_kwh=?, created_at=? WHERE snapshot_date=? AND device='fase1'",
                            (f1_kwh, datetime.now().isoformat(), day),
                        )
                n_f1 += 1

            if br_kwh > 0:
                existing_br = conn.execute(
                    "SELECT energy_kwh FROM daily_snapshots WHERE snapshot_date=? AND device='breaker'",
                    (day,),
                ).fetchone()
                # Treat suspiciously large breaker values (>= 100 kWh/day) as cumulative-counter artifacts.
                # If we have a real integral, ALWAYS overwrite these artifacts (even if 0 kWh — means "no car charging that day").
                is_cumulative_artifact = (
                    existing_br is not None
                    and existing_br[0] is not None
                    and existing_br[0] >= 100
                )
                is_placeholder_br = (
                    existing_br is None
                    or (existing_br[0] is not None and existing_br[0] < 0.01)
                    or is_cumulative_artifact
                )
                if is_placeholder_br:
                    if existing_br is None:
                        conn.execute(
                            "INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, created_at) VALUES (?, 'breaker', ?, ?)",
                            (day, br_kwh, datetime.now().isoformat()),
                        )
                    else:
                        conn.execute(
                            "UPDATE daily_snapshots SET energy_kwh=?, created_at=? WHERE snapshot_date=? AND device='breaker'",
                            (br_kwh, datetime.now().isoformat(), day),
                        )
                n_br += 1
            else:
                # No real integral data for breaker that day — but if legacy shows a
                # cumulative artifact (>= 100 kWh/day), zero it out so history isn't polluted.
                existing_br = conn.execute(
                    "SELECT energy_kwh FROM daily_snapshots WHERE snapshot_date=? AND device='breaker'",
                    (day,),
                ).fetchone()
                if (
                    existing_br is not None
                    and existing_br[0] is not None
                    and existing_br[0] >= 100
                ):
                    conn.execute(
                        "UPDATE daily_snapshots SET energy_kwh=0, created_at=? WHERE snapshot_date=? AND device='breaker'",
                        (datetime.now().isoformat(), day),
                    )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0]
        print(
            f"📸 Backfill inicial: {n_f1} dias fase1, {n_br} dias breaker. Tabela: {total} linhas"
        )

        # Mark as done
        if will_mark_done:
            _cfg["snapshots_backfilled"] = True
            save_config(_cfg)
    except Exception as e:
        print(f"⚠️ Backfill error: {e}")
    finally:
        conn.close()


# ─── Charge session DB helpers ─────────────────────────────────
import uuid as _uuid


def create_charge_session(
    soc_start, soc_target, battery_kwh, start_energy_kwh, cost_per_kwh
):
    """Insert a new active charge session. Returns the session dict (with id, uuid)."""
    session_uuid = str(_uuid.uuid4())
    now = datetime.now().isoformat()
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO charge_sessions
               (session_uuid, start_time, status, soc_start, soc_target, battery_kwh,
                start_energy_kwh, cost_per_kwh)
               VALUES (?, ?, 'active', ?, ?, ?, ?, ?)""",
            (
                session_uuid,
                now,
                soc_start,
                soc_target,
                battery_kwh,
                start_energy_kwh,
                cost_per_kwh,
            ),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "session_uuid": session_uuid,
            "start_time": now,
            "status": "active",
            "soc_start": soc_start,
            "soc_target": soc_target,
            "battery_kwh": battery_kwh,
            "start_energy_kwh": start_energy_kwh,
            "cost_per_kwh": cost_per_kwh,
        }
    finally:
        conn.close()


def update_charge_session_progress(
    session_uuid, current_energy_kwh, current_soc, duration_seconds, avg_power_w
):
    """Update an in-progress session with the latest readings (called periodically)."""
    conn = get_db()
    try:
        # Look up start_energy_kwh so we can compute the delivered-energy delta
        row = conn.execute(
            "SELECT start_energy_kwh FROM charge_sessions WHERE session_uuid = ?",
            (session_uuid,),
        ).fetchone()
        if not row:
            return
        start_energy = row[0] or 0
        energy_delivered = max(0.0, current_energy_kwh - start_energy)
        conn.execute(
            """UPDATE charge_sessions
               SET energy_delivered_kwh = ?, soc_end = ?, duration_seconds = ?, avg_power_w = ?
               WHERE session_uuid = ?""",
            (
                energy_delivered,
                current_soc,
                duration_seconds,
                avg_power_w,
                session_uuid,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def finalize_charge_session(session_uuid, end_energy_kwh, soc_end, end_reason="manual"):
    """Mark a charge session as finished. Computes totals. Saves stats for future predictions."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT start_time, start_energy_kwh, cost_per_kwh, battery_kwh, soc_start
               FROM charge_sessions WHERE session_uuid = ?""",
            (session_uuid,),
        ).fetchone()
        if not row:
            return None
        start_time_str, start_energy, cost_per_kwh, battery_kwh, soc_start = row
        start_dt = datetime.fromisoformat(start_time_str)
        end_dt = datetime.now()
        duration = int((end_dt - start_dt).total_seconds())
        energy_delivered = max(0.0, end_energy_kwh - (start_energy or 0))
        # Avoid double-counting: also recompute soc_end from energy if not provided
        if soc_end is None and battery_kwh:
            soc_end = (soc_start or 0) + (
                energy_delivered / max(0.1, battery_kwh)
            ) * 100
            soc_end = min(100.0, soc_end)
        total_cost = energy_delivered * (cost_per_kwh or 0)
        status = (
            "auto_stopped"
            if end_reason == "auto"
            else ("aborted" if end_reason == "fault" else "completed")
        )
        conn.execute(
            """UPDATE charge_sessions
               SET end_time = ?, end_energy_kwh = ?, energy_delivered_kwh = ?,
                   duration_seconds = ?, soc_end = ?, total_cost = ?, end_reason = ?, status = ?
               WHERE session_uuid = ?""",
            (
                end_dt.isoformat(),
                end_energy_kwh,
                energy_delivered,
                duration,
                soc_end,
                total_cost,
                end_reason,
                status,
                session_uuid,
            ),
        )
        conn.commit()

        # ── Save charge stats for future predictions ──
        if duration > 60 and energy_delivered > 0.1:
            _save_charge_stats(
                {
                    "date": end_dt.strftime("%Y-%m-%d"),
                    "soc_start": soc_start,
                    "soc_end": round(soc_end, 1),
                    "energy_kwh": round(energy_delivered, 3),
                    "duration_min": round(duration / 60, 1),
                    "avg_power_w": round(energy_delivered / (duration / 3600) * 1000, 0)
                    if duration > 0
                    else 0,
                    "charge_rate_kwh_per_h": round(
                        energy_delivered / (duration / 3600), 2
                    )
                    if duration > 0
                    else 0,
                    "end_reason": end_reason,
                }
            )

        return {
            "session_uuid": session_uuid,
            "end_time": end_dt.isoformat(),
            "duration_seconds": duration,
            "energy_delivered_kwh": round(energy_delivered, 4),
            "soc_end": soc_end,
            "total_cost": round(total_cost, 2),
            "status": status,
            "end_reason": end_reason,
        }
    finally:
        conn.close()


def get_active_charge_session():
    """Return the currently active session (status='active'), or None."""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, session_uuid, start_time, soc_start, soc_target, battery_kwh,
                      start_energy_kwh, cost_per_kwh, energy_delivered_kwh, duration_seconds
               FROM charge_sessions WHERE status = 'active'
               ORDER BY start_time DESC LIMIT 1""",
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "session_uuid": row[1],
            "start_time": row[2],
            "soc_start": row[3],
            "soc_target": row[4],
            "battery_kwh": row[5],
            "start_energy_kwh": row[6],
            "cost_per_kwh": row[7],
            "energy_delivered_kwh": row[8] or 0,
            "duration_seconds": row[9] or 0,
        }
    finally:
        conn.close()


def list_charge_sessions(limit=50, include_active=False):
    """Return recent charge sessions, most recent first."""
    conn = get_db()
    try:
        if include_active:
            rows = conn.execute(
                """SELECT id, session_uuid, start_time, end_time, status, soc_start, soc_end,
                          soc_target, battery_kwh, start_energy_kwh, end_energy_kwh,
                          energy_delivered_kwh, duration_seconds, avg_power_w, cost_per_kwh, total_cost, end_reason
                   FROM charge_sessions
                   ORDER BY start_time DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, session_uuid, start_time, end_time, status, soc_start, soc_end,
                          soc_target, battery_kwh, start_energy_kwh, end_energy_kwh,
                          energy_delivered_kwh, duration_seconds, avg_power_w, cost_per_kwh, total_cost, end_reason
                   FROM charge_sessions
                   WHERE status != 'active'
                   ORDER BY start_time DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "session_uuid": r[1],
                "start_time": r[2],
                "end_time": r[3],
                "status": r[4],
                "soc_start": r[5],
                "soc_end": r[6],
                "soc_target": r[7],
                "battery_kwh": r[8],
                "start_energy_kwh": r[9],
                "end_energy_kwh": r[10],
                "energy_delivered_kwh": r[11] or 0,
                "duration_seconds": r[12] or 0,
                "avg_power_w": r[13] or 0,
                "cost_per_kwh": r[14] or 0,
                "total_cost": r[15] or 0,
                "end_reason": r[16],
            }
            for r in rows
        ]
    finally:
        conn.close()


def charge_sessions_summary(days=90, limit_days=None):
    """Compute summary stats over recent charge sessions. Accepts `days` or `limit_days`."""
    if limit_days is None:
        limit_days = days
    conn = get_db()
    try:
        since = (datetime.now() - timedelta(days=limit_days)).isoformat()
        row = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(energy_delivered_kwh), 0),
                      COALESCE(SUM(total_cost), 0), COALESCE(SUM(duration_seconds), 0),
                      COALESCE(AVG(energy_delivered_kwh), 0), COALESCE(AVG(total_cost), 0),
                      COALESCE(MIN(soc_start), 0), COALESCE(MAX(soc_end), 0)
               FROM charge_sessions
               WHERE status != 'active' AND start_time >= ?""",
            (since,),
        ).fetchone()
        if not row or row[0] == 0:
            return {
                "session_count": 0,
                "total_kwh": 0,
                "total_cost": 0,
                "total_duration_hours": 0,
                "avg_kwh_per_session": 0,
                "avg_cost_per_session": 0,
                "avg_power_w": 0,
            }
        count, total_kwh, total_cost, total_dur, avg_kwh, avg_cost, min_soc, max_soc = (
            row
        )
        # avg_power_w = total_kwh * 1000 / total_hours
        total_hours = total_dur / 3600.0
        avg_power = (total_kwh * 1000 / total_hours) if total_hours > 0 else 0
        return {
            "session_count": count,
            "total_kwh": round(total_kwh, 3),
            "total_cost": round(total_cost, 2),
            "total_duration_hours": round(total_hours, 2),
            "avg_kwh_per_session": round(avg_kwh, 3),
            "avg_cost_per_session": round(avg_cost, 2),
            "avg_power_w": round(avg_power, 0),
            "soc_start_min": min_soc,
            "soc_end_max": max_soc,
            "period_days": limit_days,
        }
    finally:
        conn.close()


# ─── LOCAL-FIRST DB queries ─────────────────────────────────────
def db_today_stats():
    """Calculate today's consumption using LOCAL data.

    Uses POWER × TIME integral (much more accurate than energy counter delta).
    Handles resets automatically since we track power directly.
    """
    cfg = load_config()
    cost = cfg.get("kwh_cost", 0.956)
    today = datetime.now().strftime("%Y-%m-%d")
    month_str = datetime.now().strftime("%Y-%m")

    conn = get_db()
    try:
        # Get today's readings
        rows = conn.execute(
            "SELECT timestamp, power, energy FROM readings WHERE device='fase1' AND DATE(timestamp)=? ORDER BY timestamp",
            (today,),
        ).fetchall()

        if not rows:
            return {
                "today_kwh": 0,
                "today_cost": 0,
                "month_kwh": 0,
                "month_cost": 0,
                "kwh_cost": cost,
                "source": "local",
            }

        # Calculate consumption via POWER × TIME (the correct way)
        total_kwh = 0.0
        for i in range(1, len(rows)):
            t1, p1, e1 = rows[i - 1]
            t2, p2, e2 = rows[i]

            dt1 = datetime.fromisoformat(t1)
            dt2 = datetime.fromisoformat(t2)
            dt_seconds = (dt2 - dt1).total_seconds()

            if 0 < dt_seconds < 120:  # sanity: max 2 min gap
                # Power is in W, convert to kW and multiply by hours
                avg_power_w = (p1 + p2) / 2
                kwh = (avg_power_w / 1000) * (dt_seconds / 3600)
                total_kwh += kwh

        today_kwh = round(total_kwh, 4)

        # Month: POWER × TIME integral over all days of current month (robust, no snapshot dependency).
        # Falls back to daily_snapshots sum only if no readings exist (shouldn't happen post-backfill).
        month_start = datetime.now().strftime("%Y-%m-01")
        month_rows = conn.execute(
            "SELECT timestamp, power FROM readings "
            "WHERE device='fase1' AND DATE(timestamp)>=? AND power IS NOT NULL "
            "ORDER BY timestamp",
            (month_start,),
        ).fetchall()
        if month_rows and len(month_rows) > 1:
            month_kwh = round(_kwh_from_power_integral(month_rows), 4)
        else:
            month_row = conn.execute(
                "SELECT SUM(energy_kwh) FROM daily_snapshots WHERE snapshot_date>=? AND device='fase1'",
                (month_start,),
            ).fetchone()
            month_kwh = round(max(0, (month_row[0] or 0) if month_row else 0), 4)

        # Get readings count for verification
        count = conn.execute(
            "SELECT COUNT(*) FROM readings WHERE device='fase1' AND DATE(timestamp)=?",
            (today,),
        ).fetchone()[0]

        # BREAKER: Calculate breaker consumption via stored power readings
        # phase_c now stores breaker_power_w (V × I from DPS 6)
        br_rows = conn.execute(
            "SELECT timestamp, phase_c FROM readings WHERE device='fase1' AND DATE(timestamp)=? ORDER BY timestamp",
            (today,),
        ).fetchall()

        breaker_kwh = 0.0
        if br_rows:
            for i in range(1, len(br_rows)):
                t1, pw1 = br_rows[i - 1]
                t2, pw2 = br_rows[i]

                if pw1 is None or pw2 is None:
                    continue

                dt1 = datetime.fromisoformat(t1)
                dt2 = datetime.fromisoformat(t2)
                dt_seconds = (dt2 - dt1).total_seconds()

                if 0 < dt_seconds < 120:
                    avg_power_w = (pw1 + pw2) / 2
                    if avg_power_w > 1:
                        breaker_kwh += (avg_power_w / 1000) * (dt_seconds / 3600)

        breaker_kwh = round(breaker_kwh, 4)

        return {
            "today_kwh": today_kwh,
            "today_cost": round(today_kwh * cost, 2),
            "month_kwh": round(month_kwh, 4),
            "month_cost": round(month_kwh * cost, 2),
            "kwh_cost": cost,
            "source": "local",
            "readings": count,
            "breaker_kwh": breaker_kwh,
        }
    finally:
        conn.close()


def db_daily_history(days=30):
    """Return daily consumption from LOCAL snapshots.

    Snapshots store **consumed kWh per day** (already integrated from power×time),
    not cumulative readings — so we return them directly without diffing.
    """
    cfg = load_config()
    cost = cfg.get("kwh_cost", 0.956)

    conn = get_db()
    try:
        # Get all snapshots
        snap_rows = conn.execute(
            "SELECT snapshot_date, device, energy_kwh FROM daily_snapshots ORDER BY snapshot_date",
        ).fetchall()

        f1_daily = {}
        br_daily = {}
        for row in snap_rows:
            day, dev, energy = row
            if dev == "fase1":
                f1_daily[day] = energy
            else:
                br_daily[day] = energy

        # Collect all non-zero f1 values to compute overall average fallback
        f1_nonzero = [v for v in f1_daily.values() if v > 0]
        f1_overall_avg = sum(f1_nonzero) / len(f1_nonzero) if f1_nonzero else 0

        # Get last N days, fill missing with 0, estimate f1 when missing but breaker exists
        result = []
        today = datetime.now().date()
        sorted_days = [(today - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(days - 1, -1, -1)]

        for day in sorted_days:
            f1 = f1_daily.get(day, 0)
            br = br_daily.get(day, 0)

            # If fase1 is 0 but breaker has data, estimate house consumption
            if f1 == 0 and br > 0:
                # Find previous and next non-zero f1 days
                prev_val = None
                next_val = None
                for other_day in sorted_days:
                    other_f1 = f1_daily.get(other_day, 0)
                    if other_f1 > 0:
                        if other_day < day:
                            prev_val = other_f1
                        elif other_day > day and next_val is None:
                            next_val = other_f1

                if prev_val is not None and next_val is not None:
                    f1 = round((prev_val + next_val) / 2, 4)
                elif prev_val is not None:
                    f1 = prev_val
                elif next_val is not None:
                    f1 = next_val
                else:
                    f1 = f1_overall_avg

            result.append(
                {
                    "date": day,
                    "consumed_kwh": f1,
                    "phase1_kwh": f1_daily.get(day, 0),
                    "breaker_kwh": br,
                    "cost": round(f1 * cost, 2),
                }
            )

        return result
    finally:
        conn.close()


def db_hourly(date=None):
    """Return hourly consumption from local readings using power×time integration."""
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT timestamp, power FROM readings WHERE device='fase1' AND DATE(timestamp)=? AND power IS NOT NULL ORDER BY timestamp",
            (date,),
        ).fetchall()

        if not rows:
            return {"date": date, "hours": [], "total_kwh": 0, "source": "local"}

        # Group timestamps+power by hour for proper integration
        hourly_data = defaultdict(list)
        for ts, power in rows:
            hour = datetime.fromisoformat(ts).strftime("%H")
            hourly_data[hour].append((ts, power or 0))

        # Build array of 24 hour entries (frontend expects an array, not a dict)
        hours = []
        total_kwh = 0.0
        for h in range(24):
            hh = f"{h:02d}"
            if hh in hourly_data and len(hourly_data[hh]) > 1:
                pts = hourly_data[hh]
                kwh = round(_kwh_from_power_integral(pts), 4)
                avg_power = sum(p for _, p in pts) / len(pts)
                cnt = len(pts)
            else:
                cnt = len(hourly_data.get(hh, []))
                avg_power = hourly_data[hh][0][1] if cnt > 0 else 0
                kwh = 0
            total_kwh += kwh
            hours.append(
                {
                    "hour": hh,
                    "kwh": round(kwh, 4),
                    "avg_power_w": round(avg_power, 1),
                    "readings": cnt,
                }
            )

        cfg = load_config()
        cost = cfg.get("kwh_cost", 0.956)
        return {
            "date": date,
            "hours": hours,
            "total_kwh": round(total_kwh, 4),
            "total_cost": round(total_kwh * cost, 2),
            "source": "local",
        }
    finally:
        conn.close()


# ─── Cloud endpoints (user-triggered) ───────────────────────────
def cloud_daily_consumption(logs):
    add_by_day = defaultdict(float)
    for log in logs:
        if log.get("code") == "add_ele":
            ts_ms = log.get("event_time", 0)
            if ts_ms:
                try:
                    val = float(log.get("value", 0))
                    day = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
                    add_by_day[day] += val
                except:
                    pass
    return {day: round(wh / 1000, 4) for day, wh in add_by_day.items() if wh > 0}


# ─── Charging tracker ──────────────────────────────────────
class ChargingTracker:
    """
    Tracks an active car-charging session.

    Key insight: a Tuya circuit breaker only sees total energy flowing through it.
    So we measure the *delta* of `breaker_energy_kwh` from charge-start to now,
    and use that to project the *effective* SOC.

    Lifecycle:
        1. User clicks "Start"  → start_charge()  → state = CHARGING
        2. While CHARGING, we keep measuring energy delta + power draw
        3. When effective SOC >= target AND power drops to idle → state = COMPLETING
        4. We wait car_charge_idle_seconds_to_stop to confirm the car really stopped
        5. Then turn breaker OFF → state = DONE
        6. If effective SOC >= target but power is still high, we KEEP the breaker ON
           (car is balancing / equalising / not yet full)
    """

    STATE_IDLE = "idle"
    STATE_CHARGING = "charging"
    STATE_COMPLETING = "completing"  # target reached, waiting for car to stop pulling
    STATE_DONE = "done"  # auto-stopped after idle confirmation
    STATE_ERROR = "error"

    def __init__(self):
        self.lock = threading.Lock()
        # In-memory state (separate from config to avoid disk thrash)
        self.state = self.STATE_IDLE
        self.start_time = None
        self.start_energy_kwh = 0.0
        self.start_soc = 0
        self.target_soc = 80
        self.battery_kwh = 12.9
        self.last_power_w = 0.0
        self.peak_power_w = 0.0  # peak power seen during charging (for idle detection)
        self.power_samples = []  # rolling window for average
        self.idle_started_at = None  # when power first went below threshold
        self.energy_samples = []  # (timestamp, energy_kwh) for accurate delta calc
        self.effective_soc = 0
        self.message = ""
        self.session_uuid = None  # DB session reference

    def start(
        self, start_soc, target_soc, battery_kwh, start_energy_kwh, session_uuid=None
    ):
        with self.lock:
            self.state = self.STATE_CHARGING
            self.start_time = datetime.now()
            self.start_energy_kwh = start_energy_kwh
            self.start_soc = start_soc
            self.target_soc = target_soc
            self.battery_kwh = battery_kwh
            self.last_power_w = 0.0
            self.peak_power_w = 0.0
            self.power_samples = []
            self.idle_started_at = None
            self.energy_samples = [(datetime.now(), start_energy_kwh)]
            self.effective_soc = start_soc
            self.message = "Carregando"
            self.session_uuid = session_uuid

    def stop(self, reason="manual"):
        with self.lock:
            self.state = self.STATE_DONE if reason == "auto" else self.STATE_IDLE
            self.message = f"Parado ({reason})"
            # Reset for next session
            self.start_time = None
            self.start_energy_kwh = 0.0
            self.start_soc = 0
            self.last_power_w = 0.0
            self.peak_power_w = 0.0
            self.power_samples = []
            self.idle_started_at = None
            self.energy_samples = []
            self.session_uuid = None

    def update(
        self, current_energy_kwh, current_power_w, idle_power_w, idle_seconds_needed
    ):
        """
        Called every poll cycle while charging. Returns the new state.

        Idle detection is POWER-BASED (not gated on target SOC): the breaker
        measures only the car circuit, so when the car stops accepting charge the
        breaker power drops to ~idle (<= idle_power_w). This correctly handles
        BOTH app-started sessions AND sessions where the breaker was flipped on
        manually — in the latter case our energy-based SOC estimate may never
        reach the configured target even though the car is physically full, so a
        target-gated end check would never fire. The "balancing" case is
        preserved: while the car still draws real power (even above target) it
        stays CHARGING and the breaker is kept ON.
        """
        with self.lock:
            if self.state not in (self.STATE_CHARGING, self.STATE_COMPLETING):
                return self.state

            now = datetime.now()
            self.last_power_w = current_power_w
            self.energy_samples.append((now, current_energy_kwh))
            # Trim old samples (keep last 30 min)
            cutoff = now - timedelta(minutes=30)
            self.energy_samples = [
                (t, e) for t, e in self.energy_samples if t >= cutoff
            ]

            # Compute energy delta since start
            energy_delta = max(0.0, current_energy_kwh - self.start_energy_kwh)

            # Compute effective SOC from energy delivered
            # SOC% = start_soc + (energy_delta_kwh / battery_kwh) * 100
            self.effective_soc = (
                self.start_soc + (energy_delta / max(0.1, self.battery_kwh)) * 100
            )
            self.effective_soc = min(100.0, self.effective_soc)

            # Maintain rolling avg of last 30s of power samples
            self.power_samples.append(current_power_w)
            if len(self.power_samples) > 3:  # ~30s at 10s poll
                self.power_samples = self.power_samples[-3:]

            # Track peak power seen during this charge session (for idle detection)
            if current_power_w > self.peak_power_w:
                self.peak_power_w = current_power_w

            # Decision logic (power-based — see method docstring)
            target_reached = self.effective_soc >= self.target_soc
            power_idle = current_power_w <= idle_power_w

            if power_idle:
                # Car stopped accepting charge (finished, or top-off taper).
                # Begin / continue the completion-confirmation window.
                if self.state == self.STATE_CHARGING:
                    # Transition: CHARGING → COMPLETING
                    self.state = self.STATE_COMPLETING
                    self.idle_started_at = now
                    self.message = "Carro parou de consumir. Aguardando confirmação..."
                else:
                    # Already completing, check elapsed time
                    elapsed = (
                        (now - self.idle_started_at).total_seconds()
                        if self.idle_started_at
                        else 0
                    )
                    if elapsed >= idle_seconds_needed:
                        self.message = f"Pronto para desligar ({int(elapsed)}s idle)"
                    else:
                        self.message = f"Confirmando carga completa... {int(idle_seconds_needed - elapsed)}s"
            else:
                # Car is still drawing real power — charging or cell-balancing.
                # KEEP BREAKER ON.
                if self.state == self.STATE_COMPLETING:
                    # Resumed drawing power (e.g. balancing kicked in) — back to charging
                    self.state = self.STATE_CHARGING
                    self.message = "Carro voltou a consumir - mantendo ligado"
                elif target_reached:
                    self.message = "Meta atingida, carro ainda consumindo (balanceando)"
                else:
                    self.message = "Carregando"
                self.idle_started_at = None

            return self.state

    def should_auto_stop(self, idle_seconds_needed):
        """Returns True if we should turn the breaker off."""
        with self.lock:
            if self.state != self.STATE_COMPLETING:
                return False
            if not self.idle_started_at:
                return False
            elapsed = (datetime.now() - self.idle_started_at).total_seconds()
            return elapsed >= idle_seconds_needed

    def get_status(self):
        with self.lock:
            if self.state == self.STATE_IDLE or not self.start_time:
                return {
                    "state": self.state,
                    "charging": False,
                    "message": "Desligado",
                    "elapsed_seconds": 0,
                    "energy_delivered_kwh": 0,
                    "effective_soc": self.start_soc,
                    "estimated_remaining_minutes": None,
                    "target_reached": False,
                    "idle_seconds": 0,
                }

            elapsed = (datetime.now() - self.start_time).total_seconds()
            energy_delta = (
                max(0.0, self.last_energy_kwh - self.start_energy_kwh)
                if self.energy_samples
                else 0
            )
            # Use the last energy sample for accurate delta
            if self.energy_samples:
                energy_delta = max(
                    0.0, self.energy_samples[-1][1] - self.start_energy_kwh
                )

            # Estimate remaining time
            need_soc = max(0, self.target_soc - self.effective_soc)
            need_kwh = (need_soc / 100) * self.battery_kwh
            avg_power_w = (
                sum(self.power_samples) / max(1, len(self.power_samples))
                if self.power_samples
                else self.last_power_w
            )
            if avg_power_w > 10 and need_kwh > 0:
                est_min = (need_kwh / (avg_power_w / 1000)) * 60
            else:
                # Fallback: use historical charge rate
                avg_rate = get_avg_charge_rate()  # kWh/hour
                if avg_rate > 0 and need_kwh > 0:
                    est_min = (need_kwh / avg_rate) * 60
                else:
                    est_min = None

            idle_seconds = 0
            if self.idle_started_at:
                idle_seconds = (datetime.now() - self.idle_started_at).total_seconds()

            return {
                "state": self.state,
                "charging": self.state in (self.STATE_CHARGING, self.STATE_COMPLETING),
                "message": self.message,
                "elapsed_seconds": int(elapsed),
                "energy_delivered_kwh": round(energy_delta, 4),
                "effective_soc": round(self.effective_soc, 1),
                "start_soc": self.start_soc,
                "target_soc": self.target_soc,
                "estimated_remaining_minutes": round(est_min, 1)
                if est_min is not None
                else None,
                "target_reached": self.effective_soc >= self.target_soc,
                "idle_seconds": int(idle_seconds),
                "current_power_w": self.last_power_w,
            }

    @property
    def last_energy_kwh(self):
        if self.energy_samples:
            return self.energy_samples[-1][1]
        return self.start_energy_kwh


charging = ChargingTracker()


# ─── Poll loop ──────────────────────────────────────────────────
POLL_INTERVAL = 10  # seconds


def poll_loop():
    devs = {}
    prune_counter = 0
    # Recupera último dia com snapshot (persiste entre restarts)
    _cfg = load_config()
    last_snapshot_day = _cfg.get("last_snapshot_day", "")
    print(f"🔄 Polling iniciado. last_snapshot_day={last_snapshot_day or '(nenhum)'}")

    # Previous breaker energy counter (kWh) + timestamp, used to estimate power
    # from the DPS 1 cumulative delta when DPS 6 (V×I) reads 0 during charging.
    prev_br_counter_kwh = None
    prev_br_counter_ts = None

    # Backfill inicial: roda 1x por instalação (gate em snapshots_backfilled),
    # corrigindo placeholders antigos (0.001 / cumulativo) por integrais reais
    # de power×tempo.
    _backfill_snapshots_from_readings()

    while True:
        try:
            for key, cfg in DEVICES.items():
                try:
                    if key not in devs:
                        devs[key] = connect_device(cfg)
                    d = devs[key]
                    if key == "fase1":
                        state.update(key, read_fase1(d))
                    elif key == "breaker":
                        state.update(key, read_breaker(d))
                except Exception as e:
                    print(f"Erro {key}: {e}")
                    devs.pop(key, None)

            with state.lock:
                f1 = state.latest.get("fase1", {})
                br = state.latest.get("breaker", {})
            if f1:
                save_reading(f1, br)

            # ── AUTO-DETECT charging: breaker ON with power or energy changing ──
            if (
                br
                and br.get("switch", False)
                and charging.state == ChargingTracker.STATE_IDLE
                and (
                    br.get("power_w", 0) > 500
                    or (
                        br.get("energy_wh", 0) > 0
                        and prev_br_counter_kwh is not None
                        and prev_br_counter_kwh > 0
                        and br.get("energy_wh", 0) != prev_br_counter_kwh
                    )
                )
            ):
                cfg_detect = load_config()
                session = create_charge_session(
                    soc_start=cfg_detect.get("car_current_soc", 50),
                    soc_target=cfg_detect.get("car_target_soc", 80),
                    battery_kwh=cfg_detect.get("car_battery_kwh", 12.9),
                    start_energy_kwh=br.get("energy_wh", 0),
                    cost_per_kwh=cfg_detect.get("kwh_cost", 0.956),
                )
                charging.start(
                    start_soc=cfg_detect.get("car_current_soc", 50),
                    target_soc=cfg_detect.get("car_target_soc", 80),
                    battery_kwh=cfg_detect.get("car_battery_kwh", 12.9),
                    start_energy_kwh=br.get("energy_wh", 0),
                    session_uuid=session["session_uuid"],
                )
                print("⚡ Auto-detected charging: breaker ON with power, starting session")

            # ── Breaker power, with energy-counter fallback ──
            # DPS 6 (V×I) sometimes returns 0 during active charging. When it
            # does, estimate power from the DPS 1 cumulative counter delta so
            # auto-detect and idle detection keep working.
            # NOTE: the counter field is in kWh (see read_breaker); the legacy
            # "energy_wh" key name is kept for backward compatibility.
            br_switch_on = bool(br.get("switch")) if br else False
            br_power_w = br.get("power_w", 0) if br else 0
            br_counter_kwh = br.get("energy_wh", 0) if br else 0  # kWh (legacy name)
            if (
                br_power_w <= 0
                and br_counter_kwh > 0
                and prev_br_counter_kwh is not None
                and prev_br_counter_kwh > 0
            ):
                dt_s = (datetime.now() - prev_br_counter_ts).total_seconds()
                delta_kwh = br_counter_kwh - prev_br_counter_kwh
                if 0 < dt_s < 120 and delta_kwh > 0:
                    # kWh over seconds → watts
                    br_power_w = delta_kwh * 3_600_000.0 / dt_s
            if br_counter_kwh > 0:
                prev_br_counter_kwh = br_counter_kwh
                prev_br_counter_ts = datetime.now()

            cfg = load_config()

            # ── Charging tracker update + auto-stop check ──
            if charging.state in (
                ChargingTracker.STATE_CHARGING,
                ChargingTracker.STATE_COMPLETING,
            ):
                if br is not None and not br_switch_on:
                    # Breaker was turned OFF externally (physical switch / fault)
                    # while a session was active → finalize it now instead of
                    # leaving a ghost "active" session in the DB.
                    print(
                        "🔌 Breaker desligado externamente durante sessão — finalizando"
                    )
                    if charging.session_uuid:
                        finalize_charge_session(
                            charging.session_uuid,
                            end_energy_kwh=br_counter_kwh,
                            soc_end=charging.effective_soc,
                            end_reason="manual",
                        )
                    charging.stop(reason="manual")
                    cfg["car_charging"] = False
                    cfg["car_charge_start_time"] = None
                    save_config(cfg)
                elif br is not None:
                    # Session active and breaker ON — feed latest readings.
                    charging.update(
                        current_energy_kwh=br_counter_kwh,
                        current_power_w=br_power_w,
                        idle_power_w=cfg.get("car_charge_idle_power_w", 15),
                        idle_seconds_needed=cfg.get(
                            "car_charge_idle_seconds_to_stop", 120
                        ),
                    )
                    # Persist progress to DB (every ~10s)
                    if charging.session_uuid and charging.start_time:
                        elapsed = (datetime.now() - charging.start_time).total_seconds()
                        update_charge_session_progress(
                            charging.session_uuid,
                            current_energy_kwh=br_counter_kwh,
                            current_soc=charging.effective_soc,
                            duration_seconds=int(elapsed),
                            avg_power_w=charging.last_power_w,
                        )
                    # Auto-stop when the car finished (idle confirmed) and allowed
                    if cfg.get(
                        "car_charge_auto_stop", True
                    ) and charging.should_auto_stop(
                        cfg.get("car_charge_idle_seconds_to_stop", 120)
                    ):
                        print(
                            "🔌 Auto-stopping breaker (charge complete, idle confirmed)"
                        )
                        try:
                            d_brk = devs.get("breaker") or connect_device(
                                DEVICES["breaker"]
                            )
                            d_brk.set_value(BREAKER_SWITCH_DPS, False)
                            time.sleep(1)
                            state.update("breaker", read_breaker(d_brk))
                            # Finalize DB session
                            if charging.session_uuid:
                                with state.lock:
                                    br_end = state.latest.get("breaker", {})
                                end_energy = (
                                    br_end.get("energy_wh", 0) if br_end else 0
                                )
                                finalize_charge_session(
                                    charging.session_uuid,
                                    end_energy_kwh=end_energy,
                                    soc_end=charging.effective_soc,
                                    end_reason="auto",
                                )
                            charging.stop(reason="auto")
                            cfg["car_charging"] = False
                            save_config(cfg)
                        except Exception as e:
                            print(f"Auto-stop error: {e}")
                # else: br is None (comms fail) → skip this cycle, retry next poll

            # Daily snapshot: detecta virada de dia e fecha o dia que acabou
            today = datetime.now().strftime("%Y-%m-%d")
            if last_snapshot_day and today != last_snapshot_day:
                # O dia virou — fecha o dia que estava em curso
                closing_day = last_snapshot_day
                conn = get_db()
                try:
                    f1_rows = conn.execute(
                        "SELECT timestamp, power FROM readings "
                        "WHERE device='fase1' AND DATE(timestamp)=? AND power IS NOT NULL "
                        "ORDER BY timestamp",
                        (closing_day,),
                    ).fetchall()
                    br_counter = conn.execute(
                        "SELECT MAX(breaker_energy), MIN(breaker_energy) FROM readings "
                        "WHERE device='fase1' AND DATE(timestamp)=? AND breaker_energy IS NOT NULL",
                        (closing_day,),
                    ).fetchone()
                    f1_kwh = (
                        round(_kwh_from_power_integral(f1_rows), 4) if f1_rows else 0
                    )
                    br_kwh = (
                        round(br_counter[0] - br_counter[1], 4)
                        if br_counter and br_counter[0] is not None and br_counter[1] is not None
                        else 0
                    )
                    if f1_kwh > 0:
                        conn.execute(
                            """INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, created_at)
                               VALUES (?, 'fase1', ?, ?)
                               ON CONFLICT(snapshot_date, device) DO UPDATE SET energy_kwh = excluded.energy_kwh""",
                            (closing_day, f1_kwh, datetime.now().isoformat()),
                        )
                    if br_kwh > 0:
                        conn.execute(
                            """INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, created_at)
                               VALUES (?, 'breaker', ?, ?)
                               ON CONFLICT(snapshot_date, device) DO UPDATE SET energy_kwh = excluded.energy_kwh""",
                            (closing_day, br_kwh, datetime.now().isoformat()),
                        )
                    conn.commit()
                    print(
                        f"📸 Snapshot fechado para {closing_day}: fase1={f1_kwh}kWh breaker={br_kwh}kWh"
                    )
                except Exception as e:
                    print(f"⚠️ Erro ao fechar snapshot de {closing_day}: {e}")
                finally:
                    conn.close()
                # Persiste o novo "último dia fechado" na config
                _cfg2 = load_config()
                _cfg2["last_snapshot_day"] = today
                save_config(_cfg2)
                last_snapshot_day = today
            elif not last_snapshot_day:
                # Primeira execução: registra o dia atual sem fechar nada
                _cfg3 = load_config()
                _cfg3["last_snapshot_day"] = today
                save_config(_cfg3)
                last_snapshot_day = today

            prune_counter += 1
            if prune_counter >= 600:
                prune_db()
                prune_counter = 0

        except Exception as e:
            print(f"Poll error: {e}")
            devs = {}
        time.sleep(POLL_INTERVAL)


# ─── FastAPI app ────────────────────────────────────────────────
app = FastAPI()


@app.get("/api/status")
def api_status():
    with state.lock:
        return {
            "timestamp": datetime.now().isoformat(),
            "devices": state.latest,
            "config": load_config(),
        }


@app.get("/api/today")
def api_today():
    return db_today_stats()


@app.get("/api/daily-history")
def api_daily_history(days: int = 30):
    return {"days": db_daily_history(days)}


def db_monthly_stats(year: int, month: int):
    """Return monthly aggregated stats: total kWh, cost, daily breakdown.

    Uses POWER × TIME integration (same as db_today_stats), not energy counter delta.
    """
    cfg = load_config()
    cost = cfg.get("kwh_cost", 0.956)
    first = f"{year:04d}-{month:02d}-01"
    if month == 12:
        last = f"{year + 1:04d}-01-01"
    else:
        last = f"{year:04d}-{month + 1:02d}-01"
    conn = get_db()
    try:
        # Get all readings with power for the month, ordered by time
        rows = conn.execute(
            """SELECT DATE(timestamp) AS day, timestamp, power
               FROM readings
               WHERE device = 'fase1' AND timestamp >= ? AND timestamp < ?
                 AND power IS NOT NULL
               ORDER BY timestamp""",
            (first, last),
        ).fetchall()

        # Group by day for per-day integration
        from collections import defaultdict
        by_day = defaultdict(list)
        for day, ts, p in rows:
            by_day[day].append((ts, p or 0))

        daily = []
        total_kwh = 0.0
        for day in sorted(by_day.keys()):
            pts = by_day[day]
            if len(pts) > 1:
                kwh = round(_kwh_from_power_integral(pts), 4)
            else:
                kwh = 0
            total_kwh += kwh
            daily.append(
                {
                    "day": day,
                    "kwh": kwh,
                    "cost": round(kwh * cost, 2),
                }
            )

        return {
            "year": year,
            "month": month,
            "total_kwh": round(total_kwh, 4),
            "total_cost": round(total_kwh * cost, 2),
            "daily": daily,
            "source": "local",
        }
    finally:
        conn.close()


@app.get("/api/monthly")
def api_monthly(year: int, month: int):
    return db_monthly_stats(year, month)


@app.post("/api/cloud-sync")
def api_cloud_sync():
    """Force-refresh Tuya cloud cache. Returns summary count."""
    try:
        # Check that cloud is configured. We don't need the client object
        # itself — get_cloud_logs() opens its own session per call.
        if not get_cloud():
            return {"success": False, "error": "cloud not configured"}
        days = 7
        for dev in DEVICES.values():
            get_cloud_logs(dev["id"], days=days, use_cache=False)
        return {"success": True, "synced_days": days, "devices": len(DEVICES)}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/clear-db")
def api_clear_db(before_days: int = 30):
    """Prune old readings (keep last N days)."""
    # Guard: negative or zero values would target the future (delete everything).
    # Clamp to a minimum of 1 day.
    if before_days < 1:
        return {
            "success": False,
            "error": "before_days must be >= 1",
            "received": before_days,
        }
    try:
        cutoff = (datetime.now() - timedelta(days=before_days)).isoformat()
        conn = get_db()
        try:
            cur = conn.execute("DELETE FROM readings WHERE timestamp < ?", (cutoff,))
            deleted = cur.rowcount
            conn.commit()
            return {"success": True, "deleted": deleted, "kept_days": before_days}
        finally:
            conn.close()
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/hourly")
def api_hourly(date: str = None):
    return db_hourly(date)


@app.post("/api/breaker/on")
def api_breaker_on():
    try:
        d = connect_device(DEVICES["breaker"])
        # DPS 11 = switch_state (the real breaker control)
        # DPS 1 (default from turn_on) is total_forward_energy_kwh, doesn't work
        d.set_value(BREAKER_SWITCH_DPS, True)
        time.sleep(1)
        state.update("breaker", read_breaker(d))
        with state.lock:
            actual = state.latest.get("breaker", {}).get("switch", False)
        return {
            "success": actual,
            "state": "ON" if actual else "FAILED",
            "breaker_switch": actual,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/breaker/off")
def api_breaker_off():
    try:
        d = connect_device(DEVICES["breaker"])
        d.set_value(BREAKER_SWITCH_DPS, False)
        time.sleep(1)
        state.update("breaker", read_breaker(d))
        with state.lock:
            actual = state.latest.get("breaker", {}).get("switch", False)
        return {
            "success": not actual,
            "state": "OFF" if not actual else "FAILED",
            "breaker_switch": actual,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─── Car charging endpoints ─────────────────────────────────────
@app.post("/api/car/soc")
def api_car_soc(soc: int = 0):
    """Update current SOC (state of charge)."""
    cfg = load_config()
    cfg["car_current_soc"] = max(0, min(100, soc))
    save_config(cfg)
    return {"success": True, "car_current_soc": cfg["car_current_soc"]}


@app.post("/api/car/target")
def api_car_target(target: int = 80):
    """Update target SOC."""
    cfg = load_config()
    cfg["car_target_soc"] = max(0, min(100, target))
    save_config(cfg)
    return {"success": True, "car_target_soc": cfg["car_target_soc"]}


@app.post("/api/car/start-charge")
def api_car_start_charge():
    """Turn breaker ON to start charging. Initializes the charging tracker."""
    try:
        d = connect_device(DEVICES["breaker"])
        result = d.set_value(BREAKER_SWITCH_DPS, True)
        time.sleep(1)
        state.update("breaker", read_breaker(d))

        with state.lock:
            br = state.latest.get("breaker", {})

        if not br.get("switch", False):
            # Check fault_code for known alarms
            fault = br.get("fault_code", 0)
            if fault & 0x10000:  # no_balance alarm
                return {
                    "success": False,
                    "error": "no_balance_alarm",
                    "message": "Disjuntor bloqueado por falta de saldo (prepay).",
                    "hint": "Desative 'Switch Prepayment' (DPS 11) no app Smart Life ou recarregue o saldo.",
                    "fault_code": fault,
                    "balance_kwh": br.get("balance_kwh", 0),
                }
            if fault:
                return {
                    "success": False,
                    "error": "fault_alarm",
                    "message": f"Disjuntor bloqueado por alarme (fault_code={fault}).",
                    "fault_code": fault,
                }
            return {
                "success": False,
                "error": "no_response",
                "message": "Breaker não respondeu ao comando. Verifique conexão.",
            }

        # Initialize the charging tracker + create DB session
        cfg = load_config()
        cost_per_kwh = cfg.get("kwh_cost", 0.956)
        # Use breaker energy counter (Wh) for session tracking
        start_energy_wh = br.get("energy_wh", 0)
        start_energy = start_energy_wh if start_energy_wh else 0
        session = create_charge_session(
            soc_start=cfg.get("car_current_soc", 50),
            soc_target=cfg.get("car_target_soc", 80),
            battery_kwh=cfg.get("car_battery_kwh", 12.9),
            start_energy_kwh=start_energy,
            cost_per_kwh=cost_per_kwh,
        )
        charging.start(
            start_soc=cfg.get("car_current_soc", 50),
            target_soc=cfg.get("car_target_soc", 80),
            battery_kwh=cfg.get("car_battery_kwh", 12.9),
            start_energy_kwh=br.get("energy_kwh", 0),
            session_uuid=session["session_uuid"],
        )
        cfg["car_charging"] = True
        cfg["car_charge_start_kwh"] = br.get("energy_kwh", 0)
        cfg["car_charge_start_time"] = datetime.now().isoformat()
        cfg["car_charge_start_soc"] = cfg.get("car_current_soc", 50)
        save_config(cfg)
        return {
            "success": True,
            "state": "charging",
            "breaker_switch": True,
            "session_uuid": session["session_uuid"],
            "charge": charging.get_status(),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/car/stop-charge")
def api_car_stop_charge():
    """Turn breaker OFF to stop charging. Finalizes the DB session."""
    try:
        d = connect_device(DEVICES["breaker"])
        d.set_value(BREAKER_SWITCH_DPS, False)
        time.sleep(1)
        state.update("breaker", read_breaker(d))

        # Finalize DB session
        result = None
        if charging.session_uuid:
            with state.lock:
                br_end = state.latest.get("breaker", {})
            end_energy_wh = br_end.get("energy_wh", 0) if br_end else 0
            end_energy = end_energy_wh if end_energy_wh else 0
            result = finalize_charge_session(
                charging.session_uuid,
                end_energy_kwh=end_energy,
                soc_end=charging.effective_soc,
                end_reason="manual",
            )

        charging.stop(reason="manual")
        cfg = load_config()
        cfg["car_charging"] = False
        cfg["car_charge_start_time"] = None
        save_config(cfg)
        return {
            "success": True,
            "state": "stopped",
            "breaker_switch": False,
            "charge": charging.get_status(),
            "finalized_session": result,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/charge/state")
def api_charge_state():
    """Detailed charging session state from the tracker."""
    return charging.get_status()


@app.get("/api/charge/sessions")
def api_charge_sessions(limit: int = 50, include_active: bool = False):
    """List recent charge sessions."""
    return {
        "sessions": list_charge_sessions(limit=limit, include_active=include_active),
        "active": get_active_charge_session(),
    }


@app.get("/api/charge/summary")
def api_charge_summary(days: int = 90):
    """Summary of charge sessions over the period."""
    return charge_sessions_summary(days=days)


@app.get("/api/car/status")
def api_car_status():
    """Get car charging status."""
    cfg = load_config()
    with state.lock:
        br = state.latest.get("breaker", {})
    charge = charging.get_status()
    return {
        "charging": charge.get("charging", False),
        "charge_state": charge.get("state", "idle"),
        "charge_message": charge.get("message", ""),
        "elapsed_seconds": charge.get("elapsed_seconds", 0),
        "energy_delivered_kwh": charge.get("energy_delivered_kwh", 0),
        "effective_soc": charge.get("effective_soc", cfg.get("car_current_soc", 50)),
        "estimated_remaining_minutes": charge.get("estimated_remaining_minutes"),
        "target_reached": charge.get("target_reached", False),
        "target_soc": cfg.get("car_target_soc", 80),
        "current_soc": cfg.get("car_current_soc", 50),
        "breaker_switch": br.get("switch", False),
        "prepayment_enabled": br.get("prepayment", False),
        "balance_kwh": br.get("balance_kwh", 0),
        "fault_code": br.get("fault_code", 0),
        "energy_kwh": br.get("energy_kwh", 0),
        "phase_a": br.get("phase_a", 0),
        "phase_b": br.get("phase_b", 0),
        "auto_stop_enabled": cfg.get("car_charge_auto_stop", True),
    }


# ─── Prepayment (DPS 11) endpoints ──────────────────────────────
@app.post("/api/breaker/prepay/on")
def api_breaker_prepay_on():
    """Enable prepayment mode (DPS 11 = True)."""
    try:
        d = connect_device(DEVICES["breaker"])
        d.set_value(11, True)
        time.sleep(1)
        state.update("breaker", read_breaker(d))
        with state.lock:
            actual = state.latest.get("breaker", {}).get("prepayment", False)
        return {"success": actual, "prepayment": actual}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/breaker/prepay/off")
def api_breaker_prepay_off():
    """Disable prepayment mode (DPS 11 = False)."""
    try:
        d = connect_device(DEVICES["breaker"])
        d.set_value(11, False)
        time.sleep(1)
        state.update("breaker", read_breaker(d))
        with state.lock:
            actual = state.latest.get("breaker", {}).get("prepayment", True)
        return {"success": not actual, "prepayment": actual}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/config")
def api_config():
    return load_config()


@app.post("/api/config")
def api_config_update(cfg: dict = None):
    if cfg is None:
        return {"error": "No config provided"}
    # Whitelist of allowed config keys (prevents arbitrary key injection)
    ALLOWED_CONFIG_KEYS = {
        "kwh_cost",
        "kwh_currency",
        "car_battery_kwh",
        "car_charge_power_w",
        "car_target_soc",
        "car_current_soc",
        "car_charging",
        "car_charge_start_kwh",
        "car_charge_start_time",
        "car_charge_start_soc",
        "car_charge_idle_seconds_to_stop",
        "car_charge_idle_power_w",
        "car_charge_start_power_w",
        "car_charge_auto_stop",
        "cloud_enabled",
    }
    safe_cfg = {k: v for k, v in cfg.items() if k in ALLOWED_CONFIG_KEYS}
    rejected = set(cfg.keys()) - set(safe_cfg.keys())
    current = load_config()
    current.update(safe_cfg)
    save_config(current)
    result = {"success": True, "updated": list(safe_cfg.keys())}
    if rejected:
        result["rejected"] = sorted(rejected)
    return result


@app.get("/api/cloud-logs")
def api_cloud_logs(device: str = "fase1", days: int = 2):
    """Fetch cloud logs on-demand (user-triggered). Requires cloud_enabled=True"""
    cfg = load_config()
    if not cfg.get("cloud_enabled", False):
        return {"error": "Cloud disabled", "cloud_enabled": False}

    device_id = DEVICES.get(device, {}).get("id", device)
    logs = get_cloud_logs(device_id, days=days, use_cache=False)
    return {"count": len(logs), "logs": logs[:100]}


@app.post("/api/cloud/enable")
def api_cloud_enable():
    """Enable cloud fetching (user decision)."""
    cfg = load_config()
    cfg["cloud_enabled"] = True
    save_config(cfg)
    return {"cloud_enabled": True, "message": "Cloud enabled. Logs will be fetched."}


@app.post("/api/cloud/disable")
def api_cloud_disable():
    """Disable cloud fetching (user decision)."""
    cfg = load_config()
    cfg["cloud_enabled"] = False
    save_config(cfg)
    return {"cloud_enabled": False, "message": "Cloud disabled. Using local data only."}


@app.get("/api/cloud/status")
def api_cloud_status():
    """Check cloud status."""
    cfg = load_config()
    return {
        "cloud_enabled": cfg.get("cloud_enabled", False),
        "cloud_cached": len(_cloud_cache),
    }


@app.get("/")
def root():
    html_path = BASE_DIR / "src" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text())
    return {
        "status": "Tuya Energy Dashboard",
        "version": "2.0-local-first",
        "message": "HTML page not found",
    }


if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════════╗
║  ⚡ Energia Dashboard — http://localhost:8050           ║
║  📍 Modo: LOCAL-FIRST (coleta local ativa)               ║
║  ☁️ Cloud: Desabilitado (ative via /api/cloud/enable)   ║
╚══════════════════════════════════════════════════════════╝
    """)

    # Recover active charge session from DB (survives service restarts)
    _cfg = load_config()
    if _cfg.get("car_charging"):
        _conn = get_db()
        try:
            _row = _conn.execute(
                "SELECT session_uuid, start_time, soc_start, soc_target, battery_kwh, start_energy_kwh, cost_per_kwh"
                " FROM charge_sessions WHERE status = 'active' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            _conn.close()

        if _row:
            _uuid, _start_ts, _soc_start, _soc_target, _bat, _start_e, _cost = _row
            # Restore tracker state from DB session
            charging.start(
                start_soc=_soc_start,
                target_soc=_soc_target,
                battery_kwh=_bat,
                start_energy_kwh=_start_e,
                session_uuid=_uuid,
            )
            # Override start_time to the DB session's real start
            charging.start_time = datetime.fromisoformat(_start_ts)
            elapsed_min = (datetime.now() - charging.start_time).total_seconds() / 60
            print(
                f"🔄 Sessão recuperada do DB: {_soc_start}% → {_soc_target}% (já decorrido: {elapsed_min:.0f} min)"
            )
        elif _cfg.get("car_charge_start_time"):
            # Fallback: config has start_time but no DB session — start fresh
            _start_wh = 0
            try:
                _d = connect_device(DEVICES["breaker"])
                _br = read_breaker(_d)
                _start_wh = _br.get("energy_wh", 0)
            except Exception:
                pass
            charging.start(
                start_soc=_cfg.get("car_current_soc", 50),
                target_soc=_cfg.get("car_target_soc", 80),
                battery_kwh=_cfg.get("car_battery_kwh", 12.9),
                start_energy_kwh=_start_wh if _start_wh else 0,
            )
            charging.start_time = datetime.fromisoformat(_cfg["car_charge_start_time"])
            print(
                f"🔄 Sessão recuperada da config: SOC {_cfg.get('car_current_soc')}% → {_cfg.get('car_target_soc')}%"
            )

    threading.Thread(target=poll_loop, daemon=True).start()
    # Bind address:
    #   ENERGIA_HOST=127.0.0.1 (default, safer — only loopback)
    #   ENERGIA_HOST=0.0.0.0   (expose to LAN; required to access from other devices)
    host = os.environ.get("ENERGIA_HOST", "127.0.0.1")
    port = int(os.environ.get("ENERGIA_PORT", "8050"))
    print(f"🌐 Dashboard em http://{host}:{port}")
    print(f"   Override via ENERGIA_HOST / ENERGIA_PORT env vars")
    uvicorn.run(app, host=host, port=port, log_level="warning")
