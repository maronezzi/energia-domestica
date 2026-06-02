#!/usr/bin/env python3
"""
Tuya Monitor — Escaneia rede local e extrai dados de medidores Tuya
Dependências: pip install tinytuya pycryptodome --break-system-packages
Uso:
    python3 tuya_monitor.py --scan
    python3 tuya_monitor.py --monitor --id DEVICE_ID --ip IP --key LOCAL_KEY
    python3 tuya_monitor.py --menu
"""

import json
import time
import os
import argparse
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("tuya-monitor")

try:
    import tinytuya
except ImportError:
    log.error("Instale tinytuya: pip install tinytuya pycryptodome --break-system-packages")
    raise SystemExit(1)


CONFIG_FILE = "tuya_devices.json"


# ──────────────────────────────────────────────────────────────
# PASSO 1: ESCANEIA A REDE
# ──────────────────────────────────────────────────────────────

def scan_network(timeout=18):
    """Escaneia a rede em busca de dispositivos Tuya (UDP broadcast)."""
    log.info(f"🔍 Escaneando rede Tuya (UDP 6666/6667/7000, timeout={timeout}s)...")

    # tinytuya.scan() salva em snapshot.json
    # Parâmetros: maxretry, color, forcescan
    # maxretry default é 18s de scan; forcescan só funciona com devices.json existente
    result = tinytuya.scan()
    del result  # não precisa, usamos snapshot.json

    # Lê o snapshot que o tinytuya gera (nova estrutura v1.18+)
    snapshot_file = "snapshot.json"
    try:
        with open(snapshot_file) as f:
            snapshot = json.load(f)
    except FileNotFoundError:
        log.error("Nenhum snapshot gerado. Nenhum dispositivo encontrado?")
        return []

    devices = []
    # Nova estrutura: {'timestamp': ..., 'devices': [...]}
    device_list = snapshot.get("devices", [])

    for info in device_list:
        # ignora tuplas (broadcast/gateway entries)
        if not isinstance(info, dict):
            continue
        d = {
            "ip": info.get("ip", ""),
            "id": info.get("id", ""),
            "product": info.get("productKey", ""),
            "version": info.get("ver", "3.3"),
            "mac": info.get("mac", ""),
            "name": f"Tuya_{info.get('ip', 'unknown').replace('.', '_')}",
        }
        devices.append(d)

    if not devices:
        log.warning("Nenhum dispositivo Tuya encontrado na rede.")
        return []

    log.info(f"✅ {len(devices)} dispositivo(s) encontrado(s)\n")
    for d in devices:
        print(f"  ┌─ {d['name']}")
        print(f"  │  IP:        {d['ip']}")
        print(f"  │  Device ID: {d['id']}")
        print(f"  │  Product:   {d['product']}")
        print(f"  │  Version:   {d['version']}")
        print(f"  └─")

    # Salva pra uso posterior (sem Local Key ainda — ela vem da Cloud)
    save_devices(devices)
    log.info(f"💾 IPs salvos em {CONFIG_FILE}")
    log.info("⚠️  Para ler dados, você precisa da Local Key (obtenha em https://iot.tuya.com)")

    return devices


# ──────────────────────────────────────────────────────────────
# PASSO 2: EXTRAI DADOS DE ENERGIA
# ──────────────────────────────────────────────────────────────

