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
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
import os

import tinytuya
from fastapi import FastAPI, HTTPException
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
    "car_current_soc_ts": None,  # fim da última sessão real ou input explícito do usuário
    "car_charging": False,
    "car_charge_start_kwh": 0,  # Breaker energy counter at charge start
    "car_charge_start_time": None,  # ISO timestamp
    "car_charge_start_soc": None,  # SOC value at charge start
    "car_charge_idle_seconds_to_stop": 300,  # Wait this long with low power before auto-stop
    "car_charge_idle_power_w": 15,  # Power threshold to consider "idle/done"
    "car_charge_start_power_w": 500,  # Power threshold to auto-detect charging started
    "car_charge_auto_stop": True,  # Auto-stop when done
    "car_charge_efficiency": 0.85,  # Charging efficiency (grid→battery). Tune to match car app estimate.
    "cloud_enabled": False,  # Cloud OFF by default - user decides
}
# 17280 leituras/dia a 5s → ~29 dias de histórico bruto antes do prune
DB_MAX_ROWS = 500000


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
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# ─── TTL cache (evita recomputar agregados caros a cada request) ───
_ttl_cache: dict = {}
_ttl_lock = threading.Lock()


def _ttl_get(key, ttl):
    with _ttl_lock:
        hit = _ttl_cache.get(key)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
    return None


def _ttl_put(key, value):
    with _ttl_lock:
        _ttl_cache[key] = (time.time(), value)


def _sql_kwh(conn, col: str, start: str, end: str, min_avg_w: float = 0.0) -> float:
    """Integrate power×time (kWh) entirely in SQL for [start, end).

    Equivalent to _kwh_from_power_integral() but returns a single row instead
    of streaming tens of thousands of readings into Python — critical on the
    low-power CubieBoard. Range predicates keep the idx_device_time index seek;
    gaps >120 s are ignored (device offline). min_avg_w filters noise pairs.
    """
    row = conn.execute(
        f"""
        WITH p AS (
            SELECT {col} AS w,
                   LAG({col}) OVER (ORDER BY timestamp) AS prev_w,
                   (julianday(timestamp)
                     - LAG(julianday(timestamp)) OVER (ORDER BY timestamp)) * 86400.0 AS dt_s
            FROM readings
            WHERE device='fase1' AND timestamp >= ? AND timestamp < ? AND {col} IS NOT NULL
        )
        SELECT ROUND(SUM((prev_w + w) / 2000.0 * (dt_s / 3600.0)), 4)
        FROM p
        WHERE dt_s > 0 AND dt_s < 120 AND ((prev_w + w) / 2.0) >= ?
        """,
        (start, end, min_avg_w),
    ).fetchone()
    return round(row[0] or 0.0, 4)


def _kwh_snapshots_plus_missing_days(
    conn, start: str, end: str, device: str = "fase1", col: str = "power",
    min_avg_w: float = 0.0,
) -> float:
    """Month-to-date kWh from daily_snapshots, integrating ONLY days that lack
    a snapshot (today-in-progress or rare gaps).

    Snapshots are written once per day by poll_loop's rollover, so completed
    days cost a single indexed lookup instead of a full re-integration — the
    whole-month LAG scan took ~3.4 s on the CubieBoard; this takes ~10 ms.

    Funciona para os dois canais: device='fase1'/col='power' (casa) e
    device='breaker'/col='phase_c' (carro — usar min_avg_w=1.0 contra ruído).
    """
    snaps = {
        d: (e or 0)
        for d, e in conn.execute(
            "SELECT snapshot_date, energy_kwh FROM daily_snapshots "
            "WHERE device=? AND snapshot_date>=? AND snapshot_date<?",
            (device, start, end),
        ).fetchall()
    }
    total = sum(snaps.values())
    day = datetime.fromisoformat(start).date()
    last = datetime.fromisoformat(end).date()
    while day < last:
        ds = day.strftime("%Y-%m-%d")
        if ds not in snaps:
            nd = (day + timedelta(days=1)).strftime("%Y-%m-%d")
            has_readings = conn.execute(
                "SELECT 1 FROM readings WHERE device='fase1' AND timestamp>=? AND timestamp<? LIMIT 1",
                (ds, nd),
            ).fetchone()
            if has_readings:
                total += _sql_kwh(conn, col, ds, nd, min_avg_w)
        day += timedelta(days=1)
    return round(total, 4)


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


def _migrate_utc_to_brt(conn):
    """One-time migration: board ran in UTC until 2026-08-22; all stored
    timestamps are UTC. Shift readings/charge sessions to BRT (UTC-3, no DST
    in Brazil) so day boundaries and hourly charts match local time, then
    drop snapshots (computed on UTC-day windows) for rebuild by backfill.
    Idempotent via config gate `tz_brt_migrated`.
    """
    _cfg = load_config()
    if _cfg.get("tz_brt_migrated"):
        return
    n_read = conn.execute(
        "UPDATE readings SET timestamp = strftime('%Y-%m-%dT%H:%M:%S', timestamp, '-3 hours')"
    ).rowcount
    n_sess = conn.execute(
        "UPDATE charge_sessions SET "
        "start_time = strftime('%Y-%m-%dT%H:%M:%S', start_time, '-3 hours'), "
        "end_time = CASE WHEN end_time IS NULL THEN NULL "
        "ELSE strftime('%Y-%m-%dT%H:%M:%S', end_time, '-3 hours') END"
    ).rowcount
    conn.execute("DELETE FROM daily_snapshots")
    conn.commit()
    _cfg["tz_brt_migrated"] = True
    _cfg.pop("snapshots_backfilled", None)  # force snapshot rebuild (BRT days)
    _cfg["last_snapshot_day"] = datetime.now().strftime("%Y-%m-%d")
    save_config(_cfg)
    print(f"DB migration tz: {n_read} leituras e {n_sess} sessões movidas UTC→BRT (-3h); snapshots serão reconstruídos")


