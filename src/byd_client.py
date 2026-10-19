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
import calendar
import json
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
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

# ── Fase 2: diário de bordo ──
TRIP_MIN_KM = 0.2           # abaixo disso é ruído de GPS/odômetro
TRIP_END_STILL_CYCLES = 2   # ciclos parado para fechar a viagem
HOME_RADIUS_M_DEFAULT = 300 # raio da geocerca (GPS doméstico é ruidoso)
HOME_GRID_DEG = 0.003       # ~330 m: célula do cluster "onde o carro dorme"
HOME_MIN_POINTS = 10        # pontos mínimos na célula para chamar de casa
HOME_REFRESH_DAYS = 7       # reaprende onde o carro dorme a cada 7 dias
HOME_LOOKBACK_DAYS = 7
ENERGY_POLL_MAX_AGE = 20 * 3600  # gráfico BYD 7 dias: 1x ao dia
BRT_OFFSET_S = -3 * 3600    # Brasil sem horário de verão (placa sem tzdata)
FUEL_EVENT_DELTA_PP = 8.0   # salto de nível (%) que caracteriza abastecimento

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS byd_trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_ts INTEGER, end_ts INTEGER,        -- epoch UTC
                start_odo REAL, end_odo REAL,            -- km (delta = km rodados)
                start_lat REAL, start_lon REAL,
                end_lat REAL, end_lon REAL,
                soc_start REAL, soc_end REAL,
                depart_home INTEGER, arrive_home INTEGER -- geocerca (NULL sem casa)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_byd_trip_start ON byd_trips(start_ts)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS byd_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS byd_fuel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER,                  -- epoch UTC da detecção
                level_before REAL, level_after REAL, delta_pp REAL,
                price_per_l REAL             -- R$/L CONGELADO no momento da detecção
            )
        """)
        # Migration: tabela criada antes do preço por evento
        cur_fuel_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(byd_fuel_events)").fetchall()
        }
        if cur_fuel_cols and "price_per_l" not in cur_fuel_cols:
            conn.execute("ALTER TABLE byd_fuel_events ADD COLUMN price_per_l REAL")
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


# ── Fase 2: meta, geocerca e viagens ──────────────────────────

def _meta_get(conn, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM byd_meta WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def _meta_set(conn, key: str, value):
    conn.execute(
        "INSERT OR REPLACE INTO byd_meta (key, value) VALUES (?,?)",
        (key, str(value)),
    )


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = radians(lat1), radians(lat2)
    a = (
        sin((p2 - p1) / 2) ** 2
        + cos(p1) * cos(p2) * sin(radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * r * asin(sqrt(a))


def _model_json(m):
    """Modelo pydantic → dict JSON puro (get_charging_homepage vem em tupla)."""
    if isinstance(m, (tuple, list)):
        return [_model_json(x) for x in m]
    if hasattr(m, "model_dump"):
        return m.model_dump(mode="json", exclude_none=True)
    return m


def _brt_day_bounds(date_str: str) -> tuple[int, int]:
    """'YYYY-MM-DD' local (BRT) → (epoch início, epoch fim) em UTC."""
    y, m, d = (int(x) for x in date_str.split("-"))
    start = calendar.timegm((y, m, d, 0, 0, 0)) - BRT_OFFSET_S
    return start, start + 86400


def _brt_day(ts: float) -> str:
    return datetime.fromtimestamp(ts + BRT_OFFSET_S, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )


def _home_radius(conn) -> float:
    raw = _meta_get(conn, "home_radius_m")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(HOME_RADIUS_M_DEFAULT)


def home() -> dict | None:
    """Local aprendido onde o carro dorme (byd_meta). None até haver dados."""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        lat = _meta_get(conn, "home_lat")
        lon = _meta_get(conn, "home_lon")
        if lat is None or lon is None:
            conn.close()
            return None
        out = {
            "lat": float(lat),
            "lon": float(lon),
            "radius_m": _home_radius(conn),
        }
        upd = _meta_get(conn, "home_updated_ts")
        out["updated_ts"] = int(float(upd)) if upd else None
        conn.close()
        return out
    except (sqlite3.OperationalError, TypeError, ValueError):
        return None


def _maybe_learn_home():
    """Reaprende onde o carro dorme: célula modal (~330 m) das posições dos
    últimos 7 dias — o carro passa a maior parte parado em casa, então a
    célula com mais pontos é a casa. No máx. 1x por HOME_REFRESH_DAYS."""
    now = time.time()
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    try:
        upd = _meta_get(conn, "home_updated_ts")
        if upd and now - float(upd) < HOME_REFRESH_DAYS * 86400:
            return
        rows = conn.execute(
            "SELECT lat, lon FROM byd_positions "
            "WHERE ts > ? AND lat IS NOT NULL AND lon IS NOT NULL",
            (now - HOME_LOOKBACK_DAYS * 86400,),
        ).fetchall()
        if len(rows) < HOME_MIN_POINTS:
            return
        cells: dict = {}
        for lat, lon in rows:
            cells.setdefault((round(lat, 3), round(lon, 3)), []).append((lat, lon))
        best = max(cells.values(), key=len)
        if len(best) < HOME_MIN_POINTS:
            return
        lats = sorted(p[0] for p in best)
        lons = sorted(p[1] for p in best)
        mid = len(best) // 2
        _meta_set(conn, "home_lat", round(lats[mid], 6))
        _meta_set(conn, "home_lon", round(lons[mid], 6))
        _meta_set(conn, "home_updated_ts", int(now))
        conn.commit()
        print(
            f"BYD: casa aprendida em {round(lats[mid], 5)}, {round(lons[mid], 5)} "
            f"({len(best)} posições nos últimos {HOME_LOOKBACK_DAYS} dias)"
        )
    finally:
        conn.close()


# estado da viagem em construção (thread do poller só)
_trip: dict | None = None
_trip_still = 0


def _trip_distance_home(point, conn):
    if not point or point.get("lat") is None:
        return None
    lat, lon = _meta_get(conn, "home_lat"), _meta_get(conn, "home_lon")
    if lat is None or lon is None:
        return None
    return haversine_m(point["lat"], point["lon"], float(lat), float(lon))


def _update_trip(snap: dict, pos: dict | None, moving: bool):
    """Máquina de viagens: abre em movimento, fecha após 2 ciclos parado.
    km vem do Δodômetro (confiável); GPS só contextualiza início/fim."""
    global _trip, _trip_still
    odo = snap.get("odo_km")
    if odo is None:
        return
    if moving:
        _trip_still = 0
        point = {"lat": pos["lat"], "lon": pos["lon"]} if pos else None
        if _trip is None:
            _trip = {
                "start_ts": snap["ts"], "start_odo": odo,
                "soc_start": snap.get("soc"),
                "start_point": point, "end_point": point,
                "end_ts": snap["ts"], "end_odo": odo,
                "soc_end": snap.get("soc"),
            }
        else:
            _trip.update(
                end_ts=snap["ts"], end_odo=odo, soc_end=snap.get("soc")
            )
            if point:
                _trip["end_point"] = point
        return
    if _trip is None:
        return
    _trip_still += 1
    if _trip_still < TRIP_END_STILL_CYCLES:
        return
    trip, _trip, _trip_still = _trip, None, 0
    km = (trip["end_odo"] or 0) - (trip["start_odo"] or 0)
    if km < TRIP_MIN_KM:
        return
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    try:
        r = _home_radius(conn)
        d_start = _trip_distance_home(trip["start_point"], conn)
        d_end = _trip_distance_home(trip["end_point"], conn)
        depart = None if d_start is None else int(d_start <= r)
        arrive = None if d_end is None else int(d_end <= r)
        conn.execute(
            """INSERT INTO byd_trips
               (start_ts, end_ts, start_odo, end_odo,
                start_lat, start_lon, end_lat, end_lon,
                soc_start, soc_end, depart_home, arrive_home)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(trip["start_ts"]), int(trip["end_ts"]),
                trip["start_odo"], trip["end_odo"],
                (trip["start_point"] or {}).get("lat"),
                (trip["start_point"] or {}).get("lon"),
                (trip["end_point"] or {}).get("lat"),
                (trip["end_point"] or {}).get("lon"),
                trip["soc_start"], trip["soc_end"], depart, arrive,
            ),
        )
        _meta_set(conn, "last_trip_end_ts", int(trip["end_ts"]))
        conn.commit()
        print(
            f"BYD: viagem {km:.1f} km "
            f"({trip['soc_start'] and round(trip['soc_start'])}% → "
            f"{trip['soc_end'] and round(trip['soc_end'])}%) "
            f"{'· saiu de casa' if depart else ''}{'· chegou em casa' if arrive else ''}"
        )
    finally:
        conn.close()


