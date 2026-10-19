#!/usr/bin/env python3
"""Poller BYD — telemetria real do carro via pybyd (docs/ESTUDO_ABA_BYD.md).

Thread daemon própria com event loop asyncio próprio, no molde das threads
de leitura Tuya do dashboard. Nunca derruba o serviço: qualquer falha vira
estado + backoff exponencial. Os endpoints do dashboard leem SOMENTE o cache
(status()/history()/fresh_soc()) — nenhuma chamada síncrona à nuvem BYD no
caminho do request.

Cadência adaptativa (cada realtime acorda o T-Box do carro):
    em movimento → 60 s  |  carregando → 60 s  |  parado → 15 min (±2 min)
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "data" / "byd.json"
DB_FILE = BASE_DIR / "data" / "tuya_history.db"

POLL_MOVING = 60      # carro em movimento (base do diário de bordo, fase 2)
POLL_CHARGING = 60    # carregando
POLL_IDLE = 900       # parado: 15 min
POLL_JITTER = 120     # ±2 min só no idle (evita padrão mecânico)
GPS_EVERY_MOVING = 2  # gps a cada N ciclos em movimento
GPS_EVERY_STILL = 4
BACKOFF_MIN = 30
BACKOFF_MAX = 1800
ERR_LOG_EVERY = 30    # não inundar o service.log (mesmo molde do err_streak)
SOC_FRESH_MAX_AGE = 1800   # âncora: dado do carro com até 30 min
POLL_FRESH_MAX_AGE = 900   # âncora: coleta nossa com até 15 min
PRUNE_SNAPSHOT_DAYS = 45
PRUNE_POSITION_DAYS = 120

# Chaves do byd.json que viram campos de BydConfig (o modelo rejeita extras).
CFG_KEYS = (
    "username", "password", "base_url", "country_code", "language",
    "time_zone", "control_pin", "identifier_type",
)

_lock = threading.Lock()
_st: dict = {
    "enabled": False,        # byd.json existe e está habilitado
    "configured": False,     # credenciais preenchidas
    "running": False,        # thread viva e logada
    "vin": None,
    "model": None,
    "last_error": None,      # último erro (string) — some quando loga OK
    "last_ok_ts": None,      # epoch da última coleta bem-sucedida
    "soc": None,             # atalho: SOC da última coleta
    "snapshot": {},          # última leitura realtime (campos reduzidos)
    "gps": {},               # última posição
}


def _load_cfg() -> dict | None:
    """data/byd.json como dict, ou None se ausente/desabilitado/placeholder."""
    if not CONFIG_FILE.exists():
        return None
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"BYD: config ilegível ({e})")
        return None
    if not cfg.get("enabled", True):
        return None
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""
    if not user or not pwd or "exemplo" in user or "senha-do-app" in pwd:
        return None
    return cfg


def _dt_epoch(dt) -> float | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return float(dt)


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def init_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS byd_snapshots (
                ts INTEGER PRIMARY KEY,      -- epoch UTC da coleta
                data_ts INTEGER,             -- epoch UTC do timestamp do carro
                soc REAL, range_ev REAL, range_total REAL,
                range_oil REAL, oil_percent REAL,
                odo_km REAL, speed REAL,
                charge_state INTEGER, is_charging INTEGER, is_connected INTEGER,
                time_to_full_min INTEGER,
                temp_in_car REAL, locked INTEGER,
                doors_open INTEGER, windows_open INTEGER, online INTEGER,
                cons_ev_recent REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS byd_positions (
                ts INTEGER PRIMARY KEY,      -- epoch UTC da coleta
                gps_ts INTEGER,              -- epoch UTC do fix de GPS
                lat REAL, lon REAL, speed REAL, heading REAL, odo_km REAL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_byd_pos_ts ON byd_positions(ts)"
        )
        conn.commit()
    finally:
        conn.close()


def _save_cycle(snap: dict, pos: dict | None):
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        # INTEGER PRIMARY KEY exige inteiro: time.time() é float
        ts = int(snap["ts"])
        data_ts = int(snap["data_ts"]) if snap.get("data_ts") else None
        conn.execute(
            """INSERT OR REPLACE INTO byd_snapshots
               (ts, data_ts, soc, range_ev, range_total, range_oil, oil_percent,
                odo_km, speed, charge_state, is_charging, is_connected,
                time_to_full_min, temp_in_car, locked, doors_open, windows_open,
                online, cons_ev_recent)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ts, data_ts, snap.get("soc"),
                snap.get("range_ev"), snap.get("range_total"),
                snap.get("range_oil"), snap.get("oil_percent"),
                snap.get("odo_km"), snap.get("speed"),
                snap.get("charge_state"),
                1 if snap.get("is_charging") else 0,
                1 if snap.get("is_connected") else 0,
                snap.get("time_to_full_min"), snap.get("temp_in_car"),
                None if snap.get("locked") is None else int(bool(snap["locked"])),
                None if snap.get("doors_open") is None else int(bool(snap["doors_open"])),
                None if snap.get("windows_open") is None else int(bool(snap["windows_open"])),
                None if snap.get("online") is None else int(bool(snap["online"])),
                snap.get("cons_ev_recent"),
            ),
        )
        if pos and pos.get("lat") is not None and pos.get("lon") is not None:
            conn.execute(
                """INSERT OR REPLACE INTO byd_positions
                   (ts, gps_ts, lat, lon, speed, heading, odo_km)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    int(pos["ts"]),
                    int(pos["gps_ts"]) if pos.get("gps_ts") else None,
                    pos["lat"], pos["lon"],
                    pos.get("speed"), pos.get("heading"), pos.get("odo_km"),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _prune():
    now = time.time()
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    try:
        n1 = conn.execute(
            "DELETE FROM byd_snapshots WHERE ts < ?",
            (now - PRUNE_SNAPSHOT_DAYS * 86400,),
        ).rowcount
        n2 = conn.execute(
            "DELETE FROM byd_positions WHERE ts < ?",
            (now - PRUNE_POSITION_DAYS * 86400,),
        ).rowcount
        conn.commit()
        if n1 or n2:
            print(f"BYD prune: {n1} snapshots, {n2} posições antigas removidas")
    finally:
        conn.close()


def status() -> dict:
    """Cache thread-safe para os endpoints — nunca toca a rede."""
    with _lock:
        st = {**_st, "snapshot": {**_st["snapshot"]}, "gps": {**_st["gps"]}}
    now = time.time()
    snap = st["snapshot"]
    data_ts = snap.get("data_ts")
    # clamp >= 0: o relógio da placa pode ficar alguns segundos atrás do da BYD
    st["data_age_s"] = max(0, int(now - data_ts)) if data_ts else None
    st["poll_age_s"] = (
        max(0, int(now - st["last_ok_ts"])) if st.get("last_ok_ts") else None
    )
    if snap.get("soc") is None:
        st["soc_fresh"] = False
    else:
        st["soc_fresh"] = (
            st["poll_age_s"] is not None
            and st["poll_age_s"] < POLL_FRESH_MAX_AGE
            and st["data_age_s"] is not None
            and st["data_age_s"] < SOC_FRESH_MAX_AGE
        )
    return st


def fresh_soc() -> float | None:
    """SOC REAL recente, ou None. Usado como âncora do modelo de SOC —
    a regra da η (partida confirmada pelo dono) continua intacta."""
    with _lock:
        snap = {**_st["snapshot"]}
        last_ok = _st["last_ok_ts"]
    soc = snap.get("soc")
    if soc is None or last_ok is None:
        return None
    now = time.time()
    if now - last_ok > POLL_FRESH_MAX_AGE:
        return None
    data_ts = snap.get("data_ts")
    if data_ts and now - data_ts > SOC_FRESH_MAX_AGE:
        return None
    return _f(soc)


def history(hours: int = 24) -> list[dict]:
    """Série de snapshots para gráficos. [] se as tabelas ainda não existem."""
    since = time.time() - max(1, int(hours)) * 3600
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        rows = conn.execute(
            "SELECT ts, soc, range_ev, odo_km, charge_state, is_charging "
            "FROM byd_snapshots WHERE ts >= ? ORDER BY ts",
            (since,),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "ts": r[0], "soc": r[1], "range_ev": r[2], "odo_km": r[3],
            "charge_state": r[4], "is_charging": r[5],
        }
        for r in rows
    ]


def _build_byd_config(cfg: dict):
    from pybyd.config import BydConfig

    kwargs = {k: cfg[k] for k in CFG_KEYS if cfg.get(k) is not None}
    kwargs["mqtt_enabled"] = bool(cfg.get("mqtt_enabled", False))
    return BydConfig(**kwargs)


async def _collect(client, vin: str, gps_i: int, moving_prev: bool):
    """Um ciclo de coleta. Retorna (snapshot, pos|None, moving)."""
    rt = await client.get_vehicle_realtime(vin)
    now = time.time()
    snap = {
        "ts": now,
        "data_ts": _dt_epoch(getattr(rt, "timestamp", None)),
        "soc": _f(getattr(rt, "elec_percent", None)),
        "range_total": _f(getattr(rt, "endurance_mileage", None)),
        "range_ev": _f(getattr(rt, "ev_endurance", None)),
        "range_oil": _f(getattr(rt, "oil_endurance", None)),
        "oil_percent": _f(getattr(rt, "oil_percent", None)),
        "odo_km": _f(getattr(rt, "total_mileage", None)),
        "speed": _f(getattr(rt, "speed", None)),
        "charge_state": _i(getattr(rt, "charge_state", None)),
        "is_charging": bool(getattr(rt, "is_charging", False)),
        "is_connected": bool(getattr(rt, "is_charger_connected", False)),
        "time_to_full_min": _i(getattr(rt, "time_to_full_minutes", None)),
        "temp_in_car": _f(getattr(rt, "temp_in_car", None)),
        "locked": getattr(rt, "is_locked", None),
        "doors_open": getattr(rt, "is_any_door_open", None),
        "windows_open": getattr(rt, "is_any_window_open", None),
        "online": bool(getattr(rt, "is_online", False)),
        "cons_ev_recent": _f(getattr(rt, "nearest_energy_consumption", None)),
        # pneus ficam só no cache (mudam devagar; não viram série no DB)
        "tires": [
            _f(getattr(rt, "left_front_tire_pressure", None)),
            _f(getattr(rt, "right_front_tire_pressure", None)),
            _f(getattr(rt, "left_rear_tire_pressure", None)),
            _f(getattr(rt, "right_rear_tire_pressure", None)),
        ],
        "tire_unit": {1: "bar", 2: "psi", 3: "kPa"}.get(
            _i(getattr(rt, "tire_press_unit", None)) or 2, "psi"
        ),
    }
    moving = (snap["speed"] or 0.0) > 1.5
    every = GPS_EVERY_MOVING if moving else GPS_EVERY_STILL
    pos = None
    if gps_i % every == 0 or (moving != moving_prev):
        gps = await client.get_gps_info(vin)
        pos = {
            "ts": now,
            "gps_ts": _dt_epoch(getattr(gps, "gps_timestamp", None)),
            "lat": _f(getattr(gps, "latitude", None)),
            "lon": _f(getattr(gps, "longitude", None)),
            "speed": _f(getattr(gps, "speed", None)),
            "heading": _f(getattr(gps, "direction", None)),
            "odo_km": snap["odo_km"],
        }
    return snap, pos, moving


def _next_interval(snap: dict, moving: bool) -> float:
    if moving:
        return POLL_MOVING
    if snap.get("is_charging"):
        return POLL_CHARGING
    return POLL_IDLE + random.uniform(-POLL_JITTER, POLL_JITTER)


async def _run_loop():
    cfg = _load_cfg()
    backoff_k = 0
    err_count = 0
    while True:
        try:
            # imports de dentro do loop: pybyd ausente ou config inválida vira
            # estado + backoff (visível na aba), não morte silenciosa da thread
            from pybyd import BydClient

            byd_cfg = _build_byd_config(cfg)
            async with BydClient(byd_cfg) as client:
                await client.login()
                vehicles = await client.get_vehicles()
                vin = cfg.get("vin") or (
                    getattr(vehicles[0], "vin", None) if vehicles else None
                )
                if not vin:
                    raise RuntimeError("nenhum veículo na conta")
                model = None
                for v in vehicles:
                    if getattr(v, "vin", None) == vin:
                        model = getattr(v, "model_name", None) or getattr(
                            v, "modelName", None
                        )
                with _lock:
                    _st.update(running=True, vin=vin, model=model, last_error=None)
                if backoff_k:
                    print(f"BYD: reconectado após {backoff_k} tentativa(s)")
                backoff_k, err_count = 0, 0

                gps_i = 0
                moving_prev = False
                while True:
                    t0 = time.monotonic()
                    try:
                        snap, pos, moving = await _collect(
                            client, vin, gps_i, moving_prev
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        err_count += 1
                        if err_count == 1 or err_count % ERR_LOG_EVERY == 0:
                            print(f"BYD: erro de coleta ({err_count}x): {e}")
                        with _lock:
                            _st["last_error"] = str(e)
                        await asyncio.sleep(POLL_CHARGING)
                        continue
                    save_err = None
                    try:
                        _save_cycle(snap, pos)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # problema de DB não derruba a sessão BYD: o cache
                        # segue vivo e a gravação tenta de novo no próximo ciclo
                        err_count += 1
                        save_err = f"db: {e}"
                        if err_count == 1 or err_count % ERR_LOG_EVERY == 0:
                            print(f"BYD: erro de gravação ({err_count}x): {e}")
                    gps_i += 1
                    with _lock:
                        _st.update(
                            soc=snap["soc"], snapshot=snap,
                            gps=pos or _st["gps"],
                            last_ok_ts=snap["ts"], last_error=save_err,
                            last_attempt_ts=snap["ts"],
                        )
                    if moving != moving_prev:
                        print(f"BYD: carro {'em movimento' if moving else 'parado'}")
                    moving_prev = moving
                    await asyncio.sleep(
                        max(5, _next_interval(snap, moving) - (time.monotonic() - t0))
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            backoff_k += 1
            wait = min(BACKOFF_MAX, BACKOFF_MIN * (2 ** (backoff_k - 1)))
            name = type(e).__name__
            if "Auth" in name:
                wait = BACKOFF_MAX  # credenciais/endpoint errados: sem martelar
            with _lock:
                _st.update(running=False, last_error=f"{name}: {e}")
            print(f"BYD: {name} — retry em {int(wait)}s ({e})")
            await asyncio.sleep(wait)


def _thread_main():
    try:
        asyncio.run(_run_loop())
    except Exception as e:
        with _lock:
            _st.update(running=False, last_error=f"{type(e).__name__}: {e}")
        print(f"BYD: poller parou: {type(e).__name__}: {e}")


def start():
    """Sobe o poller (idempotente). Não lança: falha vira estado, não exceção."""
    cfg = _load_cfg()
    with _lock:
        _st["configured"] = cfg is not None
        _st["enabled"] = cfg is not None
    if cfg is None:
        print("BYD: desabilitado (data/byd.json ausente/placeholder/desligado)")
        return
    try:
        init_db()
    except Exception as e:
        print(f"BYD: init_db falhou ({e}) — poller não sobe")
        return
    threading.Thread(target=_thread_main, daemon=True, name="byd-poller").start()
    print(f"BYD: poller iniciado (idle={POLL_IDLE}s, carga/movimento={POLL_CHARGING}s)")