def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    # WAL: readers (dashboard) never block the writer (poll_loop inserts).
    conn.execute("PRAGMA journal_mode = WAL")
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
    # Migration v3: store per-day average power in the snapshot so history
    # queries never need to rescan readings (CubieBoard has a slow CPU/SD).
    cur_snap_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(daily_snapshots)").fetchall()
    }
    if "avg_power_w" not in cur_snap_cols:
        conn.execute("ALTER TABLE daily_snapshots ADD COLUMN avg_power_w REAL")
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
    _cfg = load_config()
    # Migration v2a: breaker snapshots used unscaled breaker_energy (100× too large).
    if not _cfg.get("breaker_snapshots_v2"):
        conn.execute("DELETE FROM daily_snapshots WHERE device='breaker'")
        _cfg["breaker_snapshots_v2"] = True
        _cfg.pop("snapshots_backfilled", None)
        save_config(_cfg)
        print("DB migration v2: deleted old breaker snapshots (will recompute from power readings)")
    # Migration v2b: charge_sessions stored raw DPS-1 values as kWh (100× inflated).
    if not _cfg.get("charge_sessions_v2"):
        conn.execute(
            "UPDATE charge_sessions SET "
            "start_energy_kwh = ROUND(start_energy_kwh / 100, 4), "
            "end_energy_kwh = ROUND(end_energy_kwh / 100, 4), "
            "energy_delivered_kwh = ROUND(energy_delivered_kwh / 100, 4), "
            "total_cost = ROUND(energy_delivered_kwh / 100 * cost_per_kwh, 2)"
        )
        _cfg["charge_sessions_v2"] = True
        n_fixed = conn.execute(
            "SELECT changes()"
        ).fetchone()[0]
        if n_fixed:
            print(f"DB migration v2b: fixed {n_fixed} charge_sessions (÷100 for scaling)")
        save_config(_cfg)
    # Migration tz: board ran in UTC — shift stored times to BRT (UTC-3).
    _migrate_utc_to_brt(conn)
    # Backfill v3: preenche avg_power_w dos snapshots existentes (uma única vez).
    if not _cfg.get("snapshots_avg_v1"):
        rows = conn.execute(
            "SELECT DATE(timestamp) AS day, AVG(power) FROM readings "
            "WHERE device='fase1' AND timestamp>=? AND power IS NOT NULL GROUP BY day",
            ((datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d"),),
        ).fetchall()
        for day, avg_w in rows:
            conn.execute(
                "UPDATE daily_snapshots SET avg_power_w=? WHERE snapshot_date=? AND device='fase1'",
                (round(avg_w, 1) if avg_w is not None else None, day),
            )
        _cfg["snapshots_avg_v1"] = True
        save_config(_cfg)
        print(f"DB migration v3: backfilled avg_power_w em {len(rows)} snapshots")
    # Migration v4: o fim de sessão semeava "SOC Atual do Carro" com o soc_start
    # reconciliado (estimativa de partida da próxima carga) — o dashboard
    # principal ficava preso no SOC do INÍCIO da última carga. Re-semear com o
    # soc_end da última sessão real (onde o carro está de fato).
    if not _cfg.get("car_soc_field_end_v1"):
        row = conn.execute(
            "SELECT soc_end, end_time FROM charge_sessions "
            "WHERE soc_end IS NOT NULL AND end_time IS NOT NULL "
            "AND status NOT IN ('active', 'no_charge') "
            "AND COALESCE(energy_delivered_kwh, 0) >= 0.05 "
            "ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
        _cfg["car_soc_field_end_v1"] = True
        if row:
            _cfg["car_current_soc"] = int(row[0])
            _cfg["car_current_soc_ts"] = row[1]
            print(
                f"DB migration v4: 'SOC Atual do Carro' re-semido com o fim da "
                f"última sessão real ({int(row[0])}%)"
            )
        save_config(_cfg)
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


# ─── Config ────────────────────────────────────────────────────
_cfg_cache: tuple = None  # (ts, cfg)


def load_config():
    global _cfg_cache
    now = time.time()
    if _cfg_cache and (now - _cfg_cache[0]) < 2:
        return {**_cfg_cache[1]}  # shallow copy: callers may mutate
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = {**DEFAULT_CONFIG, **json.load(f)}
    else:
        cfg = DEFAULT_CONFIG.copy()
    _cfg_cache = (now, cfg)
    return {**cfg}


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    global _cfg_cache
    _cfg_cache = (time.time(), {**cfg})


init_db()


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
    # Timeouts curtos: o default do tinytuya (5s de timeout × 5 retries)
    # fazia um device lento/offline travar a coleta por ~25s por ciclo.
    return tinytuya.Device(
        cfg["id"],
        address=cfg["ip"],
        local_key=cfg["key"],
        version=cfg["version"],
        connection_timeout=3,
        connection_retry_limit=1,
    )


def _to_num(v, default=0):
    """Safely coerce a Tuya DPS value (often int/str/None) to float."""
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


_F1_IDLE_WARNED = False


def read_fase1(d):
    """Read fase1 meter with retry + validation.

    Returns dict on success, or None if all 3 retries fail to produce a valid
    voltage (real voltage is 110-130V; <=50V means bad read). Energy (DPS 17)
    is unreliable and often None — treated as optional (returned as None, not
    a skip trigger).
    """
    global _F1_IDLE_WARNED
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
                    # Avisa só na transição pra idle — em repouso isso é o
                    # estado normal e o aviso a cada poll inundava o log.
                    if not _F1_IDLE_WARNED:
                        print(
                            f"⚠️  fase1: voltage OK ({voltage}V) but power=0 "
                            f"— idle state or bad read"
                        )
                        _F1_IDLE_WARNED = True
                else:
                    _F1_IDLE_WARNED = False
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
_last_breaker_switch = False  # cache for DPS 16 missing from partial responses
_br_energy_samples = deque(maxlen=200)  # (ts, DPS1 raw) p/ estimar potencia via delta do contador


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

    global _last_valid_energy_wh, _last_breaker_switch
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

    power_w = round(voltage_v * current_a, 1)  # V × A = W (DPS 6 — este breaker nao responde ao UPDATEDPS)

    # Estimador de potencia viva via delta do contador cumulativo (DPS 1).
    # Resolucao do contador: 1 raw = 10 Wh. Janela de ~2min suaviza a quantizacao
    # (a 2400W o contador anda ~4 unidades/60s). Saneado: sem retrocesso, cap 12kW.
    now = time.time()
    _br_energy_samples.append((now, energy_wh))
    while len(_br_energy_samples) > 1 and _br_energy_samples[0][0] < now - 120:
        _br_energy_samples.popleft()
    if len(_br_energy_samples) >= 2:
        t0, e0 = _br_energy_samples[0]
        dt = now - t0
        de = energy_wh - e0
        if power_w == 0 and dt >= 45 and 0 <= de <= 5000:
            power_w = min(de * 10.0 * 3600.0 / dt, 12000.0)

    # DPS 16 ausente numa resposta parcial (comum sob carga): repetir o
    # último valor conhecido. Defaultar para False lê "desligado" com o
    # relé fechado — ver breaker_off_externally_confirmed.
    if "16" in dps:
        _last_breaker_switch = bool(dps["16"])

    return {
        "switch": bool(_last_breaker_switch),
        "prepayment": bool(dps.get("11", False)),
        "balance_kwh": round(_to_num(dps.get("13", 0)) / 100, 2),
        "energy_kwh": round(energy_wh / 100, 4) if energy_wh else 0,  # raw/100 = kWh (cada raw unit = 10 Wh = 0.01 kWh)
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
    Also fills avg_power_w so history views never rescan readings.
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
        br_power_rows = conn.execute(
            "SELECT DATE(timestamp) AS day, timestamp, phase_c FROM readings "
            "WHERE device='fase1' AND phase_c IS NOT NULL ORDER BY timestamp"
        ).fetchall()

        # Group by day
        from collections import defaultdict

        f1_by_day = defaultdict(list)
        for day, ts, p in f1_rows:
            f1_by_day[day].append((ts, p or 0))
        br_power_by_day = defaultdict(list)
        for day, ts, pw in br_power_rows:
            br_power_by_day[day].append((ts, pw or 0))

        days = sorted(set(f1_by_day.keys()) | set(br_power_by_day.keys()))
        # NÃO incluir o dia em curso: snapshot parcial (restart/backfill no
        # meio do dia) congela a barra do dia no gráfico até a meia-noite —
        # db_daily_history integra o dia corrente ao vivo, e o rollover da
        # virada grava o snapshot fechado e completo.
        today = datetime.now().strftime("%Y-%m-%d")
        days = [d for d in days if d < today]
        n_f1, n_br = 0, 0
        for day in days:
            f1_kwh = (
                round(_kwh_from_power_integral(f1_by_day[day]), 4)
                if f1_by_day[day]
                else 0
            )
            f1_avg_w = (
                round(sum(p for _, p in f1_by_day[day]) / len(f1_by_day[day]), 1)
                if f1_by_day[day]
                else None
            )
            br_kwh = (
                round(_kwh_from_power_integral(br_power_by_day[day]), 4)
                if br_power_by_day[day]
                else 0
            )

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
                            "INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, avg_power_w, created_at) VALUES (?, 'fase1', ?, ?, ?)",
                            (day, f1_kwh, f1_avg_w, datetime.now().isoformat()),
                        )
                    else:
                        conn.execute(
                            "UPDATE daily_snapshots SET energy_kwh=?, avg_power_w=?, created_at=? WHERE snapshot_date=? AND device='fase1'",
                            (f1_kwh, f1_avg_w, datetime.now().isoformat(), day),
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
    """Insert a new active charge session. Returns the session dict (with id, uuid).

    Invariant: only ONE session may be `status='active'` at any time. Any
    leftover active rows from a previous (unfinalized) charging event are
    finalized first so today's measurements don't break into multiple lines.
    """
    finalize_stale_active_sessions(reason="manual")
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


def finalize_charge_session(session_uuid, end_energy_kwh, soc_end, end_reason="manual",
                            effective_end_time=None, charge_complete_confident=False):
    """Mark a charge session as finished. Computes totals. Saves stats for future predictions.

    effective_end_time: when the car actually stopped drawing power (excludes
    the idle confirmation tail). Falls back to now() when not provided.

    charge_complete_confident: True quando a parada foi um 'conclude' — o carro
    PAROU SOZINHO de puxar corrente com energia injetada ≥ 90% do necessário
    até o alvo. Nesse caso o 100% do carro é a referência REAL de bateria:
    soc_end é pinado em 100 (a estimativa energética pode ter ficado aquém —
    sessão 128 terminou "92%" com o carro cheio de verdade) e o soc_start é
    reconciliado de trás pra frente com o consumo medido.
    """
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
        # Duration = time the car was actually charging (exclude idle tail)
        eff_end = effective_end_time or end_dt
        duration = int(max(0, (eff_end - start_dt).total_seconds()))
        energy_delivered = max(0.0, end_energy_kwh - (start_energy or 0))
        # Avoid double-counting: also recompute soc_end from energy if not provided
        if soc_end is None and battery_kwh:
            soc_end = (soc_start or 0) + (
                energy_delivered / max(0.1, battery_kwh)
            ) * 100
            soc_end = min(100.0, soc_end)
        total_cost = energy_delivered * (cost_per_kwh or 0)
        eff = load_config().get("car_charge_efficiency", 0.85)
        eff_aprendida = None
        # Referência real de carga completa: com a parada confiante, o fim É o
        # 100% do carro. Aproveitamos a dupla (partida estimada, energia medida)
        # para aprender a eficiência real grid→bateria — a configurada pode
        # estar errada (0,85 vs ~0,94 medido), e é ela que faz a estimativa
        # energética divergir do carro.
        if (
            charge_complete_confident
            and end_reason == "auto"
            and battery_kwh
            and energy_delivered >= 2.0
        ):
            soc_end = 100.0  # o carro só para sozinho quando encheu de verdade
            if soc_start is not None and soc_start < 100:
                eff_learned = (
                    (100.0 - soc_start) / 100.0 * battery_kwh
                ) / energy_delivered
                if 0.70 <= eff_learned <= 1.0:
                    eff_aprendida = round(eff_learned, 3)
                    print(
                        f"🎓 Eficiência real aprendida: {eff:.2f} → {eff_aprendida} "
                        f"({energy_delivered:.2f} kWh no medidor para "
                        f"{100.0 - soc_start:.1f}% de bateria)"
                    )
                    eff = eff_aprendida
        # Reconciliação do SOC inicial: quando a sessão termina porque o CARRO
        # parou sozinho (end_reason='auto'), o fim é o 100% real da bateria —
        # então o soc_start verdadeiro é 100% − energia entregue ÷ eficiência.
        # O soc_start gravado no início era só estimativa (soc_end da sessão
        # anterior), que satura em 100% antes do carro terminar.
        if (
            end_reason == "auto"
            and soc_end is not None
            and soc_end >= 99
            and energy_delivered >= 0.5
            and battery_kwh
        ):
            soc_start_real = max(
                0.0, soc_end - (energy_delivered * eff / max(0.1, battery_kwh)) * 100
            )
            if soc_start is None or abs(soc_start - soc_start_real) > 0.05:
                print(
                    f"🔁 SOC inicial reconciliado: {soc_start and round(soc_start, 1)}% "
                    f"→ {soc_start_real:.1f}% ({energy_delivered:.2f} kWh até o carro parar)"
                )
            soc_start = round(soc_start_real, 2)
        # O campo "SOC Atual do Carro" reflete onde o carro ESTÁ: fim de carga
        # real → soc_end (100% quando o carro para sozinho). A estimativa de
        # partida da próxima carga NÃO vem daqui — estimate_car_soc_start usa o
        # soc_start reconciliado da última sessão completa (padrão de uso se
        # repete). Timestamp semeado = end_time (igual, não estritamente
        # posterior): input explícito do usuário continua com precedência.
        if soc_end is not None and energy_delivered >= 0.05:
            _cfg_end = load_config()
            _cfg_end["car_current_soc"] = int(soc_end)  # inteiro: piso (conservador)
            _cfg_end["car_current_soc_ts"] = end_dt.isoformat()
            if eff_aprendida is not None:
                _cfg_end["car_charge_efficiency"] = eff_aprendida
            save_config(_cfg_end)
        # Sessions that delivered no energy are marked "no_charge" so they
        # don't pollute the Carregamentos tab with phantom entries.
        if energy_delivered < 0.05:
            status = "no_charge"
        elif end_reason == "auto":
            status = "auto_stopped"
        elif end_reason == "fault":
            status = "aborted"
        else:
            status = "completed"
        conn.execute(
            """UPDATE charge_sessions
               SET end_time = ?, end_energy_kwh = ?, energy_delivered_kwh = ?,
                   duration_seconds = ?, soc_start = ?, soc_end = ?, total_cost = ?,
                   end_reason = ?, status = ?
               WHERE session_uuid = ?""",
            (
                end_dt.isoformat(),
                end_energy_kwh,
                energy_delivered,
                duration,
                soc_start,
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


def finalize_stale_active_sessions(
    end_energy_kwh=None, current_soc=None, reason="manual", keep_uuid=None
):
    """Finalize any leftover `status='active'` rows.

    Bug guard: at most ONE charge session should be live at a time. If a previous
    session was never finalized (e.g. service crash, breaker toggled externally,
    /api/car/start-charge called twice), the DB would show multiple "active"
    rows for the same logical charging event — breaking today's measurements
    across multiple lines in the Carregamentos tab.

    Call this BEFORE creating a new session, and during DB recovery on startup.
    Returns the list of finalized session_uuids (for logging).

    Pass `keep_uuid` to leave a specific session active (used by startup
    recovery to rehydrate the most recent live session).
    """
    conn = get_db()
    finalized = []
    try:
        rows = conn.execute(
            "SELECT session_uuid, start_time, start_energy_kwh, cost_per_kwh,"
            " battery_kwh, soc_start"
            " FROM charge_sessions WHERE status = 'active'"
        ).fetchall()
        if not rows:
            return finalized
        for row in rows:
            (s_uuid, start_ts, start_e, cost, bat, soc_s) = row
            if keep_uuid and s_uuid == keep_uuid:
                continue
            end_e = end_energy_kwh if end_energy_kwh is not None else (start_e or 0)
            try:
                _ = finalize_charge_session(
                    s_uuid,
                    end_energy_kwh=end_e,
                    soc_end=current_soc,
                    end_reason=reason,
                )
                finalized.append(s_uuid)
                print(
                    f"🧹 Finalized stale active session {s_uuid[:8]} "
                    f"(reason={reason})"
                )
            except Exception as e:
                print(f"⚠️ Failed to finalize stale session {s_uuid[:8]}: {e}")
    finally:
        conn.close()
    return finalized


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


def list_charge_sessions(limit=50, include_active=False, days=None, since=None):
    """Return recent charge sessions, most recent first.

    When `include_active=True`, only the most recent active session is
    returned — invariants in `create_charge_session` guarantee at most one
    live session, but defensive guard in case older "ghost" active rows
    predate the fix.

    Período opcional: `since` (ISO naive local) tem precedência sobre `days`;
    sem nenhum dos dois não há corte (comportamento antigo). A comparação
    lexicográfica vale porque os timestamps gravados são ISO local.
    """
    if since is None and days is not None:
        since = (datetime.now() - timedelta(days=days)).isoformat()
    conn = get_db()
    try:
        period_where = " WHERE start_time >= ?" if since is not None else ""
        period_and = " AND start_time >= ?" if since is not None else ""
        args = (since, limit) if since is not None else (limit,)
        if include_active:
            rows = conn.execute(
                f"""SELECT id, session_uuid, start_time, end_time, status, soc_start, soc_end,
                          soc_target, battery_kwh, start_energy_kwh, end_energy_kwh,
                          energy_delivered_kwh, duration_seconds, avg_power_w, cost_per_kwh, total_cost, end_reason
                   FROM charge_sessions{period_where}
                   ORDER BY
                       CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                       start_time DESC
                   LIMIT ?""",
                args,
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT id, session_uuid, start_time, end_time, status, soc_start, soc_end,
                          soc_target, battery_kwh, start_energy_kwh, end_energy_kwh,
                          energy_delivered_kwh, duration_seconds, avg_power_w, cost_per_kwh, total_cost, end_reason
                   FROM charge_sessions
                   WHERE status != 'active'{period_and}
                   ORDER BY start_time DESC LIMIT ?""",
                args,
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


def charge_sessions_summary(days=90, limit_days=None, since=None):
    """Compute summary stats over recent charge sessions. Accepts `days`,
    `limit_days` ou `since` (ISO naive local — sobrepõe days, ex. "este mês")."""
    if since is None:
        if limit_days is None:
            limit_days = days
        since = (datetime.now() - timedelta(days=limit_days)).isoformat()
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(energy_delivered_kwh), 0),
                      COALESCE(SUM(total_cost), 0), COALESCE(SUM(duration_seconds), 0),
                      COALESCE(AVG(energy_delivered_kwh), 0), COALESCE(AVG(total_cost), 0),
                      COALESCE(MIN(soc_start), 0), COALESCE(MAX(soc_end), 0)
               FROM charge_sessions
               WHERE status NOT IN ('active', 'no_charge') AND start_time >= ?""",
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


def estimate_car_soc_start(cfg):
    """Estimate the car's SOC when a session starts without explicit input
    (breaker flipped manually — auto-detect path).

    Ciclo de aprendizado: quando a última sessão terminou com o carro cheio
    (auto + soc_end ≥ 99), o soc_start dela foi reconciliado no fim para o
    SOC REAL de partida — é a melhor estimativa para a próxima plugada
    (padrão de uso se repete), muito melhor que o soc_end=100 que satura.
    Sessão que NÃO terminou cheia → usa o soc_end dela (último SOC conhecido).
    Um /api/car/soc explícito informado DEPOIS do fim dessa sessão ganha
    (o usuário sabe: rodou com o carro). Sem histórico → car_current_soc.

    Returns (soc, estimated) where estimated=False means the value came from
    explicit user input.
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT soc_end, soc_start, end_time, end_reason FROM charge_sessions "
            "WHERE status NOT IN ('active', 'no_charge') AND soc_end IS NOT NULL "
            "AND end_time IS NOT NULL "
            "ORDER BY start_time DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    cfg_soc = cfg.get("car_current_soc")
    cfg_ts = cfg.get("car_current_soc_ts")

    def _clamp(v):
        return max(0.0, min(100.0, float(v)))

    if row:
        soc_end, soc_start, end_time, end_reason = row
        if cfg_soc is not None and cfg_ts:
            try:
                if datetime.fromisoformat(str(cfg_ts)) > datetime.fromisoformat(
                    str(end_time)
                ):
                    return _clamp(cfg_soc), False
            except ValueError:
                pass  # timestamp corrompido — trata como não informado
        # Sessão completa (carro encheu sozinho): usa o SOC inicial reconciliado
        if end_reason == "auto" and soc_end >= 99 and soc_start is not None:
            return int(_clamp(soc_start)), True
        return int(_clamp(soc_end)), True
    if cfg_soc is not None:
        return int(_clamp(cfg_soc)), False
    return 50.0, True


def update_session_soc_start(session_uuid, soc_start):
    """Persist a mid-session SOC correction to the session's soc_start so the
    Carregamentos tab reports the corrected value."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE charge_sessions SET soc_start = ? WHERE session_uuid = ?",
            (soc_start, session_uuid),
        )
        conn.commit()
    finally:
        conn.close()


def _detect_charge_windows(
    rows, active_threshold_w=50, bridge_gap_s=900.0
):
    """Group (timestamp, power_w) readings into charging windows.

    A sample is 'active' when power > active_threshold_w. Consecutive active
    samples closer than bridge_gap_s belong to the same window — the counter-
    delta power estimator dips below the threshold for seconds at a time, so
    short gaps MUST bridge or one charge becomes dozens of fragments.
    Returns [(first_ts, last_ts, samples)] with samples=[(ts, w), ...].
    """
    windows = []
    cur = None
    for ts_str, w in rows:
        if w <= active_threshold_w:
            continue
        if cur is not None and (
            datetime.fromisoformat(ts_str) - datetime.fromisoformat(cur[1])
        ).total_seconds() <= bridge_gap_s:
            cur[1] = ts_str
            cur[2].append((ts_str, w))
        else:
            if cur is not None:
                windows.append(cur)
            cur = [ts_str, ts_str, [(ts_str, w)]]
    if cur is not None:
        windows.append(cur)
    return windows


def backfill_charge_sessions_from_readings():
    """Recreate charge sessions for charging events that were measured but
    never recorded (service was down/restarted mid-charge, or the pre-2026-09
    auto-detect bug left breaker-ON charging without a session).

    Windows of breaker power >50 W (gaps ≤15 min bridged) are integrated the
    same way the daily history does it, so Histórico and Carregamentos
    reconcile. Windows overlapping an existing session (±5 min) are skipped.
    Reconstructed rows get status='reconstructed' (no SOC info — the car's
    dashboard state at the time is unknown) and are included in summaries.

    One-shot per install (gate in config `charge_sessions_backfill_v1`).
    """
    _cfg = load_config()
    if _cfg.get("charge_sessions_backfill_v1"):
        return 0

    conn = get_db()
    created = 0
    try:
        existing = conn.execute(
            "SELECT start_time, end_time FROM charge_sessions"
        ).fetchall()
        existing_spans = [
            (
                datetime.fromisoformat(s),
                datetime.fromisoformat(e) if e else None,
            )
            for s, e in existing
        ]
        rows = conn.execute(
            "SELECT timestamp, phase_c, breaker_energy FROM readings "
            "WHERE device='fase1' AND phase_c IS NOT NULL ORDER BY timestamp"
        ).fetchall()
        energy_by_ts = {t: e for t, _w, e in rows if e is not None}
        windows = _detect_charge_windows(
            [(t, w) for t, w, _e in rows]
        )
        cost = _cfg.get("kwh_cost", 0.956)

        for first_ts, last_ts, samples in windows:
            # Integral idêntico ao do Histórico (_sql_kwh/_kwh_from_power_integral)
            energy = 0.0
            for i in range(1, len(samples)):
                t1, p1 = samples[i - 1]
                t2, p2 = samples[i]
                dt_s = (
                    datetime.fromisoformat(t2) - datetime.fromisoformat(t1)
                ).total_seconds()
                if 0 < dt_s < 120 and (p1 + p2) / 2 > 0:
                    energy += (p1 + p2) / 2000.0 * (dt_s / 3600.0)
            if energy < 0.05:
                continue
            duration = int(
                max(
                    0,
                    (
                        datetime.fromisoformat(last_ts)
                        - datetime.fromisoformat(first_ts)
                    ).total_seconds(),
                )
            )

            # Delta do contador cumulativo (DPS 1) é mais preciso que a
            # integral quando não houve falhas de leitura. Só aceita delta
            # plausível (0 ≤ delta ≤ integral + folga); senão fica a integral.
            start_e = end_e = None
            delta = None
            if len(samples) >= 2:
                e_first = energy_by_ts.get(samples[0][0])
                e_last = energy_by_ts.get(samples[-1][0])
                if e_first is not None and e_last is not None:
                    delta = round(e_last - e_first, 4)
                    start_e, end_e = round(e_first, 4), round(e_last, 4)
            if delta is None or delta < 0.05 or delta > energy + 0.5 + 0.3 * energy:
                delta = round(energy, 4)
                start_e = end_e = None

            w_start = datetime.fromisoformat(first_ts)
            w_end = datetime.fromisoformat(last_ts)
            tol = timedelta(minutes=5)
            if any(
                s <= w_end + tol and (e is None or e >= w_start - tol)
                for s, e in existing_spans
            ):
                continue  # já registrada (ou sessão ativa em curso)

            conn.execute(
                """INSERT INTO charge_sessions
                   (session_uuid, start_time, end_time, status,
                    start_energy_kwh, end_energy_kwh, energy_delivered_kwh,
                    duration_seconds, avg_power_w, cost_per_kwh, total_cost,
                    end_reason)
                   VALUES (?, ?, ?, 'reconstructed', ?, ?, ?, ?, ?, ?, ?,
                           'reconstructed')""",
                (
                    str(_uuid.uuid4()),
                    first_ts,
                    last_ts,
                    start_e,
                    end_e,
                    delta,
                    duration,
                    # delta em kWh × 1000 → potência média em W
                    round(delta * 1000 / (duration / 3600.0), 0)
                    if duration > 0
                    else 0,
                    cost,
                    round(delta * cost, 2),
                ),
            )
            created += 1
            print(
                f"🔁 Sessão recuperada das leituras: {first_ts[:16]} → "
                f"{last_ts[:16]} ({delta:.2f} kWh, {duration // 60} min)"
            )
        conn.commit()
        _cfg2 = load_config()
        _cfg2["charge_sessions_backfill_v1"] = True
        save_config(_cfg2)
        if created:
            print(f"🔁 Backfill de sessões: {created} recuperadas das leituras")
    finally:
        conn.close()
    return created


# ─── LOCAL-FIRST DB queries ─────────────────────────────────────
def db_today_stats():
    """Calculate today's consumption using LOCAL data.

    Uses POWER × TIME integral (much more accurate than energy counter delta).
    Handles resets automatically since we track power directly.
    All integrals run in SQL (single-row results) instead of pulling every
    reading of the month into Python.

    Custo hoje/mês = (fase1 + disjuntor do carro) × tarifa — circuitos
    separados, os dois passam pela conta de luz.
    """
    cfg = load_config()
    cost = cfg.get("kwh_cost", 0.956)
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")

    conn = get_db()
    try:
        # Today + breaker integrals: bounded index-range queries in SQL.
        today_kwh = _sql_kwh(conn, "power", today, tomorrow)
        breaker_kwh = _sql_kwh(conn, "phase_c", today, tomorrow, min_avg_w=1.0)

        count = conn.execute(
            "SELECT COUNT(*) FROM readings WHERE device='fase1' AND timestamp>=? AND timestamp<?",
            (today, tomorrow),
        ).fetchone()[0]

        # Month-to-date: snapshots-first (cheap); recompute at most every 60 s.
        # Casa e carro são circuitos SEPARADOS (fase1 não inclui o carro) — o
        # CUSTO cobre os dois (fase1 + disjuntor), como a conta de luz.
        has_month = conn.execute(
            "SELECT 1 FROM readings WHERE device='fase1' AND timestamp>=? LIMIT 1",
            (month_start,),
        ).fetchone()

        mkey = ("month_kwh", month_start)
        month_kwh = _ttl_get(mkey, 60)
        if month_kwh is None:
            if has_month:
                month_kwh = _kwh_snapshots_plus_missing_days(conn, month_start, tomorrow)
            else:
                # No readings this month: fall back to daily snapshot sums.
                row = conn.execute(
                    "SELECT SUM(energy_kwh) FROM daily_snapshots WHERE snapshot_date>=? AND device='fase1'",
                    (month_start,),
                ).fetchone()
                month_kwh = round(max(0, (row[0] or 0) if row else 0), 4)
            _ttl_put(mkey, month_kwh)

        mkey_br = ("month_kwh_br", month_start)
        month_br_kwh = _ttl_get(mkey_br, 60)
        if month_br_kwh is None:
            if has_month:
                month_br_kwh = _kwh_snapshots_plus_missing_days(
                    conn, month_start, tomorrow,
                    device="breaker", col="phase_c", min_avg_w=1.0,
                )
            else:
                row = conn.execute(
                    "SELECT SUM(energy_kwh) FROM daily_snapshots WHERE snapshot_date>=? AND device='breaker'",
                    (month_start,),
                ).fetchone()
                month_br_kwh = round(max(0, (row[0] or 0) if row else 0), 4)
            _ttl_put(mkey_br, month_br_kwh)

        return {
            "today_kwh": today_kwh,
            "today_cost": round((today_kwh + breaker_kwh) * cost, 2),
            "month_kwh": round(month_kwh, 4),
            "month_cost": round((month_kwh + month_br_kwh) * cost, 2),
            "kwh_cost": cost,
            "source": "local",
            "readings": count,
            "breaker_kwh": breaker_kwh,
            "month_breaker_kwh": round(month_br_kwh, 4),
        }
    finally:
        conn.close()


def db_daily_history(days=30, year=None, month=None):
    """Return daily consumption from LOCAL snapshots.

    Snapshots store **consumed kWh per day** (already integrated from power×time),
    not cumulative readings — so we return them directly without diffing.
    Queries are bounded to the requested window so the index does the work.

    Modo mês: `year`+`month` retornam o calendário daquele mês (usado pela
    navegação ◀ ▶ do Histórico). Sem eles: últimos `days` dias.
    """
    cfg = load_config()
    cost = cfg.get("kwh_cost", 0.956)

    conn = get_db()
    try:
        today = datetime.now().date()
        today_str = today.strftime("%Y-%m-%d")
        if year and month:
            first = datetime(year, month, 1)
            nxt = datetime(year + (1 if month == 12 else 0),
                           1 if month == 12 else month + 1, 1)
            last = nxt - timedelta(days=1)
            sorted_days = [
                (first + timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range((last - first).days + 1)
            ]
            # dias futuros do mês corrente não existem — fora da tabela/gráfico
            sorted_days = [d for d in sorted_days if d <= today_str]
            start_day = sorted_days[0]
            tomorrow = nxt.strftime("%Y-%m-%d")
        else:
            start_day = (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
            tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            sorted_days = [
                (today - timedelta(days=d)).strftime("%Y-%m-%d")
                for d in range(days - 1, -1, -1)
            ]

        # Get snapshots within the window only
        snap_rows = conn.execute(
            "SELECT snapshot_date, device, energy_kwh, avg_power_w "
            "FROM daily_snapshots WHERE snapshot_date>=?",
            (start_day,),
        ).fetchall()

        f1_daily = {}
        br_daily = {}
        f1_avg = {}
        for row in snap_rows:
            day, dev, energy, avg_w = row
            if dev == "fase1":
                f1_daily[day] = energy
                f1_avg[day] = round(avg_w, 1) if avg_w is not None else 0
            else:
                br_daily[day] = energy

        # Get last N days, fill missing with 0, compute from readings when missing
        result = []

        # Today has no snapshot yet (rollover closes it after midnight):
        # average power comes from its own small range query.
        today_avg = 0.0
        if today_str in sorted_days and f1_daily.get(today_str) is None:
            r = conn.execute(
                "SELECT AVG(power) FROM readings WHERE device='fase1' "
                "AND timestamp>=? AND timestamp<? AND power IS NOT NULL",
                (today_str, tomorrow),
            ).fetchone()
            today_avg = round(r[0], 1) if r and r[0] is not None else 0.0

        for i, day in enumerate(sorted_days):
            f1 = f1_daily.get(day, 0)
            br = br_daily.get(day, 0)

            next_day = sorted_days[i + 1] if i + 1 < len(sorted_days) else tomorrow

            # Dias fechados usam o snapshot (autoritativo). O dia EM CURSO é
            # sempre integrado ao vivo — um snapshot dele (backfill pós-migração
            # ou restart) é parcial e congelaria a barra do dia até a meia-noite.
            if day == today_str or f1 == 0:
                f1 = _sql_kwh(conn, "power", day, next_day)

            # Breaker (carro) — phase_c stores breaker power.
            if day == today_str or br == 0:
                br = _sql_kwh(conn, "phase_c", day, next_day)

            result.append(
                {
                    "date": day,
                    "consumed_kwh": f1,
                    "phase1_kwh": f1_daily.get(day, 0),
                    "breaker_kwh": br,
                    "breaker_cost": round(br * cost, 2),
                    "cost": round(f1 * cost, 2),
                    "avg_power_w": f1_avg.get(day) or (today_avg if day == today_str else 0),
                }
            )

        return result
    finally:
        conn.close()


def db_hourly(date=None):
    """Return hourly consumption from local readings using power×time integration.

    Single SQL query (LAG window per hour) instead of streaming all of the
    day's readings into Python.
    """
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    next_day = (datetime.fromisoformat(date) + timedelta(days=1)).strftime("%Y-%m-%d")

    conn = get_db()
    try:
        rows = conn.execute(
            """
            WITH p AS (
                SELECT strftime('%H', timestamp) AS hh,
                       power AS w,
                       LAG(power) OVER (
                           PARTITION BY strftime('%H', timestamp) ORDER BY timestamp
                       ) AS prev_w,
                       (julianday(timestamp) - LAG(julianday(timestamp)) OVER (
                           PARTITION BY strftime('%H', timestamp) ORDER BY timestamp
                       )) * 86400.0 AS dt_s
                FROM readings
                WHERE device='fase1' AND timestamp >= ? AND timestamp < ? AND power IS NOT NULL
            )
            SELECT hh, COUNT(*), AVG(w),
                   SUM(CASE WHEN dt_s > 0 AND dt_s < 120
                            THEN (prev_w + w) / 2000.0 * (dt_s / 3600.0) END)
            FROM p GROUP BY hh ORDER BY hh
            """,
            (date, next_day),
        ).fetchall()

        if not rows:
            return {"date": date, "hours": [], "total_kwh": 0, "breaker_kwh": 0.0,
                    "total_cost": 0, "source": "local"}

        by_hour = {r[0]: r for r in rows}

        # Build array of 24 hour entries (frontend expects an array, not a dict)
        hours = []
        total_kwh = 0.0
        for h in range(24):
            hh = f"{h:02d}"
            r = by_hour.get(hh)
            if r and r[1]:
                cnt = r[1]
                avg_power = r[2] or 0
                kwh = round(r[3], 4) if cnt > 1 and r[3] is not None else 0
            else:
                cnt, avg_power, kwh = 0, 0, 0
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
        # Custo do dia cobre os DOIS circuitos: casa (fase1) + carro (breaker).
        breaker_kwh = _sql_kwh(conn, "phase_c", date, next_day, min_avg_w=1.0)
        return {
            "date": date,
            "hours": hours,
            "total_kwh": round(total_kwh, 4),
            "breaker_kwh": round(breaker_kwh, 4),
            "total_cost": round((total_kwh + breaker_kwh) * cost, 2),
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
        1. User clicks "Start" (or poll_loop auto-detects breaker ON with load)
           → start() → state = CHARGING
        2. While CHARGING, we keep measuring energy delta + power draw
        3. When power drops to idle → state = COMPLETING
        4. We wait car_charge_idle_seconds_to_stop to confirm the car really stopped
        5. auto_stop_action() then decides HOW to end, using the energy already
           injected as the tie-breaker (pausa do carro ≠ carga completa):
           - energy delivered ≈ what's needed to reach the target → 'conclude'
             (car is full — open the breaker and finalize);
           - little energy injected → the pause may be transient (BMS
             balancing, charger thermal, renegotiation): 'probe_open' opens
             the relay for PROBE_OPEN_SECONDS, 'probe_close' re-closes it and
             the poll loop observes — car resumes ('resumed') → back to
             CHARGING; still zero after the idle window → 'probe_conclude'.
        6. Then turn breaker OFF → stop() → state = IDLE (ready for the next
           session — auto-detect only fires from IDLE, so "done" must not linger)
    """

    STATE_IDLE = "idle"
    STATE_CHARGING = "charging"
    STATE_COMPLETING = "completing"  # target reached, waiting for car to stop pulling
    STATE_ERROR = "error"

    # Duração da fase "relé aberto" da sonda de retomada. Passado esse tempo
    # sem retomada, o relé é religado e a observação começa (ver lifecycle 5).
    PROBE_OPEN_SECONDS = 120
    # Fração da energia necessária até o alvo (alvo − SOC inicial, com
    # eficiência) que já garante carga completa: "quase 100% injetado"
    # dispensa a sonda. Abaixo disso, uma pausa pode ser só uma pausa.
    CONFIDENT_ENERGY_FRACTION = 0.9

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
        # True average power over the whole session (not just last 3 samples)
        self.power_sum = 0.0
        self.power_count = 0
        # Last moment the car was actually drawing power (above idle threshold).
        # Used to compute effective charging duration excluding the idle tail.
        self.last_active_time = None
        self.idle_seconds_needed = 300  # updated every poll from config
        self.efficiency = 0.80  # grid→battery efficiency, updated from config
        # Sonda de retomada (ver lifecycle): 'none' | 'open' (relé aberto,
        # aguardando PROBE_OPEN_SECONDS) | 'observe' (relé religado, aguardando
        # o carro retomar ou estourar a janela de idle).
        self.probe_phase = "none"
        self.probe_phase_since = None

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
            self.power_sum = 0.0
            self.power_count = 0
            self.last_active_time = None
            self.probe_phase = "none"
            self.probe_phase_since = None

    def stop(self, reason="manual"):
        """End the session and return to IDLE unconditionally.

        Regressão: antes o auto-stop deixava state=STATE_DONE, e a
        auto-detecção no poll_loop só dispara a partir de IDLE — o próximo
        carregamento (disjuntor ligado manualmente) ficava sem sessão e sem
        registro no DB até um restart do serviço. Fora de CHARGING/COMPLETING
        o único estado válido é IDLE.
        """
        with self.lock:
            self.state = self.STATE_IDLE
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
            self.power_sum = 0.0
            self.power_count = 0
            self.last_active_time = None
            self.probe_phase = "none"
            self.probe_phase_since = None

    def apply_soc_correction(self, new_soc):
        """User corrected the SOC mid-session (dashboard input).

        Rebases start_soc so effective_soc reflects the informed value right
        away, keeping the energy-based slope for the rest of the session.
        Returns the rebased start_soc, or None when no session is live.
        """
        with self.lock:
            if self.state not in (self.STATE_CHARGING, self.STATE_COMPLETING):
                return None
            last_e = (
                self.energy_samples[-1][1]
                if self.energy_samples
                else self.start_energy_kwh
            )
            energy_delta = max(0.0, last_e - self.start_energy_kwh)
            gained = (
                energy_delta * self.efficiency / max(0.1, self.battery_kwh)
            ) * 100
            self.start_soc = max(0.0, min(100.0, float(new_soc)) - gained)
            self.effective_soc = max(0.0, min(100.0, float(new_soc)))
            return self.start_soc

    def update(
        self, current_energy_kwh, current_power_w, idle_power_w, idle_seconds_needed,
        efficiency=None,
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
            self.idle_seconds_needed = idle_seconds_needed
            if efficiency is not None:
                self.efficiency = max(0.1, min(1.0, efficiency))
            self.energy_samples.append((now, current_energy_kwh))
            # Trim old samples (keep last 30 min)
            cutoff = now - timedelta(minutes=30)
            self.energy_samples = [
                (t, e) for t, e in self.energy_samples if t >= cutoff
            ]

            # Compute energy delta since start
            energy_delta = max(0.0, current_energy_kwh - self.start_energy_kwh)

            # Compute effective SOC from energy delivered (adjusted by efficiency)
            # SOC% = start_soc + (energy_delta_kwh * efficiency / battery_kwh) * 100
            self.effective_soc = (
                self.start_soc
                + (energy_delta * self.efficiency / max(0.1, self.battery_kwh)) * 100
            )
            self.effective_soc = min(100.0, self.effective_soc)

            # Maintain rolling avg of last 30s of power samples
            self.power_samples.append(current_power_w)
            if len(self.power_samples) > 3:  # ~30s at 10s poll
                self.power_samples = self.power_samples[-3:]

            # Track peak power seen during this charge session (for idle detection)
            if current_power_w > self.peak_power_w:
                self.peak_power_w = current_power_w

            # Accumulate for true session-wide average power
            self.power_sum += current_power_w
            self.power_count += 1

            # Decision logic (power-based — see method docstring)
            target_reached = self.effective_soc >= self.target_soc
            power_idle = current_power_w <= idle_power_w

            if not power_idle:
                self.last_active_time = now

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

    def is_probing(self):
        """True enquanto a sonda de retomada está em curso.

        Enquanto True, o ramo "Breaker desligado externamente" do poll_loop
        NÃO pode finalizar a sessão: o relé está aberto por decisão nossa
        (fase 'open'), não por ação externa.
        """
        return self.probe_phase != "none"

    def _reset_probe_locked(self):
        self.probe_phase = "none"
        self.probe_phase_since = None

    def _charge_complete_confident_locked(self):
        """A energia já injetada cobre o que faltava até o alvo?

        Necessária (na rede) = (alvo − SOC inicial)/100 × bateria ÷ eficiência.
        Com ≥ CONFIDENT_ENERGY_FRACTION disso injetado, o carro só pode ter
        parado porque encheu — conclui sem sonda. Com pouca energia injetada
        NÃO dá para concluir: pode ser pausa transitória OU o carro pode ter
        começado com SOC real maior que a estimativa (a estimativa erra para
        baixo) — nesses casos a sonda decide.
        """
        needed_grid_kwh = (
            (max(0.0, self.target_soc - self.start_soc) / 100.0)
            * self.battery_kwh
            / max(0.1, self.efficiency)
        )
        if needed_grid_kwh <= 0:
            return True  # começou no alvo ou além
        delivered = max(0.0, self.last_energy_kwh - self.start_energy_kwh)
        return delivered >= self.CONFIDENT_ENERGY_FRACTION * needed_grid_kwh

    def mark_probe_closed(self):
        """Relé religado após a fase 'open' — começa a observação."""
        with self.lock:
            if self.probe_phase == "open":
                self.probe_phase = "observe"
                self.probe_phase_since = datetime.now()
                self.message = "Sonda: relé religado — aguardando o carro retomar"

    def auto_stop_action(
        self, power_w, idle_power_w, idle_seconds_needed, resume_power_w,
        probe_open_seconds=None,
    ):
        """Decide o próximo passo quando a potência caiu ao idle.

        Retorna uma das ações para o poll_loop executar:
            'conclude'       carga completa confirmada — abre o relé e finaliza
            'probe_open'     pausa longe do alvo — abre o relé por
                             probe_open_seconds e torna a avaliar
            'probe_close'    fim da fase 'open' — religa o relé e observa
            'resumed'        carro voltou a carregar durante a sonda — segue
                             a sessão (tracker já está em CHARGING)
            'probe_conclude' carro não retomou na observação — desliga e finaliza
            None             nada a fazer neste poll

        Chamar em TODO poll enquanto a sessão vive, inclusive com o relé
        aberto pela própria sonda (switch=0) — é a única forma de a fase
        'open' chegar ao fim. Nenhum sleep: as transições são por timestamp,
        o loop de coleta nunca bloqueia.
        """
        with self.lock:
            if self.state not in (self.STATE_CHARGING, self.STATE_COMPLETING):
                return None
            now = datetime.now()
            if probe_open_seconds is None:
                probe_open_seconds = self.PROBE_OPEN_SECONDS

            # ── Sonda em curso ──
            if self.probe_phase == "open":
                if power_w >= resume_power_w:
                    # Alguém religou o relé por fora e o carro voltou a puxar:
                    # trata como retomada.
                    self._reset_probe_locked()
                    self.state = self.STATE_CHARGING
                    self.idle_started_at = None
                    self.message = "Carro voltou a consumir durante a sonda"
                    return "resumed"
                if self.probe_phase_since and (
                    (now - self.probe_phase_since).total_seconds()
                    >= probe_open_seconds
                ):
                    return "probe_close"
                return None
            if self.probe_phase == "observe":
                if power_w >= resume_power_w:
                    # Carro retomou após o religamento — a pausa era
                    # transitória; segue a carga (próxima pausa sonda de novo).
                    self._reset_probe_locked()
                    self.state = self.STATE_CHARGING
                    self.idle_started_at = None
                    self.message = "Carro retomou a carga — sonda concluída"
                    return "resumed"
                if self.probe_phase_since and (
                    (now - self.probe_phase_since).total_seconds()
                    >= idle_seconds_needed
                ):
                    return "probe_conclude"
                return None

            # ── Sem sonda: exige a janela de idle inteira antes de decidir ──
            if self.state != self.STATE_COMPLETING or not self.idle_started_at:
                return None
            elapsed = (now - self.idle_started_at).total_seconds()
            if elapsed < idle_seconds_needed:
                return None
            if self._charge_complete_confident_locked():
                return "conclude"
            # Pausa com energia muito abaixo do necessário: BMS/balanceamento,
            # térmica do carregador ou renegociação — sondar antes de desligar.
            self.probe_phase = "open"
            self.probe_phase_since = now
            self.message = (
                "Pausa com carga incompleta — testando retomada "
                f"(relé abre por {probe_open_seconds}s)"
            )
            return "probe_open"

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
                    "idle_seconds_needed": self.idle_seconds_needed,
                }

            now = datetime.now()
            elapsed = (now - self.start_time).total_seconds()
            energy_delta = (
                max(0.0, self.energy_samples[-1][1] - self.start_energy_kwh)
                if self.energy_samples
                else 0
            )

            # ── Estimate remaining time ──
            # Only show a prediction when the car is actively drawing power.
            # In COMPLETING state the car stopped — no meaningful estimate.
            need_soc = max(0, self.target_soc - self.effective_soc)
            # Energy needed at battery, then grossed up for charging losses
            need_kwh_battery = (need_soc / 100) * self.battery_kwh
            need_kwh_grid = need_kwh_battery / max(0.1, self.efficiency)
            est_min = None
            if self.state == self.STATE_CHARGING and need_kwh_grid > 0:
                avg_power_w = (
                    sum(self.power_samples) / max(1, len(self.power_samples))
                    if self.power_samples
                    else self.last_power_w
                )
                if avg_power_w > 10:
                    est_min = (need_kwh_grid / (avg_power_w / 1000)) * 60
                elif energy_delta > 0.05:
                    # Current reading is low but we DID deliver energy — use
                    # the session-wide average as a better estimate.
                    sess_avg = self.session_avg_power_w
                    if sess_avg > 10:
                        est_min = (need_kwh_grid / (sess_avg / 1000)) * 60

            # ── Idle seconds + real-time message ──
            idle_seconds = 0
            message = self.message
            if self.idle_started_at:
                idle_seconds = (now - self.idle_started_at).total_seconds()
                if self.state == self.STATE_COMPLETING:
                    remaining = max(0, int(self.idle_seconds_needed - idle_seconds))
                    if remaining > 0:
                        message = f"Confirmando carga completa... {remaining}s"
                    else:
                        message = f"Pronto para desligar ({int(idle_seconds)}s idle)"
            # Sonda em curso tem precedência na mensagem: o relé está aberto
            # (ou recém-religado) por decisão nossa, não é fim de carga.
            if self.probe_phase == "open":
                remaining = 0
                if self.probe_phase_since:
                    remaining = max(
                        0,
                        int(
                            self.PROBE_OPEN_SECONDS
                            - (now - self.probe_phase_since).total_seconds()
                        ),
                    )
                message = (
                    f"Pausa detectada — testando retomada "
                    f"(relé aberto, religa em {remaining}s)"
                )
            elif self.probe_phase == "observe":
                message = "Testando retomada: relé religado, aguardando o carro"

            return {
                "state": self.state,
                "charging": self.state in (self.STATE_CHARGING, self.STATE_COMPLETING),
                "message": message,
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
                "idle_seconds_needed": self.idle_seconds_needed,
                "probe_phase": self.probe_phase,
                "current_power_w": self.last_power_w,
            }

    @property
    def last_energy_kwh(self):
        if self.energy_samples:
            return self.energy_samples[-1][1]
        return self.start_energy_kwh

    @property
    def session_avg_power_w(self):
        """True average power over all poll readings in this session."""
        if self.power_count > 0:
            return self.power_sum / self.power_count
        return 0.0

    @property
    def effective_end_time(self):
        """When the car actually stopped drawing power (excludes idle tail).

        Returns last_active_time if available, otherwise start_time.
        Used to compute effective charging duration without the idle
        confirmation window.
        """
        return self.last_active_time or self.start_time


charging = ChargingTracker()


def breaker_idle_watchdog_should_stop(
    breaker_on, session_active, power_w, idle_power_w,
    idle_since, idle_seconds_needed, now=None,
):
    """True quando o disjuntor está ON sem sessão ativa e sem consumo relevante
    há tempo suficiente — sinaliza que o watchdog deve desligar o disjuntor."""
    if not breaker_on or session_active:
        return False
    if power_w > idle_power_w:
        return False
    if idle_since is None:
        return False
    now = now or datetime.now()
    return (now - idle_since).total_seconds() >= idle_seconds_needed


def breaker_off_confirmed(off_streak, confirm_polls=3):
    """True quando switch=0 persistiu por polls consecutivos suficientes.

    Leituras Tuya pela LAN ocasionalmente retornam payload vazio/corrompido,
    e o DPS 16 decodifica como False mesmo com o relé fisicamente fechado
    (potência continua fluindo). Tratar uma única leitura ruim como
    "desligado externamente" finaliza a sessão em curso, e a auto-detecção
    do poll seguinte cria outra — quebrando um carregamento contínuo em
    várias linhas na aba Carregamentos. Exigir `confirm_polls` leituras
    consecutivas elimina o ruído sem atrasar significativamente a detecção
    de um desligamento real.
    """
    return off_streak >= confirm_polls


def breaker_off_externally_confirmed(off_streak, off_idle_streak, confirm_polls=3):
    """True quando o desligamento externo está confirmado por DOIS critérios.

    switch=0 persistente (off_streak) E potência abaixo do idle persistente
    (off_idle_streak). O segundo critério existe porque o DPS 16 queima como
    False em RAJADA com o relé fechado: nos incidentes de 2026-09-18 e
    2026-09-20, duas leituras consecutivas corrompidas viraram 4 polls do
    loop (poll=5s, leitura do breaker=10s), estouraram o debounce de 3 e a
    sessão foi finalizada no meio da carga com ~2.7kW fluindo — a
    auto-detecção abriu outra 6-12s depois e a carga apareceu em duas
    linhas na aba Carregamentos. Relé aberto de verdade corta a potência
    junto; switch=0 com potência alta é ruído, não desligamento.
    """
    return off_streak >= confirm_polls and off_idle_streak >= confirm_polls


# ─── Poll loop ──────────────────────────────────────────────────
POLL_INTERVAL = 5  # seconds — fase1 + lógica de controle
BREAKER_POLL_INTERVAL = 10  # seconds — breaker (só interessa durante carga)


def _reader_loop(key, cfg, interval):
    """Thread de leitura de um device, em cadência própria.

    Cada device tem sua thread: um breaker lento/offline (o UPDATEDPS do DPS 6
    frequentemente fica sem resposta) não atrasa a coleta da fase1. O prazo da
    próxima leitura é absoluto (deadline), então o período real acompanha o
    intervalo configurado em vez de acumular o tempo de leitura.
    """
    dev = None
    err_streak = 0
    next_t = time.time()
    while True:
        try:
            if dev is None:
                dev = connect_device(cfg)
            if key == "fase1":
                state.update(key, read_fase1(dev))
            elif key == "breaker":
                state.update(key, read_breaker(dev))
            err_streak = 0
        except Exception as e:
            # 1º erro e depois ~1x/hora — falha persistente não inunda o log
            err_streak += 1
            if err_streak == 1 or err_streak % 360 == 0:
                print(f"Erro {key} ({err_streak} consecutivos): {e}")
            dev = None
        next_t += interval
        delay = next_t - time.time()
        if delay < 0.1:
            # Leitura demorou mais que o intervalo — reancora sem acumular dívida
            next_t = time.time() + interval
            delay = interval
        time.sleep(delay)


def poll_loop():
    # Threads de leitura: fase1 e breaker em paralelo, cada um no seu ritmo
    for key, cfg in DEVICES.items():
        interval = BREAKER_POLL_INTERVAL if key == "breaker" else POLL_INTERVAL
        threading.Thread(
            target=_reader_loop, args=(key, cfg, interval), daemon=True
        ).start()

    prune_counter = 0
    err_streak = 0
    # Recupera último dia com snapshot (persiste entre restarts)
    _cfg = load_config()
    last_snapshot_day = _cfg.get("last_snapshot_day", "")
    print(
        f"🔄 Polling iniciado (fase1={POLL_INTERVAL}s, breaker={BREAKER_POLL_INTERVAL}s). "
        f"last_snapshot_day={last_snapshot_day or '(nenhum)'}"
    )

    # Previous breaker energy counter (kWh) + timestamp, used to estimate power
    # from the DPS 1 cumulative delta when DPS 6 (V×I) reads 0 during charging.
    prev_br_counter_kwh = None
    prev_br_counter_ts = None

    # Watchdog: momento em que o disjuntor ficou ON sem consumo relevante.
    # Usado para desligar o disjuntor quando não há sessão ativa.
    breaker_idle_since = None

    # Limiar de potência que caracteriza "carro carregando" na auto-detecção.
    cfg_start_power_w = load_config().get("car_charge_start_power_w", 500)

    # Debounce do estado do switch: polls consecutivos com switch=0.
    # Uma leitura isolada de 0 costuma ser ruído de comunicação (ver
    # breaker_off_confirmed), não um desligamento real.
    breaker_off_streak = 0

    # Streak do desligamento REAL: switch=0 E potência abaixo do idle no
    # mesmo poll (ver breaker_off_externally_confirmed). O DPS 16 queima
    # como False em rajada com o relé fechado; sem o critério de potência,
    # o finalize falso partia a sessão em duas linhas no meio da carga.
    breaker_off_idle_streak = 0

    # Backfill inicial: roda 1x por instalação (gate em snapshots_backfilled),
    # corrigindo placeholders antigos (0.001 / cumulativo) por integrais reais
    # de power×tempo.
    _backfill_snapshots_from_readings()

    while True:
        try:
            with state.lock:
                f1 = state.latest.get("fase1", {})
                br = state.latest.get("breaker", {})
            if f1:
                save_reading(f1, br)

            # Debounce: conta polls consecutivos com switch=0. Leitura ON
            # (ou ausência de dados do breaker) zera o contador.
            if br and not br.get("switch", False):
                breaker_off_streak += 1
            else:
                breaker_off_streak = 0

            # ── AUTO-DETECT charging: breaker ON with power or energy changing ──
            if (
                br
                and br.get("switch", False)
                and charging.state == ChargingTracker.STATE_IDLE
                and (
                    br.get("power_w", 0)
                    > cfg_start_power_w
                    or (
                        br.get("energy_wh", 0) > 0
                        and prev_br_counter_kwh is not None
                        and prev_br_counter_kwh > 0
                        and br.get("energy_wh", 0) != prev_br_counter_kwh
                    )
                )
            ):
                cfg_detect = load_config()
                # SOC inicial: usuário pode ter ligado o disjuntor manualmente
                # sem informar o SOC — estima a partir da última sessão.
                soc_start, soc_estimated = estimate_car_soc_start(cfg_detect)
                if soc_estimated:
                    print(
                        f"⚡ SOC inicial estimado em {soc_start:.0f}% "
                        f"(última sessão / config — corrija no dashboard se o "
                        f"carro rodou desde a última carga)"
                    )
                session = create_charge_session(
                    soc_start=soc_start,
                    soc_target=cfg_detect.get("car_target_soc", 80),
                    battery_kwh=cfg_detect.get("car_battery_kwh", 12.9),
                    start_energy_kwh=br.get("energy_kwh", 0),
                    cost_per_kwh=cfg_detect.get("kwh_cost", 0.956),
                )
                charging.start(
                    start_soc=soc_start,
                    target_soc=cfg_detect.get("car_target_soc", 80),
                    battery_kwh=cfg_detect.get("car_battery_kwh", 12.9),
                    start_energy_kwh=br.get("energy_kwh", 0),
                    session_uuid=session["session_uuid"],
                )
                # Sync config so recovery and UI reflect the auto-detected session
                cfg_detect["car_charging"] = True
                cfg_detect["car_current_soc"] = soc_start
                cfg_detect["car_charge_start_kwh"] = br.get("energy_kwh", 0)
                cfg_detect["car_charge_start_time"] = datetime.now().isoformat()
                cfg_detect["car_charge_start_soc"] = soc_start
                save_config(cfg_detect)
                print("⚡ Auto-detected charging: breaker ON with power, starting session")

            # ── Breaker power, with energy-counter fallback ──
            # DPS 6 (V×I) sometimes returns 0 during active charging. When it
            # does, estimate power from the DPS 1 cumulative counter delta so
            # auto-detect and idle detection keep working.
            # NOTE: br_counter_kwh holds the raw DPS 1 value (energy_wh). Per Tuya
            # spec (scale=2) each raw unit = 10 Wh = 0.01 kWh. When the DPS 6 base64
            # decode fails and power_w is 0, we estimate power from the counter delta:
            #   delta_J = delta_raw * 10 Wh/unit * 3600 J/Wh = delta_raw * 36000 J
            #   power_W = delta_J / dt_s
            br_switch_on = bool(br.get("switch")) if br else False
            br_power_w = br.get("power_w", 0) if br else 0
            br_counter_raw = br.get("energy_wh", 0) if br else 0
            if (
                br_power_w <= 0
                and br_counter_raw > 0
                and prev_br_counter_kwh is not None
                and prev_br_counter_kwh > 0
            ):
                dt_s = (datetime.now() - prev_br_counter_ts).total_seconds()
                delta_raw = br_counter_raw - prev_br_counter_kwh
                if 0 < dt_s < 120 and delta_raw > 0:
                    # 1 raw unit = 10 Wh → delta_J = delta_raw * 10 * 3600
                    br_power_w = delta_raw * 36_000.0 / dt_s
            if br_counter_raw > 0:
                prev_br_counter_kwh = br_counter_raw
                prev_br_counter_ts = datetime.now()

            cfg = load_config()
            cfg_start_power_w = cfg.get("car_charge_start_power_w", 500)

            # Streak de "switch=0 com potência idle": um desligamento real
            # abre o relé E corta o consumo juntos. Ruído do DPS 16 (switch=0
            # com o carro puxando 2.7kW) falha no critério de potência e não
            # conta — é o que impede o finalize falso no meio da carga.
            if (
                br
                and not br_switch_on
                and br_power_w <= cfg.get("car_charge_idle_power_w", 15)
            ):
                breaker_off_idle_streak += 1
            else:
                breaker_off_idle_streak = 0

            # ── Charging tracker update + auto-stop check ──
            if charging.state in (
                ChargingTracker.STATE_CHARGING,
                ChargingTracker.STATE_COMPLETING,
            ):
                if breaker_off_externally_confirmed(
                    breaker_off_streak, breaker_off_idle_streak
                ) and not charging.is_probing():
                    # Breaker was turned OFF externally (physical switch / fault)
                    # while a session was active → finalize it now instead of
                    # leaving a ghost "active" session in the DB. Exige DOIS
                    # critérios persistentes: switch=0 E potência idle em polls
                    # consecutivos — leitura de switch=0 com potência fluindo é
                    # ruído de comunicação do DPS 16, não um desligamento real
                    # (incidentes de 2026-09-18 e 2026-09-20). Durante a sonda
                    # de retomada o relé está aberto POR CONTA DO PRÓPRIO
                    # serviço — is_probing() impede o finalize aqui.
                    print(
                        "🔌 Breaker desligado externamente durante sessão — finalizando"
                    )
                    if charging.session_uuid:
                        finalize_charge_session(
                            charging.session_uuid,
                            end_energy_kwh=br.get("energy_kwh", 0),
                            soc_end=charging.effective_soc,
                            end_reason="manual",
                            effective_end_time=charging.effective_end_time,
                        )
                        # finalize gravou car_current_soc no config; recarregar
                        # para o save_config abaixo não reverter a escrita com
                        # o dict velho do cache de 2s (lost update).
                        cfg = load_config()
                    charging.stop(reason="manual")
                    cfg["car_charging"] = False
                    cfg["car_charge_start_time"] = None
                    save_config(cfg)
                else:
                    # Sessão viva: alimenta o tracker com relé ON (inclusive
                    # na fase 'observe' da sonda) e roda a decisão de
                    # auto-stop em TODO poll — inclusive com o relé aberto
                    # pela própria sonda (fase 'open', switch=0), senão a
                    # sonda nunca chega ao fim.
                    if br is not None and br_switch_on:
                        # Session active and breaker ON — feed latest readings.
                        charging.update(
                            current_energy_kwh=br.get("energy_kwh", 0),
                            current_power_w=br_power_w,
                            idle_power_w=cfg.get("car_charge_idle_power_w", 15),
                            idle_seconds_needed=cfg.get(
                                "car_charge_idle_seconds_to_stop", 300
                            ),
                            efficiency=cfg.get("car_charge_efficiency", 0.80),
                        )
                        # Persist progress to DB (every ~10s)
                        if charging.session_uuid and charging.start_time:
                            # Effective duration excludes the idle confirmation tail
                            eff_end = charging.effective_end_time
                            elapsed = (
                                eff_end - charging.start_time
                            ).total_seconds()
                            update_charge_session_progress(
                                charging.session_uuid,
                                current_energy_kwh=br.get("energy_kwh", 0),
                                current_soc=charging.effective_soc,
                                duration_seconds=int(max(0, elapsed)),
                                avg_power_w=charging.session_avg_power_w,
                            )

                    # ── Fim de carga: concluir ou sondar retomada? ──
                    # Incidente de 2026-09-22 (sessão 126): o carro pausou aos
                    # 62% e a janela de idle leu "carga completa" — o serviço
                    # abriu o relé e a carga morreu com alvo em 100%. Agora a
                    # energia injetada decide: perto do necessário → conclui;
                    # muito abaixo → sonda (abre 120s, religa, observa).
                    # Roda com relé ON OU com sonda em curso (fase 'open' tem
                    # switch=0); sem breaker algum, decidir às cegas só
                    # inflamaria reconnect a cada poll.
                    if (br is not None and br_switch_on) or charging.is_probing():
                        if cfg.get("car_charge_auto_stop", True):
                            action = charging.auto_stop_action(
                                power_w=br_power_w,
                                idle_power_w=cfg.get("car_charge_idle_power_w", 15),
                                idle_seconds_needed=cfg.get(
                                    "car_charge_idle_seconds_to_stop", 300
                                ),
                                resume_power_w=cfg_start_power_w,
                            )
                            if action == "conclude":
                                print(
                                    "🔌 Auto-stopping breaker (charge complete, idle confirmed)"
                                )
                                try:
                                    d_brk = connect_device(DEVICES["breaker"])
                                    d_brk.set_value(BREAKER_SWITCH_DPS, False)
                                    time.sleep(1)
                                    state.update("breaker", read_breaker(d_brk))
                                    # Finalize DB session
                                    if charging.session_uuid:
                                        with state.lock:
                                            br_end = state.latest.get("breaker", {})
                                        end_energy = (
                                            br_end.get("energy_kwh", 0) if br_end else 0
                                        )
                                        finalize_charge_session(
                                            charging.session_uuid,
                                            end_energy_kwh=end_energy,
                                            soc_end=charging.effective_soc,
                                            end_reason="auto",
                                            effective_end_time=charging.effective_end_time,
                                            charge_complete_confident=True,
                                        )
                                        # finalize gravou car_current_soc (e talvez
                                        # a eficiência aprendida) no config;
                                        # recarregar para o save_config abaixo não
                                        # reverter a escrita com o dict velho do
                                        # cache de 2s.
                                        cfg = load_config()
                                    charging.stop(reason="auto")
                                    cfg["car_charging"] = False
                                    save_config(cfg)
                                except Exception as e:
                                    print(f"Auto-stop error: {e}")
                            elif action == "probe_open":
                                print(
                                    "🔌 Sonda de retomada: carga incompleta + pausa — "
                                    "abrindo relé por 120s"
                                )
                                try:
                                    d_brk = connect_device(DEVICES["breaker"])
                                    d_brk.set_value(BREAKER_SWITCH_DPS, False)
                                    time.sleep(1)
                                    state.update("breaker", read_breaker(d_brk))
                                except Exception as e:
                                    print(f"Probe breaker-off error: {e}")
                            elif action == "probe_close":
                                print(
                                    "🔌 Sonda de retomada: religando o relé — "
                                    "aguardando o carro"
                                )
                                try:
                                    d_brk = connect_device(DEVICES["breaker"])
                                    d_brk.set_value(BREAKER_SWITCH_DPS, True)
                                    time.sleep(1)
                                    state.update("breaker", read_breaker(d_brk))
                                    charging.mark_probe_closed()
                                except Exception as e:
                                    print(f"Probe breaker-on error: {e}")
                            elif action == "probe_conclude":
                                print(
                                    "🔌 Sonda de retomada: carro não voltou a "
                                    "carregar — desligando e concluindo a sessão"
                                )
                                try:
                                    d_brk = connect_device(DEVICES["breaker"])
                                    d_brk.set_value(BREAKER_SWITCH_DPS, False)
                                    time.sleep(1)
                                    state.update("breaker", read_breaker(d_brk))
                                    if charging.session_uuid:
                                        with state.lock:
                                            br_end = state.latest.get("breaker", {})
                                        end_energy = (
                                            br_end.get("energy_kwh", 0) if br_end else 0
                                        )
                                        # SEM charge_complete_confident: a sonda
                                        # só prova que o carro não retomou — não
                                        # que ele encheu. soc_end fica na
                                        # estimativa energética.
                                        finalize_charge_session(
                                            charging.session_uuid,
                                            end_energy_kwh=end_energy,
                                            soc_end=charging.effective_soc,
                                            end_reason="auto",
                                            effective_end_time=charging.effective_end_time,
                                        )
                                        # finalize gravou car_current_soc no
                                        # config; recarregar para o save_config
                                        # abaixo não reverter a escrita.
                                        cfg = load_config()
                                    charging.stop(reason="auto")
                                    cfg["car_charging"] = False
                                    save_config(cfg)
                                except Exception as e:
                                    print(f"Probe conclude error: {e}")
                            # 'resumed' e None: nada a fazer neste poll
                # else: sem dados do breaker, ou switch=0 ainda não confirmado
                # (possível ruído de comunicação) → pula este ciclo e reavalia
                # no próximo poll.

            # ── Breaker idle watchdog ──
            # Desliga o disjuntor quando ele está ON sem consumo relevante e
            # não há sessão de carregamento ativa (ex.: sessão parada
            # manualmente mas o disjuntor ficou ligado, ou o carro terminou
            # de carregar sem sessão rastreada).
            if br and br_switch_on and cfg.get("car_charge_auto_stop", True):
                session_active = charging.state in (
                    ChargingTracker.STATE_CHARGING,
                    ChargingTracker.STATE_COMPLETING,
                )
                if not session_active:
                    idle_w = cfg.get("car_charge_idle_power_w", 15)
                    idle_s = cfg.get("car_charge_idle_seconds_to_stop", 300)
                    if br_power_w <= idle_w:
                        if breaker_idle_since is None:
                            breaker_idle_since = datetime.now()
                    else:
                        breaker_idle_since = None
                    if breaker_idle_watchdog_should_stop(
                        breaker_on=True,
                        session_active=False,
                        power_w=br_power_w,
                        idle_power_w=idle_w,
                        idle_since=breaker_idle_since,
                        idle_seconds_needed=idle_s,
                    ):
                        elapsed_idle = (
                            datetime.now() - breaker_idle_since
                        ).total_seconds()
                        print(
                            f"🔌 Breaker idle watchdog: {int(elapsed_idle)}s "
                            f"sem consumo — desligando disjuntor"
                        )
                        try:
                            d_brk = connect_device(DEVICES["breaker"])
                            d_brk.set_value(BREAKER_SWITCH_DPS, False)
                            time.sleep(1)
                            state.update("breaker", read_breaker(d_brk))
                        except Exception as e:
                            print(f"Watchdog breaker-off error: {e}")
                        breaker_idle_since = None
                else:
                    breaker_idle_since = None
            else:
                breaker_idle_since = None

            # Daily snapshot: detecta virada de dia e fecha o dia que acabou
            today = datetime.now().strftime("%Y-%m-%d")
            if last_snapshot_day and today != last_snapshot_day:
                # O dia virou — fecha o dia que estava em curso
                closing_day = last_snapshot_day
                conn = get_db()
                snap_ok = False
                try:
                    f1_rows = conn.execute(
                        "SELECT timestamp, power FROM readings "
                        "WHERE device='fase1' AND DATE(timestamp)=? AND power IS NOT NULL "
                        "ORDER BY timestamp",
                        (closing_day,),
                    ).fetchall()
                    br_power_rows = conn.execute(
                        "SELECT timestamp, phase_c FROM readings "
                        "WHERE device='fase1' AND DATE(timestamp)=? AND phase_c IS NOT NULL "
                        "ORDER BY timestamp",
                        (closing_day,),
                    ).fetchall()
                    f1_kwh = (
                        round(_kwh_from_power_integral(f1_rows), 4) if f1_rows else 0
                    )
                    br_kwh = (
                        round(_kwh_from_power_integral(br_power_rows), 4)
                        if br_power_rows
                        else 0
                    )
                    f1_avg_w = (
                        round(sum(p for _, p in f1_rows) / len(f1_rows), 1)
                        if f1_rows
                        else None
                    )
                    if f1_kwh > 0:
                        conn.execute(
                            """INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, avg_power_w, created_at)
                               VALUES (?, 'fase1', ?, ?, ?)
                               ON CONFLICT(snapshot_date, device) DO UPDATE SET
                                 energy_kwh = excluded.energy_kwh,
                                 avg_power_w = excluded.avg_power_w""",
                            (closing_day, f1_kwh, f1_avg_w, datetime.now().isoformat()),
                        )
                    if br_kwh > 0:
                        conn.execute(
                            """INSERT INTO daily_snapshots (snapshot_date, device, energy_kwh, created_at)
                               VALUES (?, 'breaker', ?, ?)
                               ON CONFLICT(snapshot_date, device) DO UPDATE SET energy_kwh = excluded.energy_kwh""",
                            (closing_day, br_kwh, datetime.now().isoformat()),
                        )
                    conn.commit()
                    snap_ok = True
                    print(
                        f"📸 Snapshot fechado para {closing_day}: fase1={f1_kwh}kWh breaker={br_kwh}kWh"
                    )
                except Exception as e:
                    print(f"⚠️ Erro ao fechar snapshot de {closing_day}: {e}")
                finally:
                    conn.close()
                # Persiste o novo "último dia fechado" na config SOMENTE se o
                # snapshot foi gravado; senão mantém o marcador para retry na
                # próxima iteração (evita perder o dia em caso de lock/erro).
                if snap_ok:
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
            # 1º erro e depois ~1x/hora (360 ciclos × 5s) — falha persistente
            # (ex.: disco cheio) não inunda o service.log.
            err_streak += 1
            if err_streak == 1 or err_streak % 360 == 0:
                print(f"Poll error ({err_streak} consecutivos): {e}")
        else:
            err_streak = 0
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
def api_daily_history(
    days: int = 30, year: int = None, month: int = None
):
    days = max(1, min(days, 365))
    key = ("daily_history", days, year, month)
    cached = _ttl_get(key, 45)
    if cached is not None:
        return {"days": cached}
    result = db_daily_history(days=days, year=year, month=month)
    _ttl_put(key, result)
    return {"days": result}


def db_monthly_stats(year: int, month: int):
    """Return monthly aggregated stats: total kWh, cost, daily breakdown.

    Snapshots-first: per-day kWh comes from daily_snapshots (written at each
    day rollover); only days without a snapshot (today / gaps) get integrated
    via SQL. A full re-integration took ~3.4 s on the CubieBoard; this is
    ~10 ms of lookups plus one single-day integral.

    total_cost = (fase1 + breaker) × tarifa — circuitos separados; o kWh
    total continua só da casa (fase1), com o carro exposto à parte.
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
        by_day = dict(
            conn.execute(
                "SELECT snapshot_date, energy_kwh FROM daily_snapshots "
                "WHERE device='fase1' AND snapshot_date>=? AND snapshot_date<?",
                (first, last),
            ).fetchall()
        )

        daily = []
        today_str = datetime.now().strftime("%Y-%m-%d")
        day = datetime.fromisoformat(first).date()
        last_d = datetime.fromisoformat(last).date()
        while day < last_d:
            ds = day.strftime("%Y-%m-%d")
            if ds == today_str:
                # Dia em curso: snapshot parcial engana o total — integra ao vivo
                nd = (day + timedelta(days=1)).strftime("%Y-%m-%d")
                kwh = _sql_kwh(conn, "power", ds, nd)
            elif ds in by_day and by_day[ds] is not None:
                kwh = round(by_day[ds], 4)
            else:
                nd = (day + timedelta(days=1)).strftime("%Y-%m-%d")
                has_readings = conn.execute(
                    "SELECT 1 FROM readings WHERE device='fase1' AND timestamp>=? AND timestamp<? LIMIT 1",
                    (ds, nd),
                ).fetchone()
                if not has_readings:
                    day += timedelta(days=1)
                    continue  # dia sem nenhum dado (futuro/mês incompleto)
                kwh = _sql_kwh(conn, "power", ds, nd)
            daily.append({"day": ds, "kwh": kwh, "cost": round(kwh * cost, 2)})
            day += timedelta(days=1)

        total_kwh = round(sum(d["kwh"] for d in daily), 4)
        # Custo total cobre os DOIS circuitos: casa (fase1) + carro (breaker).
        breaker_kwh = _kwh_snapshots_plus_missing_days(
            conn, first, last, device="breaker", col="phase_c", min_avg_w=1.0,
        )

        return {
            "year": year,
            "month": month,
            "total_kwh": total_kwh,
            "breaker_kwh": round(breaker_kwh, 4),
            "total_cost": round((total_kwh + breaker_kwh) * cost, 2),
            "daily": daily,
            "source": "local",
        }
    finally:
        conn.close()


@app.get("/api/monthly")
def api_monthly(year: int, month: int):
    key = ("monthly", year, month)
    cached = _ttl_get(key, 45)
    if cached is not None:
        return cached
    result = db_monthly_stats(year, month)
    _ttl_put(key, result)
    return result


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
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    key = ("hourly", date)
    cached = _ttl_get(key, 45)
    if cached is not None:
        return cached
    result = db_hourly(date)
    _ttl_put(key, result)
    return result


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
    cfg["car_current_soc_ts"] = datetime.now().isoformat()
    save_config(cfg)
    # Mid-session correction: rebase the tracker (effective_soc follows the
    # informed value immediately) and the session row so the report is truthful.
    rebased = charging.apply_soc_correction(soc)
    if rebased is not None and charging.session_uuid:
        update_session_soc_start(charging.session_uuid, rebased)
    return {
        "success": True,
        "car_current_soc": cfg["car_current_soc"],
        "applied_to_session": rebased is not None,
    }


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
        # Use breaker energy counter for session tracking (scaled to kWh)
        start_energy = br.get("energy_kwh", 0) or 0
        # SOC inicial: se o usuário não informou após a última carga, estima
        # a partir do soc_end dela; input explícito recente tem precedência.
        soc_start, _soc_estimated = estimate_car_soc_start(cfg)
        session = create_charge_session(
            soc_start=soc_start,
            soc_target=cfg.get("car_target_soc", 80),
            battery_kwh=cfg.get("car_battery_kwh", 12.9),
            start_energy_kwh=start_energy,
            cost_per_kwh=cost_per_kwh,
        )
        charging.start(
            start_soc=soc_start,
            target_soc=cfg.get("car_target_soc", 80),
            battery_kwh=cfg.get("car_battery_kwh", 12.9),
            start_energy_kwh=start_energy,
            session_uuid=session["session_uuid"],
        )
        cfg["car_charging"] = True
        cfg["car_current_soc"] = soc_start
        cfg["car_charge_start_kwh"] = br.get("energy_kwh", 0)
        cfg["car_charge_start_time"] = datetime.now().isoformat()
        cfg["car_charge_start_soc"] = soc_start
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
            end_energy = br_end.get("energy_kwh", 0) if br_end else 0
            result = finalize_charge_session(
                charging.session_uuid,
                end_energy_kwh=end_energy,
                soc_end=charging.effective_soc,
                end_reason="manual",
                effective_end_time=charging.effective_end_time,
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
    st = charging.get_status()
    if not st.get("charging"):
        # Estimativa de partida da próxima carga — aprendida do soc_start
        # reconciliado da última sessão completa (ou do último input do
        # usuário). O frontend mostra como dica ao lado do campo editável;
        # is_estimate=False significa input explícito (o campo já o exibe).
        est, is_estimate = estimate_car_soc_start(load_config())
        st["next_start_soc"] = est
        st["next_start_soc_is_estimate"] = is_estimate
    return st


@app.get("/api/charge/sessions")
def api_charge_sessions(limit: int = 50, include_active: bool = False,
                        days: int = None, since: str = None):
    """List recent charge sessions."""
    if days is not None:
        # Clamp como api_daily_history (days negativo cortaria no futuro).
        # 9999 = todo o histórico ("Tudo" no frontend).
        days = max(1, min(days, 9999))
    if since is not None:
        try:
            datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Parâmetro 'since' inválido (ISO esperado)."
            )
    return {
        "sessions": list_charge_sessions(limit=limit, include_active=include_active,
                                         days=days, since=since),
        "active": get_active_charge_session(),
    }


@app.get("/api/charge/summary")
def api_charge_summary(days: int = 90, since: str = None):
    """Summary of charge sessions over the period."""
    # Clamp como api_daily_history; 9999 = todo o histórico ("Tudo").
    days = max(1, min(days, 9999))
    if since is not None:
        try:
            datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Parâmetro 'since' inválido (ISO esperado)."
            )
    return charge_sessions_summary(days=days, since=since)


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
        "car_charge_efficiency",
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

    # Recover active charge session from DB (survives service restarts).
    # Always check the DB — don't rely solely on the config flag, which can
    # be stale after crashes or external breaker toggles.
    backfill_charge_sessions_from_readings()

    _cfg = load_config()
    _conn = get_db()
    try:
        _active_rows = _conn.execute(
            "SELECT id, session_uuid, start_time, soc_start, soc_target, battery_kwh,"
            " start_energy_kwh, cost_per_kwh, soc_end FROM charge_sessions"
            " WHERE status = 'active' ORDER BY id DESC"
        ).fetchall()
    finally:
        _conn.close()

    # Bug guard: if there are multiple actives, finalize all but the latest
    if len(_active_rows) > 1:
        print(
            f"⚠️ Found {len(_active_rows)} active charge sessions on startup"
            f" — finalizing all but the latest"
        )
        finalize_stale_active_sessions(reason="manual", keep_uuid=_active_rows[0][1])

    _row = _active_rows[0] if _active_rows else None

    if _row:
        _id, _uuid, _start_ts, _soc_start, _soc_target, _bat, _start_e, _cost, _soc_end = _row
        # Last known SOC — used only for the log line and to sync
        # car_current_soc (point of departure for the NEXT session).
        _recover_soc = _soc_end if _soc_end and _soc_end > _soc_start else _soc_start
        # O tracker recalcula o SOC efetivo do zero a cada poll
        # (soc_start + delta de energia), então reiniciar com o frame
        # original da sessão reproduz exatamente o SOC pré-restart.
        # Semeá-lo com _recover_soc + o contador ORIGINAL somaria a energia
        # pré-restart duas vezes.
        charging.start(
            start_soc=_soc_start,
            target_soc=_soc_target,
            battery_kwh=_bat,
            start_energy_kwh=_start_e,
            session_uuid=_uuid,
        )
        charging.start_time = datetime.fromisoformat(_start_ts)
        elapsed_min = (datetime.now() - charging.start_time).total_seconds() / 60
        print(
            f"🔄 Sessão recuperada do DB: {_recover_soc:.1f}% → {_soc_target}%"
            f" (decorrido: {elapsed_min:.0f} min)"
        )
        # Sync config so it reflects the real state
        _cfg["car_charging"] = True
        _cfg["car_charge_start_time"] = _start_ts
        _cfg["car_charge_start_kwh"] = _start_e
        _cfg["car_current_soc"] = int(_recover_soc)
        save_config(_cfg)
    elif _cfg.get("car_charging"):
        # Config says charging but no active DB session — start fresh
        _start_kwh = 0
        try:
            _d = connect_device(DEVICES["breaker"])
            _br = read_breaker(_d)
            _start_kwh = _br.get("energy_kwh", 0)
        except Exception:
            pass
        charging.start(
            start_soc=_cfg.get("car_current_soc", 50),
            target_soc=_cfg.get("car_target_soc", 80),
            battery_kwh=_cfg.get("car_battery_kwh", 12.9),
            start_energy_kwh=_start_kwh if _start_kwh else 0,
        )
        if _cfg.get("car_charge_start_time"):
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
