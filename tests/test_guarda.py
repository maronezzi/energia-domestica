#!/usr/bin/env python3
"""
Guarda de energia no PC (sentinela de bateria).

Núcleo testado: GuardaState.on_tick — debounce de 2 ticks, os 4 eventos
(queda confirmada, bateria-só, bateria fraca, recuperação com duração),
sobrevivência de restart (to_dict/from_dict) e o parser de sessões
gráficas. O comportamento-chave: carregador fora + placa fora = ENERGIA
CAIU; carregador fora sozinho = alerta só na tela (desplugado ≠ queda).

Run with:  python3 -m pytest tests/test_guarda.py -v
"""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "deploy"))

import guarda_energia as g  # noqa: E402

T0 = 1_800_000_000.0
TICK = 15.0


def tick(st, ac, board, batt=98, n=T0):
    return st.on_tick(ac, board, batt, n)


class TestQuedaConfirmada(unittest.TestCase):
    """Carregador fora + placa fora = ENERGIA CAIU (após 2 ticks)."""

    def test_primeiro_tick_fora_soh_debounce(self):
        st = g.GuardaState()
        a = tick(st, ac=False, board=False)
        self.assertEqual(a["event"], "nada")
        self.assertIsNone(st.queda_start)

    def test_segundo_tick_confirma_e_para_units(self):
        st = g.GuardaState()
        tick(st, ac=False, board=False)
        a = tick(st, ac=False, board=False, n=T0 + TICK)
        self.assertEqual(a["event"], "queda_confirmada")
        self.assertTrue(a["stop_units"])
        self.assertFalse(a["start_units"])
        self.assertIn("bateria", a["notify"].lower())
        self.assertIn("ENERGIA CAIU", a["telegram"])
        self.assertEqual(st.queda_start, T0 + TICK)

    def test_bateria_fraca_alerta_uma_vez_so(self):
        st = g.GuardaState()
        tick(st, ac=False, board=False)
        a = tick(st, ac=False, board=False, batt=15, n=T0 + TICK)
        self.assertEqual(a["event"], "queda_confirmada")  # a queda fala primeiro
        a = tick(st, ac=False, board=False, batt=14, n=T0 + 2 * TICK)
        self.assertEqual(a["event"], "bateria_fraca")
        a = tick(st, ac=False, board=False, batt=13, n=T0 + 3 * TICK)
        self.assertEqual(a["event"], "nada")  # não repete

    def test_em_queda_sem_repetir_alerta(self):
        st = g.GuardaState()
        tick(st, ac=False, board=False)
        tick(st, ac=False, board=False, n=T0 + TICK)
        for i in range(3, 8):
            a = tick(st, ac=False, board=False, n=T0 + i * TICK)
            self.assertEqual(a["event"], "nada")


class TestRecuperacao(unittest.TestCase):
    def test_carregador_volta_por_2_ticks(self):
        st = g.GuardaState()
        tick(st, ac=False, board=False)
        st.queda_start = T0  # ancora fixa p/ duração determinística
        a = tick(st, ac=True, board=False, n=T0 + 100)  # 1º bom: debounce
        self.assertEqual(a["event"], "nada")
        a = tick(st, ac=True, board=False, n=T0 + 160)  # 2º bom: recupera
        self.assertEqual(a["event"], "recuperou")
        self.assertTrue(a["start_units"])
        self.assertIn("2min", a["notify"])  # duração = 160s → 2min
        self.assertIsNone(st.queda_start)

    def test_restart_no_meio_da_queda_preserva_duracao(self):
        st = g.GuardaState()
        tick(st, ac=False, board=False)
        st.queda_start = T0
        salvo = st.to_dict()
        # guarda reinicia (service restart) e recarrega do disco
        st2 = g.GuardaState.from_dict(salvo)
        self.assertEqual(st2.queda_start, T0)
        tick(st2, ac=True, board=True, n=T0 + 3700)   # 1º bom: debounce
        a = tick(st2, ac=True, board=True, n=T0 + 3760)  # 2º: recupera
        self.assertEqual(a["event"], "recuperou")
        self.assertIn("1h", a["notify"])


