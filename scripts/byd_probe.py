#!/usr/bin/env python3
"""Sonda BYD (Fase 0): valida conta/regiao/endpoint e despeja telemetria real.

Script standalone - nao toca no dashboard. Plano completo em docs/ESTUDO_ABA_BYD.md.

Uso:
    python3 scripts/byd_probe.py                  # dump completo, uma vez
    python3 scripts/byd_probe.py --raw            # dump + salva JSON bruto em logs/
    python3 scripts/byd_probe.py --loop 5         # 5 leituras espacadas (teste de movimento/GPS)
    python3 scripts/byd_probe.py --region eu      # testa o endpoint europeu default do pybyd
    python3 scripts/byd_probe.py --vin LSJW...    # escolhe o veiculo pelo VIN

Config: data/byd.json (copie de src/byd.example.json e preencha usuario/senha).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_FILE = BASE_DIR / "data" / "byd.json"
LOGS_DIR = BASE_DIR / "logs"

PLACEHOLDERS = {"seu-email@exemplo.com", "senha-do-app-my-byd"}

# Presets de regiao (docs/ESTUDO_ABA_BYD.md §4). O BR e inferido do catalogo de
# regioes - confirmar se ele autentica e justamente o objetivo desta sonda.
REGIONS = {
    "br": {
        "base_url": "https://dilinkappoversea-br.byd.auto",
        "country_code": "BR",
        "language": "pt",
        "time_zone": "America/Sao_Paulo",
    },
    "eu": {
        "base_url": "https://dilinkappoversea-eu.byd.auto",
        "country_code": "NL",
        "language": "en",
        "time_zone": "Europe/Amsterdam",
    },
    "mx": {
        "base_url": "https://dilinkappoversea-mx.byd.auto",
        "country_code": "MX",
        "language": "es",
        "time_zone": "America/Mexico_City",
    },
}

# Chaves do byd.json que viram campos de BydConfig (o modelo rejeita extras).
CFG_KEYS = (
    "username",
    "password",
    "base_url",
    "country_code",
    "language",
    "time_zone",
    "control_pin",
    "identifier_type",
)


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"[X] Config nao encontrada: {CONFIG_FILE}")
        print(f"    Copie o template e preencha:")
        print(f"      cp src/byd.example.json data/byd.json")
        sys.exit(2)
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if cfg.get("username") in PLACEHOLDERS or cfg.get("password") in PLACEHOLDERS:
        print(f"[X] Credenciais nao preenchidas em {CONFIG_FILE}")
        print("    Preencha username/password (os mesmos do app MY BYD).")
        sys.exit(2)
    if not cfg.get("username") or not cfg.get("password"):
        print(f"[X] username/password obrigatorios em {CONFIG_FILE}")
        sys.exit(2)
    return cfg


def build_config(cfg: dict, region: str | None):
    try:
        from pybyd.config import BydConfig
    except ImportError as e:
        print(f"[X] pybyd nao instalado: {e}")
        print("    Na placa (armv7 - aiohttp/cryptography vem do apk):")
        print("      apk add py3-aiohttp py3-cryptography")
        print("      pip install 'paho-mqtt>=2.1,<3'")
        print("      pip install --no-deps pybyd==0.0.79")
        sys.exit(2)

    if region:
        if region not in REGIONS:
            print(f"[X] Regiao desconhecida: {region} (opcoes: {', '.join(REGIONS)})")
            sys.exit(2)
        cfg = {**cfg, **REGIONS[region]}
        print(f"[i] Overrides de regiao ({region}): {REGIONS[region]['base_url']}")

    kwargs = {k: cfg[k] for k in CFG_KEYS if cfg.get(k) is not None}
    kwargs["mqtt_enabled"] = bool(cfg.get("mqtt_enabled", False))
    return BydConfig(**kwargs)


def fnum(v, suf: str = "") -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.1f}".rstrip("0").rstrip(".") + suf
    except (TypeError, ValueError):
        return str(v)


def enum_name(v):
    if v is None:
        return "—"
    return getattr(v, "name", v)


def age_str(dt) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    if secs < 90:
        return f"{secs}s"
    if secs < 7200:
        return f"{secs // 60}min"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}"


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def model_json(m):
    if isinstance(m, (tuple, list)):
        return [model_json(x) for x in m]
    if hasattr(m, "model_dump"):
        return m.model_dump(mode="json", exclude_none=True)
    return m


async def dump_full(client, vin: str, raw: bool) -> None:
    rt = await client.get_vehicle_realtime(vin)
    gps = await client.get_gps_info(vin)

    print("\n=== REALTIME ===")
    print(f"online:            {enum_name(rt.online_state)}   (dado de {age_str(rt.timestamp)} atras)")
    print(f"SOC:               {fnum(rt.elec_percent, '%')}")
    print(f"autonomia EV:      {fnum(rt.endurance_mileage, ' km')}")
    print(f"odometro:          {fnum(rt.total_mileage, ' km')}")
    print(f"velocidade:        {fnum(rt.speed, ' km/h')}")
    print(f"carga:             estado={enum_name(rt.effective_charging_state)} carregando={rt.is_charging} conectado={rt.is_charger_connected}")
    print(f"tempo ate cheia:   {rt.time_to_full_minutes} min" if rt.time_to_full_minutes is not None else "tempo ate cheia:   —")
    print(f"temp interna:      {fnum(rt.temp_in_car, ' °C')}")
    print(f"travado:           {rt.is_locked}   portas/janelas abertas: {rt.is_any_door_open}/{rt.is_any_window_open}")
    unit = {1: "bar", 2: "psi", 3: "kpa"}.get(
        int(rt.tire_press_unit) if rt.tire_press_unit is not None else 1, "bar"
    )
    print(
        f"pneus ({unit}):       {fnum(rt.left_front_tire_pressure)} / {fnum(rt.right_front_tire_pressure)}"
        f" / {fnum(rt.left_rear_tire_pressure)} / {fnum(rt.right_rear_tire_pressure)}"
    )

    print("\n=== GPS ===")
    print(f"posicao:           {gps.latitude}, {gps.longitude}")
    if gps.latitude is not None and gps.longitude is not None:
        print(f"                   https://maps.google.com/?q={gps.latitude},{gps.longitude}")
    print(f"velocidade/rumbo:  {fnum(gps.speed, ' km/h')} / {fnum(gps.direction, '°')}")
    print(f"fix de gps:        {age_str(gps.gps_timestamp)} atras")

    try:
        chg = await client.get_charging_homepage(vin)
        print("\n=== CHARGING HOMEPAGE ===")
        print(json.dumps(model_json(chg), ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"\n=== CHARGING HOMEPAGE === (falhou: {e})")

    try:
        en = await client.get_energy_consumption(vin)
        print("\n=== CONSUMO ===")
        if en.self_graph is not None:
            print(f"7 dias ({en.self_graph.energy_consumption_unit}): {en.self_graph.energy_consumption}")
        if en.cumulative_energy_consumption is not None:
            c = en.cumulative_energy_consumption
            print(
                f"vida util: {fnum(c.avg_ev_consumption, ' kWh/100km')} em {fnum(c.total_mileage, ' km')}"
            )
        if en.nearest_energy_consumption is not None:
            n = en.nearest_energy_consumption
            print(
                f"ultimos 50 km: {fnum(n.avg_ev_consumption, ' kWh/100km')}"
                f" ({fnum(n.ev_consumption, ' kWh')} usados)"
            )
    except Exception as e:
        print(f"\n=== CONSUMO === (falhou: {e})")

    if raw:
        LOGS_DIR.mkdir(exist_ok=True)
        out = LOGS_DIR / f"byd_dump_{datetime.now():%Y%m%d_%H%M%S}.json"
        payload = {"vin": vin}
        for name, model in (("realtime", rt), ("gps", gps)):
            payload[name] = model_json(model)
        try:
            payload["charging_homepage"] = model_json(await client.get_charging_homepage(vin))
        except Exception as e:
            payload["charging_homepage"] = {"erro": str(e)}
        try:
            payload["energy"] = model_json(await client.get_energy_consumption(vin))
        except Exception as e:
            payload["energy"] = {"erro": str(e)}
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[i] Dump bruto salvo em {out}")


async def loop_once(client, vin: str, prev: dict) -> dict:
    rt = await client.get_vehicle_realtime(vin)
    gps = await client.get_gps_info(vin)
    d_pos = d_odo = None
    if prev.get("lat") is not None and gps.latitude is not None:
        d_pos = haversine_m(prev["lat"], prev["lon"], gps.latitude, gps.longitude)
    if prev.get("odo") is not None and rt.total_mileage is not None:
        d_odo = rt.total_mileage - prev["odo"]
    now = datetime.now()
    print(
        f"{now:%H:%M:%S} | SOC {fnum(rt.elec_percent, '%'):>6}"
        f" | aut {fnum(rt.endurance_mileage, 'km'):>7}"
        f" | odo {fnum(rt.total_mileage, 'km'):>9} (+{fnum(d_odo, 'km')})"
        f" | rt {fnum(rt.speed, 'km/h'):>7}"
        f" | GPS {gps.latitude},{gps.longitude} {fnum(gps.speed, 'km/h')} (fix {age_str(gps.gps_timestamp)})"
        + (f" | Δpos {d_pos:.0f} m" if d_pos is not None else "")
    )
    return {"lat": gps.latitude, "lon": gps.longitude, "odo": rt.total_mileage}


async def main(cfg: dict, args) -> None:
    from pybyd import BydClient

    print(
        f"[i] Endpoint: {cfg['base_url']}  country={cfg.get('country_code')}"
        f"  lang={cfg.get('language')}  mqtt={bool(cfg.get('mqtt_enabled', False))}"
    )
    try:
        async with BydClient(build_config(cfg, args.region)) as client:
            try:
                await client.login()
            except Exception as e:
                name = type(e).__name__
                print(f"\n[X] Login falhou ({name}): {e}")
                if "Auth" in name:
                    print("    Checklist:")
                    print("      1. usuario/senha sao os do APP MY BYD (nao do site)?")
                    print("      2. endpoint certo para a conta? tente: --region eu  (ou mx)")
                    print("      3. country_code/language batem com o cadastro do app?")
                code = getattr(e, "code", None)
                if code:
                    print(f"    code={code} endpoint={getattr(e, 'endpoint', '?')}")
                sys.exit(3)
            print("[OK] Login aceito pela BYD.")

            vehicles = await client.get_vehicles()
            if not vehicles:
                print("[X] Nenhum veiculo na conta.")
                sys.exit(3)
            print(f"[i] {len(vehicles)} veiculo(s) na conta:")
            for v in vehicles:
                model = getattr(v, "model_name", None) or getattr(v, "modelName", None) or "?"
                print(f"      VIN {getattr(v, 'vin', '?')}  modelo {model}")

            vin = args.vin or getattr(vehicles[0], "vin", None)
            if args.vin and all(getattr(v, "vin", None) != args.vin for v in vehicles):
                print(f"[X] VIN {args.vin} nao esta na conta.")
                sys.exit(3)

            if args.loop:
                prev: dict = {}
                for i in range(args.loop):
                    if i:
                        print(f"--- aguardando {args.interval}s ---")
                        await asyncio.sleep(args.interval)
                    prev = await loop_once(client, vin, prev)
                print(f"\n[i] {args.loop} leituras concluidas.")
            else:
                await dump_full(client, vin, raw=args.raw)
    except KeyboardInterrupt:
        print("\n[i] Interrompido.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sonda BYD (Fase 0) - docs/ESTUDO_ABA_BYD.md")
    ap.add_argument("--raw", action="store_true", help="salva JSON bruto do dump em logs/")
    ap.add_argument("--loop", type=int, metavar="N", help="repete realtime+gps N vezes (teste de movimento)")
    ap.add_argument("--interval", type=int, default=60, help="intervalo do --loop em segundos (default 60)")
    ap.add_argument("--region", choices=sorted(REGIONS), help="sobrepoe base_url/country/idioma do byd.json")
    ap.add_argument("--vin", help="VIN especifico (default: primeiro da conta)")
    args = ap.parse_args()

    if sys.version_info < (3, 11):
        print(f"[X] Python {sys.version.split()[0]} - pybyd exige >= 3.11")
        sys.exit(2)

    asyncio.run(main(load_config(), args))
