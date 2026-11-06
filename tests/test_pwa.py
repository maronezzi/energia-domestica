#!/usr/bin/env python3
"""
Unit tests for the PWA static layer: /manifest.json, /sw.js e /icons/{name}.

Regra central: são rotas de arquivo puro — nada de lógica, nada de estado.
O manifest tem de ser válido e apontar para arquivos que existem; o sw.js
só intercepta GET e nunca toca em /api/* (dado fresco vence cache).

Run with:  python3 -m pytest tests/test_pwa.py -v
"""
import sys
import json
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


class TestPwaRoutes(unittest.TestCase):
    """Rotas de estático chamadas direto (padrão do repo, sem TestClient)."""

    def setUp(self):
        import dashboard
        self.d = dashboard

    def test_manifest_rota(self):
        r = self.d.manifest_file()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.media_type, "application/manifest+json")

    def test_sw_rota(self):
        r = self.d.serviceworker()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.media_type, "text/javascript")

    def test_icone_rota(self):
        r = self.d.pwa_icon("icon-512.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.media_type, "image/png")

    def test_icone_fora_da_whitelist_404(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self.d.pwa_icon("../../etc/passwd")


class TestPwaFiles(unittest.TestCase):
    """Conteúdo dos arquivos servidos."""

    def setUp(self):
        self.src = PROJECT_ROOT / "src"

    def test_manifest_valido_e_icones_existem(self):
        m = json.loads((self.src / "manifest.json").read_text(encoding="utf-8"))
        for k in ("name", "short_name", "start_url", "display", "theme_color", "icons"):
            self.assertIn(k, m)
        self.assertEqual(m["display"], "standalone")
        self.assertTrue(m["icons"], "manifest sem ícones")
        for ic in m["icons"]:
            self.assertTrue((PROJECT_ROOT / "src" / ic["src"].lstrip("/")).is_file(),
                            f"ícone do manifest não existe: {ic['src']}")

    def test_sw_somente_get_e_sem_api(self):
        sw = (self.src / "sw.js").read_text(encoding="utf-8")
        self.assertIn("addEventListener('fetch'", sw)
        self.assertIn("req.method !== 'GET'", sw)      # POST de controle passa sempre
        self.assertIn("startsWith('/api/')", sw)       # dados: sempre rede
        self.assertIn("CACHE = 'energia-shell-", sw)   # versão p/ invalidação

    def test_icones_sao_png(self):
        for name in ("icon-512.png", "apple-touch-icon.png"):
            head = (self.src / "icons" / name).read_bytes()[:8]
            self.assertEqual(head, b"\x89PNG\r\n\x1a\n", f"{name} não é PNG")


class TestStatusTomadaHonesto(unittest.TestCase):
    """Regressão 06/11/2026: o painel exibia 'Na tomada' com o carro na rua.
    48h de snapshots provaram que is_charger_connected é flag de conectividade
    do T-Box (fica 1 até dirigindo a 58 km/h) e charge_state=15 é 'acordado',
    não 'plugado'. Tomada real = is_charging — a UI não pode alegar tomada
    por outro caminho."""

    def setUp(self):
        self.html = (PROJECT_ROOT / "src" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_sem_na_tomada_por_flag_ou_charge_state(self):
        self.assertNotIn("15: 'Na tomada'", self.html)   # mapa BYD_CHARGE_TXT
        self.assertNotIn("'Na tomada'", self.html)       # fallback is_connected

    def test_carregando_vem_somente_de_is_charging(self):
        self.assertIn("snap.is_charging ? BYD_CHARGE_TXT[1]", self.html)

    def test_byd_client_documenta_a_semantica_do_flag(self):
        src = (PROJECT_ROOT / "src" / "byd_client.py").read_text(encoding="utf-8")
        self.assertIn("não de tomada", src)


if __name__ == "__main__":
    unittest.main()