class TestBateriaSo(unittest.TestCase):
    """Carregador fora com a PLACA no ar: desplugado ≠ falta de energia."""

    def test_alerta_so_na_tela_sem_units_sem_telegram(self):
        st = g.GuardaState()
        tick(st, ac=False, board=True)
        a = tick(st, ac=False, board=True, n=T0 + TICK)
        self.assertEqual(a["event"], "bateria_so")
        self.assertIn("bateria", a["notify"].lower())
        self.assertIsNone(a["telegram"])
        self.assertFalse(a["stop_units"])
        self.assertIsNone(st.queda_start)

    def test_nao_repete_e_reseta_ao_voltar_ac(self):
        st = g.GuardaState()
        tick(st, ac=False, board=True)
        tick(st, ac=False, board=True, n=T0 + TICK)
        self.assertEqual(tick(st, ac=False, board=True, n=T0 + 2 * TICK)["event"], "nada")
        # volta a tomada 2 ticks → reset silencioso
        tick(st, ac=True, board=True, n=T0 + 3 * TICK)
        tick(st, ac=True, board=True, n=T0 + 3.5 * TICK)
        # desplugou de novo → alerta de novo (novo episódio)
        tick(st, ac=False, board=True, n=T0 + 4 * TICK)
        a = tick(st, ac=False, board=True, n=T0 + 5 * TICK)
        self.assertEqual(a["event"], "bateria_so")

    def test_queda_real_tem_prioridade_sobre_bateria_so(self):
        # placa fora ANTES do segundo tick de carregador fora = queda
        st = g.GuardaState()
        tick(st, ac=False, board=False)
        a = tick(st, ac=False, board=False, n=T0 + TICK)
        self.assertEqual(a["event"], "queda_confirmada")


class TestPlacaSo(unittest.TestCase):
    def test_placa_fora_com_ac_ok_nao_alerta(self):
        st = g.GuardaState()
        tick(st, ac=True, board=False)
        a = tick(st, ac=True, board=False, n=T0 + TICK)
        self.assertEqual(a["event"], "nada")
        self.assertIsNone(st.queda_start)


class TestEfeitos(unittest.TestCase):
    def test_parse_sessions_soh_graficas_ativas(self):
        list_out = "c1 1000 bruno seat0 - active no -\nc2 1000 bruno seat0 tty2 active no -\n"
        shows = {
            "c1": "Type=x11\nActive=yes\n",
            "c2": "Type=tty\nActive=yes\n",
        }
        pairs = g.parse_sessions(list_out, lambda sid: shows[sid])
        self.assertEqual(pairs, [("1000", "bruno")])

    def test_parse_sessions_inativa_fora(self):
        pairs = g.parse_sessions("c3 1000 bruno seat0 - idle no -",
                                 lambda sid: "Type=x11\nActive=no\n")
        self.assertEqual(pairs, [])

    def test_notify_cmd_como_root_usa_sudo(self):
        # root avisando o bruno → sudo + DBUS do usuário
        chamadas = []

        def fake_run(cmd, **kw):
            chamadas.append(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        with unittest.mock.patch.object(g.os, "geteuid", return_value=0), \
             unittest.mock.patch.object(g, "graphical_sessions",
                                        return_value=[("1000", "bruno")]), \
             unittest.mock.patch.object(g, "subprocess") as sp:
            sp.run.side_effect = fake_run
            n = g.notify_users("teste")
        self.assertEqual(n, 1)
        self.assertIn("sudo", chamadas[0])
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
                      chamadas[0])

    def test_notify_cmd_como_proprio_usuario_sem_sudo(self):
        chamadas = []

        def fake_run(cmd, **kw):
            chamadas.append(cmd)
            r = MagicMock()
            r.returncode = 0
            return r

        with unittest.mock.patch.object(g.os, "geteuid", return_value=1000), \
             unittest.mock.patch.object(g, "graphical_sessions",
                                        return_value=[("1000", "bruno")]), \
             unittest.mock.patch.object(g, "subprocess") as sp:
            sp.run.side_effect = fake_run
            n = g.notify_users("teste")
        self.assertEqual(n, 1)
        self.assertNotIn("sudo", chamadas[0])  # bruno avisando a si mesmo

    def test_units_sem_root_soh_loga(self):
        with unittest.mock.patch.object(g.os, "geteuid", return_value=1000):
            out = g.set_units(["ollama.service"], stop=True)
        self.assertEqual(len(out), 1)
        self.assertIn("sem root", out[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
