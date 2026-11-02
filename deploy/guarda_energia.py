#!/usr/bin/env python3
"""
Guarda de energia — o PC de bateria como sentinela da casa.

O PC fica 24/7 na tomada; quando a rede cai, o carregador dele desliga
(/sys/class/power_supply/ADP1/online → 0) e a placa cubie2 (na mesma rede
elétrica) sai do ar em segundos. A partir disso o guarda:

  • confirma a queda (2 ticks seguidos de carregador fora + placa fora);
  • alerta NA TELA se houver usuário logado (notify-send, urgência crítica);
  • alerta por Telegram se houver internet (best-effort: o roteador morre
    junto — o alerta de QUEDA pode não sair; o de VOLTA sai confiável);
  • minimiza recursos: para a lista de units pesadas (padrão ollama.service)
    e reduz a própria cadência de 15s para 60s;
  • avisa a volta da energia com a duração e religa as units;
  • avisa bateria < LOW_BATT_PCT com a queda aberta (a guarda morre junto).

Carregador fora SEM placa fora ≠ falta confirmada: alguém pode ter
desplugado o PC — alerta só na tela, sem minimizar, sem Telegram.
Placa fora SEM carregador fora: log apenas (o vigia da placa cobre).

Fonte da verdade: deploy/guarda_energia.py NO REPO (padrão do vigia).
A cópia viva fica em /usr/local/bin/guarda_energia.py no PC, instalada
pelo dono (sudo — ver deploy/guarda-energia.service).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse

# ─── Config (env sobrepõe; mesmos valores no .service) ──────────
BOARD_URL = os.environ.get("GUARDA_BOARD_URL",
                           "http://192.168.18.133:8050/api/status")
BOARD_TIMEOUT_S = 3
AC_ONLINE_PATH = os.environ.get("GUARDA_AC",
                                "/sys/class/power_supply/ADP1/online")
BATT_CAPACITY_PATH = os.environ.get("GUARDA_BATT",
                                    "/sys/class/power_supply/BAT1/capacity")
TOKEN_FILE = os.environ.get("GUARDA_TOKEN_FILE", "/etc/guarda/token")
CHAT_ID = os.environ.get("GUARDA_CHAT", "71802842")
UNITS_STOP = [u for u in os.environ.get(
    "GUARDA_UNITS", "ollama.service").split(",") if u.strip()]
TICK_S = 15            # cadência normal
OUTAGE_TICK_S = 60     # cadência com a queda confirmada (economia)
DEBOUNCE_TICKS = 2     # ticks seguidos para confirmar
LOW_BATT_PCT = 20
STATE_FILE_CANDIDATES = ["/var/lib/guarda/estado.json",
                         os.path.expanduser("~/.local/state/guarda/estado.json")]


# ─── Leituras (pure-able: caminhos em constantes para os testes) ─
def ac_online(path: str = AC_ONLINE_PATH) -> bool | None:
    """True = na tomada. None = máquina sem info de fonte (não julga)."""
    try:
        with open(path) as f:
            return f.read().strip() == "1"
    except OSError:
        return None


def battery_pct(path: str = BATT_CAPACITY_PATH) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def board_up(url: str = BOARD_URL, timeout: int = BOARD_TIMEOUT_S) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def fmt_dur(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}min"
    h = m // 60
    return f"{h}h {m % 60}min"


# ─── Máquina de estados (pura — testável sem I/O) ───────────────
DEBOUNCE = DEBOUNCE_TICKS


class GuardaState:
    """Estado persistível da guarda. on_tick() devolve as AÇÕES do tick:

    {"event": "nada" | "queda_confirmada" | "bateria_so" | "bateria_fraca"
              | "recuperou",
     "notify": str|None, "telegram": str|None,
     "stop_units": bool, "start_units": bool}
    """

    def __init__(self, state: dict | None = None):
        s = state or {}
        self.queda_start = s.get("queda_start")             # epoch | None
        self.battery_only_warned = s.get("battery_only_warned", False)
        self.low_batt_warned = s.get("low_batt_warned", False)
        self.ac_bad = 0
        self.ac_good = 0
        self.board_bad = 0

    def to_dict(self) -> dict:
        return {
            "queda_start": self.queda_start,
            "battery_only_warned": self.battery_only_warned,
            "low_batt_warned": self.low_batt_warned,
        }

    @classmethod
    def from_dict(cls, state):
        return cls(state)

    def on_tick(self, ac_online: bool | None, board_up: bool,
                batt_pct: int | None, now: float) -> dict:
        a = {"event": "nada", "notify": None, "telegram": None,
             "stop_units": False, "start_units": False}
        if ac_online is False:
            self.ac_bad += 1
            self.ac_good = 0
        elif ac_online is True:
            self.ac_bad = 0
            self.ac_good += 1
        # None: sem informação — não abre nem recupera
        self.board_bad = 0 if board_up else self.board_bad + 1

        # tomada de volta por 2 ticks encerra o episódio "bateria-só"
        # (silencioso — novo desplugamento volta a alertar)
        if ac_online is True and self.ac_good >= DEBOUNCE:
            self.battery_only_warned = False

        em_queda = self.queda_start is not None

        # ── recuperação: carregador de volta por 2 ticks ──
        if em_queda and self.ac_good >= DEBOUNCE:
            dur = now - (self.queda_start or now)
            b = f"{batt_pct}%" if batt_pct is not None else "—"
            a.update(
                event="recuperou",
                notify=(f"🔌 Energia VOLTOU — fora por {fmt_dur(dur)} "
                        f"(bateria {b}). Serviços pesados religando."),
                telegram=(f"🔌 *Energia VOLTOU* — fora por {fmt_dur(dur)} "
                          f"(bateria do PC: {b})"),
                start_units=True,
            )
            self.queda_start = None
            self.low_batt_warned = False
            self.battery_only_warned = False
            return a

        # ── abertura ──
        if self.ac_bad >= DEBOUNCE and not em_queda:
            if self.board_bad >= DEBOUNCE:
                self.queda_start = now
                b = f"{batt_pct}%" if batt_pct is not None else "—"
                ts = time.strftime("%d/%m %H:%M", time.localtime(now))
                a.update(
                    event="queda_confirmada",
                    notify=(f"⚡ ENERGIA CAIU — PC na bateria ({b}), "
                            f"placa fora do ar. Cortando serviços pesados."),
                    telegram=(f"⚡ *ENERGIA CAIU* ({ts}) — PC na bateria ({b}), "
                              f"placa fora do ar. Guarda ativa."),
                    stop_units=True,
                )
                return a
            if not self.battery_only_warned:
                self.battery_only_warned = True
                a.update(
                    event="bateria_so",
                    notify=(f"🔋 PC na bateria ({batt_pct or '?'}%) — se não "
                            f"foi você, a energia caiu NESTE circuito "
                            f"(a placa segue no ar)."),
                )
                return a

        # ── em queda: bateria acabando ──
        if (em_queda and batt_pct is not None and batt_pct < LOW_BATT_PCT
                and not self.low_batt_warned):
            self.low_batt_warned = True
            a.update(
                event="bateria_fraca",
                notify=f"🪫 Bateria {batt_pct}% — a guarda morre em breve.",
                telegram=(f"🪫 *Bateria do PC em {batt_pct}%* — guarda "
                          f"encerrando; a casa segue sem luz."),
            )
            return a
        return a


# ─── Efeitos (I/O real — injetáveis nos testes) ─────────────────
def parse_sessions(list_out: str, show_fn) -> list[tuple[str, str]]:
    """(uid, user) das sessões GRÁFICAS ativas."""
    out = []
    for line in list_out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        sid, uid, user = parts[0], parts[1], parts[2]
        info = show_fn(sid) or ""
        if "Active=yes" in info and ("Type=x11" in info or "Type=wayland" in info):
            out.append((uid, user))
    return out


def graphical_sessions(runner=None) -> list[tuple[str, str]]:
    runner = runner or subprocess.run
    try:
        listing = runner(["loginctl", "list-sessions", "--no-legend"],
                         capture_output=True, text=True, timeout=5)
    except Exception:
        return []

    def show_fn(sid):
        r = runner(["loginctl", "show-session", sid, "-p", "Type", "-p", "Active"],
                   capture_output=True, text=True, timeout=5)
        return r.stdout

    return parse_sessions(listing.stdout, show_fn)


def notify_users(text: str, runner=None) -> int:
    """notify-send crítico para cada sessão gráfica. Retorna nº de avisos."""
    sent = 0
    euid = os.geteuid()
    for uid, user in graphical_sessions(runner):
        bus = f"/run/user/{uid}/bus"
        if not os.path.exists(bus):
            continue
        cmd = ["notify-send", "-u", "critical", "-a", "Guarda de Energia",
               "⚡ Guarda de Energia", text]
        if int(uid) != euid:
            cmd = ["sudo", "-u", user, "env",
                   f"DBUS_SESSION_BUS_ADDRESS=unix:path={bus}"] + cmd
        try:
            r = (runner or subprocess.run)(cmd, capture_output=True,
                                           text=True, timeout=10)
            if r.returncode == 0:
                sent += 1
        except Exception:
            pass
    return sent


def read_token() -> str | None:
    tok = os.environ.get("GUARDA_TOKEN")
    if tok:
        return tok.strip()
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def send_telegram(text: str, token: str | None = None) -> bool:
    """Best-effort: sem token/sem internet → False silencioso."""
    token = token or read_token()
    if not token:
        return False
    try:
        data = urllib.parse.urlencode(
            {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
        ).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def set_units(units: list[str], stop: bool) -> list[str]:
    """Para/religa as units pesadas. Sem root: skip com log (o serviço
    roda como root; a execução manual do dono não precisa parar nada)."""
    results = []
    if os.geteuid() != 0:
        return ["sem root — units NÃO tocadas (isto é normal fora do serviço)"]
    verb = "stop" if stop else "start"
    for u in units:
        try:
            r = subprocess.run(["systemctl", verb, u],
                               capture_output=True, text=True, timeout=30)
            results.append(f"{verb} {u}: rc={r.returncode}")
        except Exception as e:
            results.append(f"{verb} {u}: erro {e}")
    return results


# ─── Estado em disco (a queda sobrevive a restart do guarda) ────
def _state_path() -> str | None:
    for p in STATE_FILE_CANDIDATES:
        d = os.path.dirname(p)
        try:
            os.makedirs(d, exist_ok=True)
            return p
        except OSError:
            continue
    return None


def load_state() -> dict:
    p = _state_path()
    if p:
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    p = _state_path()
    if not p:
        return
    try:
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, p)
    except Exception:
        pass


# ─── Loop principal ─────────────────────────────────────────────
def _tick(gs: GuardaState, force_outage: bool) -> dict:
    ac = ac_online()
    if force_outage:
        ac = False
    batt = battery_pct()
    up = False if force_outage else board_up()
    if ac is None and not force_outage:
        # máquina sem sinal de fonte: nada a julgar
        print("guarda: sem leitura de fonte (AC) — tick ignorado")
        return {"event": "nada", "notify": None, "telegram": None,
                "stop_units": False, "start_units": False}
    return gs.on_tick(False if force_outage else ac, up, batt, time.time())


def run_effects(a: dict, gs: GuardaState) -> None:
    if a.get("stop_units"):
        for line in set_units(UNITS_STOP, stop=True):
            print(f"guarda: {line}")
    if a.get("start_units"):
        for line in set_units(UNITS_STOP, stop=False):
            print(f"guarda: {line}")
    if a.get("notify") and notify_users(a["notify"]):
        print(f"guarda: alerta na tela enviado")
    if a.get("telegram"):
        ok = send_telegram(a["telegram"])
        print(f"guarda: telegram {'enviado' if ok else 'FALHOU (sem internet/token?)'}")


def main(argv: list[str]) -> int:
    once = "--once" in argv
    test = "--test" in argv
    if test:
        once = True  # 🧪 teste roda UM tick de queda simulada e sai
    interval = TICK_S
    gs = GuardaState.from_dict(load_state())
    if gs.queda_start:
        print(f"guarda: QUEDA já aberta (desde "
              f"{time.strftime('%d/%m %H:%M', time.localtime(gs.queda_start))}) "
              f"— seguindo dela")
    print(f"guarda: iniciada (placa={BOARD_URL}, units={UNITS_STOP}, "
          f"tick={TICK_S}s)")
    while True:
        if test:
            # 🧪 simula os DOIS ticks da queda (o 1º é só debounce)
            _tick(gs, force_outage=True)
        a = _tick(gs, force_outage=test)
        if a.get("event") != "nada" or once:
            print(f"guarda: evento={a['event']}")
        if test and once:
            # 🧪 modo teste: prova os ALERTAS (tela+telegram rotulados) sem
            # tocar em units nem gravar estado
            if a.get("notify"):
                a["notify"] = "🧪 TESTE — " + a["notify"]
            if a.get("telegram"):
                a["telegram"] = "🧪 TESTE (ignore) — " + a["telegram"]
            a["stop_units"] = False
            a["start_units"] = False
            run_effects(a, gs)
            return 0
        run_effects(a, gs)
        if a.get("event") in ("queda_confirmada",):
            interval = OUTAGE_TICK_S
        elif a.get("event") == "recuperou":
            interval = TICK_S
        save_state(gs.to_dict())
        if once:
            print(f"guarda: estado atual — AC={'ok' if ac_online() else 'FORA'}, "
                  f"placa={'ok' if board_up() else 'FORA'}, "
                  f"bateria={battery_pct()}%, queda_aberta={gs.queda_start is not None}")
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