def extract_energy_data(dev_id, dev_ip, dev_key, version="3.3"):
    """Conecta no medidor Tuya via TCP local e lê todos os DPS."""
    log.info(f"📡 Conectando em {dev_ip} (ID: {dev_id})...")

    try:
        # Cria dispositivo Tinytuya
        d = tinytuya.Device(
            id=dev_id,
            address=dev_ip,
            key=dev_key,
            version=float(version),
        )

        # Tenta pegar status
        data = d.status()
        if not data:
            log.warning("Device não respondeu. Tentando DPS 1-20...")
            data = d.status(use_dps="1-20")

        log.info(f"✅ Dados recebidos:\n")
        print(f"  ╔══ DADOS DO MEDIDOR ════════════════╗")

        # DPS comuns de medidor de energia
        dps_labels = {
            1:   ("Liga/Desliga", ""),
            17:  ("Tensão (V)", "V"),
            18:  ("Corrente (A)", "A"),
            19:  ("Potência Instantânea (W)", "W"),
            20:  ("Energia Total (kWh)", "kWh"),
            21:  ("Frequência (Hz)", "Hz"),
            22:  ("Potência Aparente (VA)", "VA"),
            23:  ("Fator de Potência", ""),
            45:  ("Energia Dia Atual (kWh)", "kWh"),
            46:  ("Energia Dia Anterior (kWh)", "kWh"),
            47:  ("Energia Mês Atual (kWh)", "kWh"),
            48:  ("Energia Mês Anterior (kWh)", "kWh"),
            101: ("Potência Ativa (W)", "W"),
            102: ("Energia Reativa (kVarh)", "kVarh"),
            103: ("Potência Reativa (Var)", "Var"),
        }

        dps_data = data.get("dps", data)
        found_any = False
        for dps_num, (label, unit) in dps_labels.items():
            val = dps_data.get(str(dps_num), dps_data.get(dps_num))
            if val is not None:
                # Ajuste de escala: tensão Tuya vem x10
                display_val = val
                if dps_num == 20 and isinstance(val, (int, float)) and val > 1000:
                    display_val = f"{val/10:.1f}"
                    unit = "V (x10→real)"
                print(f"  ║  DPS[{dps_num:>3}] {label:<32} {display_val} {unit}")
                found_any = True

        if not found_any:
            print(f"  ║  DPS brutos: {json.dumps(data, indent=4)}")
            print(f"  ║")
            print(f"  ║  → Verifique os DPS do seu medidor específico")
            print(f"  ║  → Tente: d.status(use_dps='1-50') para medidores de energia")

        print(f"  ╚════════════════════════════════════════╝")
        return data

    except Exception as e:
        log.error(f"Erro ao conectar: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# PASSO 3: MONITORA EM TEMPO REAL
# ──────────────────────────────────────────────────────────────

def monitor_device(dev_id, dev_ip, dev_key, version="3.3", interval=5, csv=False):
    """Loop contínuo lendo dados do medidor."""
    log.info(f"📡 Monitorando {dev_id} @ {dev_ip} (intervalo={interval}s)")

    try:
        d = tinytuya.Device(
            id=dev_id,
            address=dev_ip,
            key=dev_key,
            version=float(version),
        )

        csv_file = None
        if csv:
            csv_file = f"energy_log_{dev_id}_{datetime.now().strftime('%Y%m%d')}.csv"
            if not os.path.exists(csv_file):
                with open(csv_file, "w") as f:
                    f.write("timestamp,voltage_V,current_A,power_W,energy_kWh,frequency_Hz\n")

        print(f"\n{'TIME':<10} {'VOLTAGE':>10} {'CURRENT':>10} {'POWER':>10} {'ENERGY':>10} {'FREQ':>8}")
        print("-" * 65)

        while True:
            try:
                data = d.status()
                dps = data.get("dps", data)

                ts = datetime.now().strftime("%H:%M:%S")
                voltage = dps.get("17", dps.get(17, "?"))
                current = dps.get("18", dps.get(18, "?"))
                power   = dps.get("19", dps.get(19, "?"))
                energy  = dps.get("20", dps.get(20, "?"))
                freq    = dps.get("21", dps.get(21, "?"))

                print(f"{ts:<10} {str(voltage):>10} {str(current):>10} {str(power):>10} {str(energy):>10} {str(freq):>8}")

                if csv_file:
                    with open(csv_file, "a") as f:
                        f.write(f"{datetime.now().isoformat()},{voltage},{current},{power},{energy},{freq}\n")

                time.sleep(interval)

            except KeyboardInterrupt:
                print(f"\n\n🛑 Monitoramento interrompido.")
                if csv_file:
                    log.info(f"📝 Log salvo em {csv_file}")
                break

    except Exception as e:
        log.error(f"Erro no monitoramento: {e}")


# ──────────────────────────────────────────────────────────────
# UTILITÁRIOS
# ──────────────────────────────────────────────────────────────

def save_devices(devices):
    """Salva dispositivos em JSON (sem Local Key — usar Tuya IoT Cloud pra obtê-la)."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(devices, f, indent=2)


def load_devices():
    """Carrega dispositivos salvos do scan."""
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def add_device_manually():
    """Pede IP, Device ID e Local Key ao usuário."""
    print("\n📋 Adicionar dispositivo manualmente:")
    ip      = input("  IP do dispositivo: ").strip()
    dev_id  = input("  Device ID: ").strip()
    local_key = input("  Local Key (da Tuya Cloud): ").strip()
    version  = input("  Versão protocolo (ENTER=3.3): ").strip() or "3.3"
    name     = input("  Nome (opcional): ").strip() or f"Tuya_{ip.replace('.', '_')}"

    d = {
        "ip": ip,
        "id": dev_id,
        "key": local_key,
        "version": version,
        "name": name,
    }

    devices = load_devices()
    devices = [x for x in devices if x.get("id") != dev_id]  # remove duplicado
    devices.append(d)
    save_devices(devices)

    print(f"\n✅ Dispositivo salvo em {CONFIG_FILE}")
    return d


# ──────────────────────────────────────────────────────────────
# MENU INTERATIVO
# ──────────────────────────────────────────────────────────────

def interactive_menu():
    while True:
        print("\n╔══════════════════════════════════════════╗")
        print("║      TTYA MONITOR — MENU                 ║")
        print("╠══════════════════════════════════════════╣")
        print("║  1. Escanear rede local                 ║")
        print("║  2. Listar dispositivos salvos         ║")
        print("║  3. Adicionar dispositivo manualmente   ║")
        print("║  4. Ler dados de energia (1 vez)        ║")
        print("║  5. Monitorar tempo real (loop)        ║")
        print("║  6. Ler todos os dispositivos salvos   ║")
        print("║  0. Sair                                ║")
        print("╚══════════════════════════════════════════╝")

        choice = input("\n> ").strip()

        if choice == "1":
            scan_network()

        elif choice == "2":
            devices = load_devices()
            if not devices:
                print("Nenhum dispositivo salvo. Escaneie primeiro (opção 1).")
            else:
                for i, d in enumerate(devices, 1):
                    has_key = "🔑" if d.get("key") else "⚠️ "
                    print(f"  {i}. {has_key} {d.get('name','?')} | {d.get('ip','?')} | {d.get('id','?')}")

        elif choice == "3":
            add_device_manually()

        elif choice == "4":
            devices = load_devices()
            if not devices:
                print("Nenhum dispositivo salvo. Escaneie primeiro (opção 1).")
                continue
            for i, d in enumerate(devices, 1):
                print(f"  {i}. {d.get('name','?')} | {d.get('ip','?')}")

            if not any(d.get("key") for d in devices):
                print("\n⚠️  Nenhum dispositivo tem Local Key.")
                print("   Obtenha em: https://iot.tuya.com → Cloud → Devices → Query Device Details")
                cont = input("   Continuar mesmo assim? (s/n): ").strip().lower()
                if cont != "s":
                    continue

            sel = input("Escolha o número (Enter=1): ").strip() or "1"
            try:
                d = devices[int(sel) - 1]
                if not d.get("key"):
                    print("⚠️  Sem Local Key — não será possível ler dados.")
                    print("   get local key em: https://iot.tuya.com/cloud/")
                else:
                    extract_energy_data(d["id"], d["ip"], d["key"], d.get("version","3.3"))
            except (ValueError, IndexError):
                print("Seleção inválida.")

        elif choice == "5":
            devices = load_devices()
            if not devices:
                print("Nenhum dispositivo salvo. Escaneie primeiro (opção 1).")
                continue

            # Só mostra os que têm key
            with_key = [d for d in devices if d.get("key")]
            if not with_key:
                print("⚠️  Nenhum dispositivo tem Local Key. Adicione manualmente (opção 3).")
                continue

            for i, d in enumerate(with_key, 1):
                print(f"  {i}. {d.get('name','?')} | {d.get('ip','?')}")

            sel = input("Escolha o número (Enter=1): ").strip() or "1"
            interval = input("Intervalo em segundos (Enter=5): ").strip() or "5"
            csv_opt = input("Salvar CSV? (s/n, Enter=n): ").strip().lower() == "s"

            try:
                d = with_key[int(sel) - 1]
                monitor_device(d["id"], d["ip"], d["key"], d.get("version","3.3"), int(interval), csv=csv_opt)
            except (ValueError, IndexError):
                print("Seleção inválida.")

        elif choice == "6":
            devices = load_devices()
            if not devices:
                print("Nenhum dispositivo salvo.")
                continue
            for d in devices:
                if d.get("key"):
                    extract_energy_data(d["id"], d["ip"], d["key"], d.get("version","3.3"))
                else:
                    print(f"\n⚠️  {d.get('name','?')} ({d.get('ip','?')}) — sem Local Key")
                time.sleep(1)

        elif choice == "0":
            print("Tchau! 👋")
            break


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tuya Monitor — Escaneia e lê medidores Tuya localmente",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  python3 tuya_monitor.py --scan
  python3 tuya_monitor.py --read --id YOUR_DEVICE_ID --ip 192.168.1.100 --key YOUR_LOCAL_KEY
  python3 tuya_monitor.py --monitor --id YOUR_DEVICE_ID --ip 192.168.1.100 --key YOUR_LOCAL_KEY --interval 10 --csv
  python3 tuya_monitor.py --menu
        """
    )
    parser.add_argument("--scan", action="store_true", help="Escanear rede local em busca de dispositivos Tuya")
    parser.add_argument("--read", action="store_true", help="Ler dados uma vez")
    parser.add_argument("--monitor", action="store_true", help="Monitoramento contínuo em tempo real")
    parser.add_argument("--menu", action="store_true", help="Menu interativo")
    parser.add_argument("--id", dest="dev_id", help="Device ID do dispositivo")
    parser.add_argument("--ip", dest="dev_ip", help="IP local do dispositivo")
    parser.add_argument("--key", dest="dev_key", help="Local Key (da Tuya Cloud)")
    parser.add_argument("--version", default="3.3", help="Versão do protocolo (padrão: 3.3)")
    parser.add_argument("--interval", type=int, default=5, help="Intervalo em segundos (padrão: 5)")
    parser.add_argument("--csv", action="store_true", help="Salvar leituras em CSV")
    parser.add_argument("--debug", action="store_true", help="Ativar debug verbose")

    args = parser.parse_args()

    if args.debug:
        tinytuya.set_debug(True)

    if args.scan:
        scan_network()

    elif args.read:
        if not args.dev_id or not args.dev_ip or not args.dev_key:
            print("--read requer --id, --ip e --key")
            raise SystemExit(1)
        extract_energy_data(args.dev_id, args.dev_ip, args.dev_key, args.version)

    elif args.monitor:
        if not args.dev_id or not args.dev_ip or not args.dev_key:
            print("--monitor requer --id, --ip e --key")
            raise SystemExit(1)
        monitor_device(args.dev_id, args.dev_ip, args.dev_key, args.version, args.interval, args.csv)

    elif args.menu or len(vars(args)) == 0:
        interactive_menu()

    else:
        parser.print_help()