async def _maybe_poll_energy(client, vin: str):
    """Gráfico de consumo dos últimos 7 dias direto da BYD — 1x ao dia.
    (A assinatura da lib não aceita powerType: a perna do gráfico é a que a
    BYD mandar — combustível no DM-i; o consumo EV é calculado por ΔSOC.)"""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    try:
        last = _meta_get(conn, "energy_last_ts")
        if last and time.time() - float(last) < ENERGY_POLL_MAX_AGE:
            return
        en = _model_json(await client.get_energy_consumption(vin))
        _meta_set(conn, "energy_graph", json.dumps(en, ensure_ascii=False))
        _meta_set(conn, "energy_last_ts", int(time.time()))
        conn.commit()
        print("BYD: gráfico de consumo 7 dias atualizado")
    finally:
        conn.close()


def _current_fuel_price():
    """Preço padrão do litro (car_fuel_price do tuya_config.json) para congelar
    em novos abastecimentos — leitura tolerante: config é domínio do dashboard,
    mas o carimbo precisa acontecer na DETECÇÃO, não na exibição."""
    try:
        cfg = json.loads(
            (BASE_DIR / "data" / "tuya_config.json").read_text(encoding="utf-8")
        )
        v = cfg.get("car_fuel_price")
        return float(v) if v is not None else None
    except (OSError, ValueError, TypeError):
        return None


