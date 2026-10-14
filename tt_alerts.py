"""
tt_alerts.py - Avisador TT Cup -> Telegram.

Variables de entorno (Railway):
  ZYLA_API_KEY    clave de Zyla Labs
  TELEGRAM_TOKEN  token del bot de Telegram
  MY_CHAT_ID      tu chat de Telegram
  TEST_MODE       1 = modo prueba (informes de lo que llega). 0 = solo picks
  POLL_SECONDS    (opcional) segundos entre consultas, por defecto 60
Arranque:  python tt_alerts.py
"""
import os
import time
import traceback
import urllib.parse
import urllib.request

import tt_feed
from tt_feed import fetch, build_groups, h2h

TG = os.getenv("TELEGRAM_TOKEN", "")
CHAT = os.getenv("MY_CHAT_ID", "")
POLL = int(os.getenv("POLL_SECONDS", "60"))
TEST = os.getenv("TEST_MODE", "1") == "1"
REPORT_EVERY = 15 * 60          # en modo prueba: resumen cada 15 min

sent = set()
seen_matches = {}
last_report = 0
last_error = ""


def send(text):
    print(text)
    if not (TG and CHAT):
        return
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG}/sendMessage", data, timeout=15)
    except Exception as ex:
        print("WARN telegram", ex)


def startup_report():
    lines = ["🟢 Avisador TT Cup arrancado" + (" (MODO PRUEBA)" if TEST else "")]
    try:
        urls = tt_feed.discover()
        for k, label in (("live", "En vivo"), ("date", "Por fecha"), ("h2h", "H2H")):
            lines.append(f"{'✅' if urls[k] else '❌'} {label}")
        if not all(urls.values()):
            lines.append(f"(URLs encontradas: {len(tt_feed.FOUND)})")
            for u in tt_feed.FOUND[:40]:
                lines.append("· " + u.split(f"/api/{tt_feed.API_ID}/")[-1][:80])
    except Exception as ex:
        lines.append(f"❌ No pude leer las funciones de Zyla: {ex}")
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
    global last_report
    events, live_total = fetch()
    groups, parsed = build_groups(events)

    if TEST:
        now = time.time()
        if now - last_report >= REPORT_EVERY:
            last_report = now
            live = [p for p in parsed if p["status"] == "inprogress"]
            lines = [f"🧪 TT Cup: {len(parsed)} partidos hoy · {len(live)} en vivo · "
                     f"{len(groups)} grupos detectados · tenis de mesa en vivo total: {live_total}"]
            for p in live[:5]:
                lines.append(f"  🔴 {p['a']} {p['score']} {p['b']}")
            send("\n".join(lines))

    for g in groups:
        if not g.pending():
            continue
        for r in g.analyze():
            m = r["match"]
            if m.final:
                continue
            key = (m.mid, r["pick"], tuple((pm.mid, w) for pm, w in r["conds"]), r["partial"])
            if key in sent:
                continue
            sent.add(key)
            send(pick_text(r, g))


if __name__ == "__main__":
    startup_report()
    while True:
        try:
            tick()
            last_error = ""
        except Exception as ex:
            err = f"{type(ex).__name__}: {ex}"
            traceback.print_exc()
            if err != last_error:          # avisa de cada error nuevo una sola vez
                send(f"⚠️ Error avisador: {err[:300]}")
                last_error = err
        time.sleep(POLL)
