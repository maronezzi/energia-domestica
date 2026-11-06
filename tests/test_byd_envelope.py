#!/usr/bin/env python3
"""Envelope vazio da nuvem BYD (carro dormindo/offline) não pode virar leitura.

Quando o T-Box não responde, a nuvem devolve realtime com timestamp=None e
TODOS os numéricos em 0 (soc=0, range=0, temp=0, charge_state=-1). Gravar
isso como leitura desenhou vales falsos no gráfico de SOC (188 linhas só em
06/11/2026) e mostrava "0 km"/"0 °C" no status. A guarda do _collect anula
os zeros quando data_ts é None (mesma regra da sentinela do odo_km) e
preserva leitura nonzero.

Run with:  python3 tests/test_byd_envelope.py -v
"""
import asyncio
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


class FakeRt:
    """Molde do realtime da BYD com os campos que o _collect lê."""

    def __init__(self, **kw):
        defaults = dict(
            timestamp=None,  # None = envelope vazio (carro dormindo)
            elec_percent=0.0, endurance_mileage=0.0, ev_endurance=0.0,
            oil_endurance=0.0, oil_percent=0.0, total_mileage=None,
            speed=0.0, charge_state=-1, is_charging=False,
            is_charger_connected=False, time_to_full_minutes=None,
            temp_in_car=0.0, is_locked=None, is_any_door_open=False,
            is_any_window_open=False, is_online=False,
            nearest_energy_consumption=None,
            left_front_tire_pressure=0.0, right_front_tire_pressure=0.0,
            left_rear_tire_pressure=0.0, right_rear_tire_pressure=0.0,
            tire_press_unit=2,
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


class TestEnvelope(unittest.TestCase):
    def _collect(self, rt):
        import byd_client

        class _NoGpsClient:
            async def get_vehicle_realtime(self, vin):
                return rt

        return byd_client._collect(_NoGpsClient(), "VIN", 1, False)
        # gps_i=1 % GPS_EVERY_STILL=4 ≠ 0 e moving==moving_prev → sem GPS

    def test_envelope_vazio_nao_grava_soc_zero(self):
        snap, pos, moving = asyncio.run(self._collect(FakeRt()))
        self.assertIsNone(snap["data_ts"])
        for k in ("soc", "range_ev", "range_total", "range_oil", "temp_in_car"):
            self.assertIsNone(snap[k], f"{k} deveria virar None no envelope")
        self.assertIsNone(snap["odo_km"])
        self.assertFalse(moving)  # speed 0 → parado (conservador)

    def test_leitura_real_passa_intacta(self):
        rt = FakeRt(timestamp=1794063360.0, elec_percent=40.0,
                    endurance_mileage=180.0, ev_endurance=150.0, speed=0.0)
        snap, pos, moving = asyncio.run(self._collect(rt))
        self.assertIsNotNone(snap["data_ts"])
        self.assertEqual(snap["soc"], 40.0)
        self.assertEqual(snap["range_ev"], 150.0)

    def test_zero_com_data_ts_real_e_leitura(self):
        # com timestamp presente, 0 não é descartado (é leitura da BYD,
        # rara mas legítima — ex.: soc=0 não acontece na prática, mas a
        # guarda não pode descartar por causa do timestamp)
        rt = FakeRt(timestamp=1794063360.0, elec_percent=0.0,
                    temp_in_car=0.0)
        snap, pos, moving = asyncio.run(self._collect(rt))
        self.assertEqual(snap["soc"], 0.0)
        self.assertEqual(snap["temp_in_car"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