def _update_fuel(snap: dict, moving: bool):
    """Detecta abastecimento: salto de ≥ FUEL_EVENT_DELTA_PP no nível do
    tanque (oil_percent) entre leituras com o carro parado. A baseline
    (byd_meta.fuel_level) segue o consumo normal; em movimento não atualiza —
    nível balança e o abastecimento é confirmado no próximo ciclo parado.
    Litros ficam em Δpp — convertidos com car_tank_l na hora de exibir, para
    o dono corrigir a capacidade sem perder histórico."""
    level = snap.get("oil_percent")
    if level is None:
        return
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    try:
        raw = _meta_get(conn, "fuel_level")
        if moving:
            return
        prev = float(raw) if raw is not None else None
        if prev is None:
            _meta_set(conn, "fuel_level", level)
            conn.commit()
            return
        delta = level - prev
        if delta >= FUEL_EVENT_DELTA_PP:
            conn.execute(
                "INSERT INTO byd_fuel_events (ts, level_before, level_after, delta_pp, price_per_l)"
                " VALUES (?,?,?,?,?)",
                (int(snap["ts"]), prev, level, round(delta, 1), _current_fuel_price()),
            )
            print(
                f"BYD: ⛽ abastecimento detectado: {prev:.0f}% → {level:.0f}% "
                f"(+{delta:.0f} p.p.)"
            )
        _meta_set(conn, "fuel_level", level)
        conn.commit()
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
    home_pos = home()
    st["home"] = home_pos
    g = st["gps"]
    if home_pos and g.get("lat") is not None and g.get("lon") is not None:
        d = haversine_m(g["lat"], g["lon"], home_pos["lat"], home_pos["lon"])
        st["at_home"] = d <= home_pos["radius_m"]
        st["home_dist_m"] = int(d)
    else:
        st["at_home"] = None
        st["home_dist_m"] = None
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
            "SELECT ts, soc, range_ev, odo_km, charge_state, is_charging, oil_percent "
            "FROM byd_snapshots WHERE ts >= ? ORDER BY ts",
            (since,),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "ts": r[0], "soc": r[1], "range_ev": r[2], "odo_km": r[3],
            "charge_state": r[4], "is_charging": r[5], "oil_percent": r[6],
        }
        for r in rows
    ]


