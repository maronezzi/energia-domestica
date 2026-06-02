#!/usr/bin/env python3
"""
Tuya Energy Logger - Lê os dois medidores em tempo real e salva em CSV
Uso: python3 tuya_logger.py

Configure as credenciais em data/devices.json (mesmo formato do dashboard).
"""

import json
import os
import sys
import tinytuya
import csv
import time
from datetime import datetime
from pathlib import Path

# Load credentials from data/devices.json (gitignored)
BASE_DIR = Path(__file__).resolve().parent.parent
DEVICES_FILE = BASE_DIR / "data" / "devices.json"
if DEVICES_FILE.exists():
    with open(DEVICES_FILE) as f:
        cfg = json.load(f)
    DEVICES = [
        {
            "name": cfg.get("fase1", {}).get("name", "fase1"),
            "id": cfg.get("fase1", {}).get("id", ""),
            "ip": cfg.get("fase1", {}).get("ip", ""),
            "key": cfg.get("fase1", {}).get("key", ""),
            "version": cfg.get("fase1", {}).get("version", 3.4),
        },
        {
            "name": cfg.get("breaker", {}).get("name", "breaker"),
            "id": cfg.get("breaker", {}).get("id", ""),
            "ip": cfg.get("breaker", {}).get("ip", ""),
            "key": cfg.get("breaker", {}).get("key", ""),
            "version": cfg.get("breaker", {}).get("version", 3.5),
        },
    ]
else:
    print(f"⚠️  {DEVICES_FILE} not found.")
    print(f"   Copy src/devices.example.json to data/devices.json and fill in credentials.")
    DEVICES = []

CSV_FILE = "tuya_energy_log.csv"
INTERVAL = 5  # segundos

def init_csv():
    exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow([
                "timestamp",
                "device",
                # Medidor Fase 1
                "fase1_voltage_v",
                "fase1_current_a",
                "fase1_power_w",
                "fase1_energy_kwh",
                # Breaker
                "breaker_energy_kwh",
                "breaker_switch",
                "breaker_phase_a",
                "breaker_phase_b",
                "breaker_phase_c",
            ])
    print(f"📝 Log: {CSV_FILE}")

def read_medidor_fase1(d):
    dps = d.status().get("dps", {})
    return {
        "voltage_v": round(dps.get("20", 0) / 10, 1) if dps.get("20", 0) > 100 else dps.get("20", 0),
        "current_a": dps.get("18", 0),
        "power_w": dps.get("19", 0),
        "energy_kwh": dps.get("17", 0),
    }

def read_breaker(d):
    dps = d.status().get("dps", {})
    return {
        "energy_kwh": dps.get("1", 0),
        "switch": dps.get("16", dps.get("11", False)),
        "phase_a": dps.get("101", 0),
        "phase_b": dps.get("102", 0),
        "phase_c": dps.get("103", 0),
    }

def main():
    print("=" * 65)
    print("  TTYA ENERGY LOGGER — Medição em tempo real")
    print("=" * 65)
    print(f"  Intervalo: {INTERVAL}s | Ctrl+C pra parar")
    print()

    # Conecta nos devices
    devs = []
    for cfg in DEVICES:
        print(f"  🔌 Conectando em {cfg['name']} ({cfg['ip']})...")
        try:
            d = tinytuya.Device(
                cfg["id"],
                address=cfg["ip"],
                local_key=cfg["key"],
                version=cfg["version"],
            )
            test = d.status()
            print(f"     ✅ Conectado! DPS: {list(test.get('dps',{}).keys())}")
            devs.append((cfg["name"], d))
        except Exception as e:
            print(f"     ❌ Erro: {e}")
            devs.append((cfg["name"], None))

    if not any(d for _, d in devs):
        print("Nenhum device conectado. Encerrando.")
        return

    init_csv()

    # Cabeçalho
    print()
    print(f"{'TIME':<10} {'FASE1':>18} {'BREAKER':>20}")
    print(f"{'':10} {'V':>6} {'A':>5} {'W':>6} {'kWh':>8} {'kWh':>8} {'SW':>4}")
    print("-" * 65)

    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        row = [datetime.now().isoformat()]

        # Linha pro terminal
        line_parts = []

        for name, d in devs:
            if d is None:
                line_parts.extend(["-"] * 5)
                row.extend(["", "", "", "", ""])
                continue

            try:
                if "Fase 1" in name:
                    m = read_medidor_fase1(d)
                    v = f"{m['voltage_v']:.1f}"
                    a = f"{m['current_a']:.1f}" if m['current_a'] else "0"
                    w = f"{m['power_w']}"
                    k = f"{m['energy_kwh']}"
                    line_parts.extend([v, a, w, k])
                    row.extend([m["voltage_v"], m["current_a"], m["power_w"], m["energy_kwh"]])
                elif "Breaker" in name:
                    m = read_breaker(d)
                    k = f"{m['energy_kwh']}"
                    sw = "ON" if m["switch"] else "OFF"
                    line_parts.extend([k, sw])
                    row.extend([m["energy_kwh"], m["switch"], m["phase_a"], m["phase_b"], m["phase_c"]])
            except Exception as e:
                line_parts.extend(["ERR"] * (5 if "Fase 1" in name else 2))
                row.extend([""] * (4 if "Fase 1" in name else 5))

        print(f"{ts:<10} {' '.join(f'{p:>6}' for p in line_parts)}")

        # Salva CSV
        with open(CSV_FILE, "a", newline="") as f:
            csv.writer(f).writerow(row)

        time.sleep(INTERVAL)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n🛑 Parado. Log salvo em:", CSV_FILE)
