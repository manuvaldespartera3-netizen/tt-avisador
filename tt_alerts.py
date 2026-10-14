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

TG = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("MY_CHAT_ID", "")
POLL = int(os.getenv("POLL_SECONDS", "60"))
RUN = int(os.getenv("RUN_SECONDS", "270"))      # ~4,5 min por ejecucion
TEST = os.getenv("TEST_MODE", "1") == "1"
REPORT_EVERY = 15 * 60
STATE_FILE = os.getenv("STATE_FILE", "state.json")
VERSION = "2"   # al cambiarlo se reenvia el mensaje de arranque

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


def pick_text(r, g):
    m = r["match"]
    when = f" {m.time}" if m.time else ""
    if r["conds"]:
        cond = " y ".join(f"gana {w} ({pm.a}-{pm.b})" for pm, w in r["conds"])
        if r["partial"]:
            cond += f" [{r['partial']}]"
        txt = f"⏳ Si {cond}\n→ PICK {r['pick']} vs {r['vs']}{when}"
    else:
        txt = f"✅ PICK {r['pick']} vs {r['vs']}{when}\n{r['reason']}"
    if r["warn"]:
        txt += "\n⚠️ puede decidir por puntos"
    hh = h2h(m.mid) if m.mid else ""
    txt += f"\n{hh}" if hh else "\nSin H2H"
    return f"🏓 {' · '.join(g.players)}\n{txt}"


def tick():
    events, live_total = fetch()
    groups, parsed = build_groups(events)

    if TEST and time.time() - state["last_report"] >= REPORT_EVERY:
        state["last_report"] = time.time()
        live = [p for p in parsed if p["status"] == "inprogress"]
        lines = [f"🧪 TT Cup: {len(parsed)} partidos hoy · {len(live)} en vivo · "
                 f"{len(groups)} grupos · tenis de mesa en vivo total: {live_total}"]
        for p in live[:5]:
            lines.append(f"  🔴 {p['a']} {p['score']} {p['b']}")
        send("\n".join(lines))

    sent = set(state["sent"])
    for g in groups:
        if not g.pending():
            continue
        for r in g.analyze():
            m = r["match"]
            if m.final:
                continue
            key = "|".join([m.mid, r["pick"]] + [f"{pm.mid}:{w}" for pm, w in r["conds"]]
                           + [str(r["partial"])])
            if key in sent:
                continue
            sent.add(key)
            state["sent"].append(key)
            send(pick_text(r, g))


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
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"
            traceback.print_exc()
            if err != state["last_error"]:
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