def positions_for_day(date_str: str) -> list[dict]:
    """Posições de um dia local (BRT), para o mapa do diário de bordo."""
    start, end = _brt_day_bounds(date_str)
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        rows = conn.execute(
            "SELECT ts, gps_ts, lat, lon, speed, heading, odo_km "
            "FROM byd_positions WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start, end),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "ts": r[0], "gps_ts": r[1], "lat": r[2], "lon": r[3],
            "speed": r[4], "heading": r[5], "odo_km": r[6],
        }
        for r in rows
    ]


def trips_for_day(date_str: str) -> list[dict]:
    start, end = _brt_day_bounds(date_str)
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        rows = conn.execute(
            "SELECT start_ts, end_ts, start_odo, end_odo, start_lat, start_lon, "
            "end_lat, end_lon, soc_start, soc_end, depart_home, arrive_home "
            "FROM byd_trips WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts",
            (start, end),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        out.append(
            {
                "start_ts": r[0], "end_ts": r[1],
                "km": round((r[3] or 0) - (r[2] or 0), 1),
                "start_lat": r[4], "start_lon": r[5],
                "end_lat": r[6], "end_lon": r[7],
                "soc_start": r[8], "soc_end": r[9],
                "depart_home": r[10], "arrive_home": r[11],
            }
        )
    return out


def daily_trips(days: int = 14) -> dict:
    """Por dia local (BRT): km rodados, kWh EV estimado (ΔSOC × bateria) e
    contagem de viagens — derivados de byd_trips."""
    days = max(1, min(int(days), 90))
    since = time.time() - days * 86400
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        rows = conn.execute(
            "SELECT start_ts, end_ts, start_odo, end_odo, soc_start, soc_end "
            "FROM byd_trips WHERE start_ts >= ? ORDER BY start_ts",
            (since,),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return {}
    battery_kwh = 12.9  # mesmo default do dashboard (car_battery_kwh)
    out: dict = {}
    for start_ts, end_ts, o1, o2, s1, s2 in rows:
        day = _brt_day(start_ts)
        d = out.setdefault(day, {"km": 0.0, "kwh_ev": 0.0, "trips": 0, "seconds": 0})
        d["km"] += (o2 or 0) - (o1 or 0)
        d["trips"] += 1
        if end_ts and start_ts:
            d["seconds"] += max(0, int(end_ts - start_ts))
        if s1 is not None and s2 is not None and s2 < s1:
            d["kwh_ev"] += (s1 - s2) / 100.0 * battery_kwh
    for d in out.values():
        d["km"] = round(d["km"], 1)
        d["kwh_ev"] = round(d["kwh_ev"], 2)
        d["kwh_per_100km"] = (
            round(d["kwh_ev"] / d["km"] * 100, 1) if d["km"] >= 2 else None
        )
        d["hours"] = round(d["seconds"] / 3600, 2)
    return out


def last_trip_after(epoch: float | None) -> bool:
    """True se houve viagem concluída após o epoch dado (prova física de
    que o carro foi dirigido — base para a confirmação de partida da η)."""
    if epoch is None:
        return False
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        row = conn.execute(
            "SELECT MAX(end_ts) FROM byd_trips"
        ).fetchone()
        conn.close()
    except sqlite3.OperationalError:
        return False
    return bool(row and row[0] and row[0] > epoch)


def monthly_trips(months: int = 3) -> dict:
    """Agregação mensal (BRT) das viagens: km, kWh EV estimado, contagem."""
    months = max(1, min(int(months), 24))
    since = time.time() - months * 31 * 86400
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        rows = conn.execute(
            "SELECT start_ts, end_ts, start_odo, end_odo, soc_start, soc_end "
            "FROM byd_trips WHERE start_ts >= ? ORDER BY start_ts",
            (since,),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return {}
    battery_kwh = 12.9
    out: dict = {}
    for start_ts, end_ts, o1, o2, s1, s2 in rows:
        month = _brt_day(start_ts)[:7]
        d = out.setdefault(
            month, {"km": 0.0, "kwh_ev": 0.0, "trips": 0, "seconds": 0}
        )
        d["km"] += (o2 or 0) - (o1 or 0)
        d["trips"] += 1
        if end_ts and start_ts:
            d["seconds"] += max(0, int(end_ts - start_ts))
        if s1 is not None and s2 is not None and s2 < s1:
            d["kwh_ev"] += (s1 - s2) / 100.0 * battery_kwh
    for d in out.values():
        d["km"] = round(d["km"], 1)
        d["kwh_ev"] = round(d["kwh_ev"], 2)
        d["kwh_per_100km"] = (
            round(d["kwh_ev"] / d["km"] * 100, 1) if d["km"] >= 2 else None
        )
        d["hours"] = round(d["seconds"] / 3600, 2)
    return out


def fuel_events(days: int = 90) -> list[dict]:
    """Abastecimentos detectados (Δpp de tanque + preço congelado de cada um);
    litros calculados na exibição com car_tank_l do config."""
    since = time.time() - max(1, min(int(days), 365)) * 86400
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        rows = conn.execute(
            "SELECT id, ts, level_before, level_after, delta_pp, price_per_l "
            "FROM byd_fuel_events WHERE ts >= ? ORDER BY ts DESC",
            (since,),
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return []
    return [
        {
            "id": r[0], "ts": r[1], "level_before": r[2], "level_after": r[3],
            "delta_pp": r[4], "price": r[5],
        }
        for r in rows
    ]


def set_fuel_price(event_id: int, price: float) -> bool:
    """Corrige o preço de UM abastecimento (o valor oscila; cada evento guarda
    o seu). False se o evento não existir (ex.: tabela ainda não criada)."""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        cur = conn.execute(
            "UPDATE byd_fuel_events SET price_per_l = ? WHERE id = ?",
            (float(price), int(event_id)),
        )
        conn.commit()
        ok = cur.rowcount > 0
        conn.close()
        return ok
    except (sqlite3.OperationalError, TypeError, ValueError):
        return False


def energy_graph() -> dict | None:
    """Último gráfico de consumo 7 dias despejado da BYD (bruto)."""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        raw = _meta_get(conn, "energy_graph")
        last = _meta_get(conn, "energy_last_ts")
        conn.close()
    except sqlite3.OperationalError:
        return None
    if not raw:
        return None
    try:
        out = json.loads(raw)
        out["fetched_ts"] = int(float(last)) if last else None
        return out
    except (TypeError, ValueError):
        return None


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
                    # diário de bordo: viagens, casa, gráfico BYD e combustível
                    try:
                        _update_trip(snap, pos, moving)
                        _maybe_learn_home()
                        await _maybe_poll_energy(client, vin)
                        _update_fuel(snap, moving)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        if err_count == 1 or err_count % ERR_LOG_EVERY == 0:
                            print(f"BYD: erro no diário de bordo: {e}")
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
