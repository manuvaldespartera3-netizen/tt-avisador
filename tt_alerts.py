"""
tt_alerts.py - Avisador TT Cup -> Telegram (pensado para GitHub Actions).

Cada ejecucion consulta la API varias veces (cada POLL_SECONDS) durante
RUN_SECONDS y termina. GitHub la relanza cada 5 minutos. Lo ya enviado se
guarda en state.json para no repetir avisos.

Secretos / variables:
  ZYLA_API_KEY    clave de Zyla Labs
  TELEGRAM_TOKEN  token del bot nuevo (BotFather)
  MY_CHAT_ID      tu id de Telegram
  TEST_MODE       1 = modo prueba (resumenes cada 15 min). 0 = solo picks
"""
import json
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request

import tt_feed
from tt_feed import fetch, build_groups, h2h

TG = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()
POLL = int(os.getenv("POLL_SECONDS", "60"))
RUN = int(os.getenv("RUN_SECONDS", "270"))      # ~4,5 min por ejecucion
TEST = os.getenv("TEST_MODE", "1") == "1"
REPORT_EVERY = 15 * 60
STATE_FILE = os.getenv("STATE_FILE", "state.json")
VERSION = "3"   # al cambiarlo se reenvia el mensaje de arranque

state = {"sent": [], "last_report": 0, "last_error": "", "started": False, "urls": {}}


def load_state():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except Exception:
        pass
    for k, v in (state.get("urls") or {}).items():
        if v and not tt_feed.URLS.get(k):
            tt_feed.URLS[k] = v


def save_state():
    state["sent"] = state["sent"][-3000:]
    state["urls"] = dict(tt_feed.URLS)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send(text):
    print(text, flush=True)
    if not (TG and CHAT):
        return
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG}/sendMessage", data, timeout=15)
    except Exception as ex:
        print("WARN telegram", ex)


def startup_report():
    lines = ["🟢 Avisador TT Cup activo" + (" (MODO PRUEBA)" if TEST else "")]
    try:
        events, live = fetch()
        groups, parsed = build_groups(events)
        lines.append(f"✅ Conexión con Zyla OK: {len(parsed)} partidos TT Cup hoy, "
                     f"{live} en vivo, {len(groups)} grupos")
    except Exception as ex:
        lines.append(f"❌ No pude leer TT Cup en Zyla: {ex}")
    lines.append("✅ H2H activo" if tt_feed.URL_H2H else "⏳ H2H pendiente de configurar")
    send("\n".join(lines))


def kind(r):
    return "OFICIAL" if "eliminado" in r["reason"] else "POTENCIAL"


def h2h_line(m):
    hh = h2h(m.mid) if m.mid else ""
    return hh if hh else "Sin H2H"


def confirm_text(r, g):
    m = r["match"]
    k = kind(r)
    when = f" · {m.time}" if m.time else ""
    rival = "eliminado" if k == "OFICIAL" else "ya clasificado"
    head = (f"🟢🟢 PICK OFICIAL: {r['pick'].upper()} 🟢🟢" if k == "OFICIAL"
            else f"🟡 PICK POTENCIAL: {r['pick'].upper()}")
    txt = f"{head}\nvs {r['vs']} ({rival}){when}\n{h2h_line(m)}"
    if r["warn"]:
        txt += "\n⚠️ puede decidir por puntos"
    return txt


def heads_up_text(r, pm, w):
    m = r["match"]
    k = kind(r)
    sa, sb = pm.live
    cond = f"Si gana {w}"
    if r["partial"]:
        cond += f" ({r['partial']})"
    return (f"⏳ ATENTO: {pm.a} {sa}-{sb} {pm.b} (3.er set o más)\n"
            f"{cond} → posible PICK {k} {r['pick'].upper()} vs {r['vs']}"
            + (f" · {m.time}" if m.time else ""))


def in_late_stage(pm):
    """Mitad del 3.er set o mas avanzado."""
    if not pm.live:
        return False
    played = sum(pm.live)
    return played >= 3 or (played == 2 and pm.live_pts >= 10)


def tick():
    events, live_total = fetch()
    groups, parsed = build_groups(events)

    if TEST and time.time() - state["last_report"] >= REPORT_EVERY:
        state["last_report"] = time.time()
        live = [p for p in parsed if p["status"] == "inprogress"]
        lines = [f"🧪 TT Cup: {len(parsed)} partidos hoy · {len(live)} en vivo · "
                 f"{len(groups)} grupos"]
        for p in live[:5]:
            lines.append(f"  🔴 {p['a']} {p['score']} {p['b']}")
        send("\n".join(lines))

    sent = set(state["sent"])

    def once(key, text_fn):
        if key in sent:
            return
        sent.add(key)
        state["sent"].append(key)
        send(text_fn())

    for g in groups:
        if not g.pending():
            continue
        for r in g.analyze():
            m = r["match"]
            # regla: solo picks de partidos que AUN NO han empezado
            if m.final or m.live is not None:
                continue
            if not r["conds"]:
                # pick seguro -> confirmacion
                once(f"C|{m.mid}|{r['pick']}", lambda r=r, g=g: confirm_text(r, g))
            elif len(r["conds"]) == 1:
                pm, w = r["conds"][0]
                if in_late_stage(pm):
                    once(f"H|{m.mid}|{r['pick']}|{pm.mid}|{w}|{r['partial']}",
                         lambda r=r, pm=pm, w=w: heads_up_text(r, pm, w))


def main():
    load_state()
    if state.get("started") != VERSION:
        startup_report()
        state["started"] = VERSION
        save_state()
    end = time.time() + RUN
    while True:
        try:
            tick()
            state["last_error"] = ""
            state["fails"] = 0
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"
            traceback.print_exc()
            state["fails"] = state.get("fails", 0) + 1
            # 429 (demasiadas peticiones) o fallos sueltos de red: reintentar en
            # silencio; solo avisar si el problema dura ~10 intentos seguidos.
            transient = "429" in err or "timed out" in err or "URLError" in err \
                or "502" in err or "503" in err or "504" in err
            if transient:
                if state["fails"] == 10:
                    send(f"⚠️ El avisador lleva ~10 min sin poder leer Zyla: {err[:200]}")
                time.sleep(60)          # espera extra antes de reintentar
            elif err != state["last_error"]:
                send(f"⚠️ Error avisador: {err[:300]}")
                state["last_error"] = err
        save_state()
        if "--loop" not in sys.argv and time.time() + POLL > end:
            break
        time.sleep(POLL)


if __name__ == "__main__":
    if "--reset" in sys.argv:
        state["started"] = False
    main()
