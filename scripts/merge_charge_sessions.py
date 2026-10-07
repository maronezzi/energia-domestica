#!/usr/bin/env python3
"""Funde duas sessões de carregamento que eram uma carga só.

Uma rajada de leituras corrompidas do DPS 16 (switch=False com o relé
fechado — ver breaker_off_externally_confirmed em src/dashboard.py) fazia
o serviço finalizar a sessão no meio da carga com end_reason='manual' e a
auto-detecção abrir outra 6-12s depois: a mesma carga aparecia em duas
linhas na aba Carregamentos. Este script funde a sessão seguinte na
anterior, reconstruindo a linha como se a carga nunca tivesse sido partida.

Uso:
    python3 scripts/merge_charge_sessions.py <id_anterior> <id_seguinte> [--db CAMINHO] [--yes]

Ex.: python3 scripts/merge_charge_sessions.py 122 123

Regras de segurança (nada é alterado se algo não bater):
  - nenhuma das duas sessões pode estar 'active';
  - a sessão seguinte deve começar até 120s depois do fim da anterior
    (padrão do falso "desligado externamente");
  - o contador de energia da seguinte deve ser >= o da anterior.

Campos fundados: a linha resultante herda o início (start_time, soc_start,
contador inicial, custo/kWh) da anterior e o fim real (end_time, soc_end,
status, end_reason) da seguinte; duração é a soma das efetivas (exclui as
caudas de idle) e energia é o delta dos contadores.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "tuya_history.db"
MAX_GAP_S = 120

COLS = (
    "id, session_uuid, start_time, end_time, status, soc_start, soc_end,"
    " soc_target, battery_kwh, start_energy_kwh, end_energy_kwh,"
    " energy_delivered_kwh, duration_seconds, avg_power_w, cost_per_kwh,"
    " total_cost, end_reason"
)


def _parse_ts(s):
    return datetime.fromisoformat(s)


def _fetch(conn, session_id):
    row = conn.execute(
        f"SELECT {COLS} FROM charge_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not row:
        sys.exit(f"❌ Sessão {session_id} não encontrada.")
    keys = [c.strip() for c in COLS.split(",")]
    return dict(zip(keys, row))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prev_id", type=int, help="id da sessão anterior (mantida)")
    ap.add_argument("next_id", type=int, help="id da sessão seguinte (fundida e removida)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"DB (padrão: {DEFAULT_DB})")
    ap.add_argument("--yes", action="store_true", help="aplica sem perguntar")
    args = ap.parse_args()

    if not args.db.exists():
        sys.exit(f"❌ DB não encontrado: {args.db}")
    conn = sqlite3.connect(args.db)
    conn.execute("BEGIN IMMEDIATE")
    try:
        prev = _fetch(conn, args.prev_id)
        nxt = _fetch(conn, args.next_id)

        for label, s in (("anterior", prev), ("seguinte", nxt)):
            if s["status"] == "active":
                sys.exit(f"❌ Sessão {label} (id {s['id']}) ainda está 'active' — finalize primeiro.")
            if not s["end_time"]:
                sys.exit(f"❌ Sessão {label} (id {s['id']}) sem end_time.")

        start_gap = (_parse_ts(nxt["start_time"]) - _parse_ts(prev["start_time"])).total_seconds()
        if start_gap <= 0:
            sys.exit("❌ A sessão seguinte deve ser posterior à anterior.")
        gap = (_parse_ts(nxt["start_time"]) - _parse_ts(prev["end_time"])).total_seconds()
        if not (0 <= gap <= MAX_GAP_S):
            sys.exit(
                f"❌ Intervalo entre as sessões é {gap:.0f}s (limite {MAX_GAP_S}s) — "
                "não parece o padrão do falso 'desligado externamente'."
            )
        if (nxt["end_energy_kwh"] or 0) < (prev["start_energy_kwh"] or 0):
            sys.exit("❌ Contador da sessão seguinte menor que o inicial da anterior — recusa.")

        end_energy = nxt["end_energy_kwh"] or 0.0
        start_energy = prev["start_energy_kwh"] or 0.0
        energy = round(max(0.0, end_energy - start_energy), 4)
        duration = (prev["duration_seconds"] or 0) + (nxt["duration_seconds"] or 0)
        avg_power_w = round(energy / (duration / 3600.0) * 1000, 0) if duration > 0 else 0
        total_cost = round(energy * (prev["cost_per_kwh"] or 0), 2)

        merged = {
            "end_time": nxt["end_time"],
            "end_energy_kwh": end_energy,
            "energy_delivered_kwh": energy,
            "duration_seconds": duration,
            "soc_end": nxt["soc_end"],
            "avg_power_w": avg_power_w,
            "total_cost": total_cost,
            "end_reason": nxt["end_reason"],
            "status": nxt["status"],
        }

        print(f"── Sessão anterior (mantida, id {prev['id']})")
        for k in ("start_time", "end_time", "status", "end_reason", "soc_start",
                  "soc_end", "energy_delivered_kwh", "duration_seconds"):
            print(f"   {k:>22}: {prev[k]}")
        print(f"── Sessão seguinte (removida, id {nxt['id']})")
        for k in ("start_time", "end_time", "status", "end_reason", "soc_start",
                  "soc_end", "energy_delivered_kwh", "duration_seconds"):
            print(f"   {k:>22}: {nxt[k]}")
        print(f"── Linha fundada (id {prev['id']})")
        for k, v in merged.items():
            print(f"   {k:>22}: {v}")

        if not args.yes:
            if not sys.stdin.isatty():
                sys.exit("❌ Sem --yes e sem terminal interativo — nada foi alterado.")
            if input("\nAplicar? (s/N): ").strip().lower() != "s":
                sys.exit("🛑 Abortado — nada foi alterado.")

        conn.execute(
            """UPDATE charge_sessions
               SET end_time = ?, end_energy_kwh = ?, energy_delivered_kwh = ?,
                   duration_seconds = ?, soc_end = ?, avg_power_w = ?,
                   total_cost = ?, end_reason = ?, status = ?
               WHERE id = ?""",
            (
                merged["end_time"], merged["end_energy_kwh"],
                merged["energy_delivered_kwh"], merged["duration_seconds"],
                merged["soc_end"], merged["avg_power_w"], merged["total_cost"],
                merged["end_reason"], merged["status"], prev["id"],
            ),
        )
        conn.execute("DELETE FROM charge_sessions WHERE id = ?", (nxt["id"],))
        conn.commit()
        print(f"✅ Sessão {nxt['id']} fundida em {prev['id']} — carga única de "
              f"{energy:.2f} kWh em {duration / 60:.1f} min.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
