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
import csv
import json
import os
from datetime import datetime
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
VERSION = "6"   # al cambiarlo se reenvia el mensaje de arranque
PICKS_FILE = os.getenv("PICKS_FILE", "picks.csv")
SUMMARY_HOUR = os.getenv("SUMMARY_AT", "23:45")   # hora del resumen diario
FIELDS = ["fecha", "hora", "tipo", "pick", "rival", "h2h_pick", "h2h_rival",
          "aviso_puntos", "match_id", "resultado", "marcador", "momento",
          "calidad", "enviado"]
LIVE_PTS_MAX = int(os.getenv("LIVE_PTS_MAX", "8"))   # puntos jugados en el 1.er set

state = {"sent": [], "last_report": 0, "last_error": "", "started": False, "urls": {},
         "last_summary": ""}
picks = []   # historial (se guarda en picks.csv dentro del repositorio)


def load_state():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except Exception:
        pass
    for k, v in (state.get("urls") or {}).items():
        if v and not tt_feed.URLS.get(k):
            tt_feed.URLS[k] = v


def load_picks():
    picks.clear()
    try:
        with open(PICKS_FILE, newline="", encoding="utf-8") as f:
            picks.extend(csv.DictReader(f))
    except FileNotFoundError:
        pass


def save_picks():
    with open(PICKS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for p in picks:
            w.writerow({k: p.get(k, "") for k in FIELDS})


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
    lines = ["🟢 Avisador TT Cup activo (v6)" + (" (MODO PRUEBA)" if TEST else ""),
             "Solo OFICIALES · ⭐ = alta calidad · cada pick trae su cuota mínima"]
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


def h2h_for(m, pick):
    """(victorias del pick, victorias del rival) o None."""
    d = h2h(m.mid) if m.mid else None
    if not d:
        return None
    return d if pick == m.a else (d[1], d[0])


# Resultado del historico (20/07-17/10, 234 picks oficiales):
#   ⭐ sin aviso de puntos + rival con 2 sets o menos en el grupo -> 84% (cuota min 1.30)
#   normal de dia (antes de las 20:00)                              -> 77% (cuota min 1.55)
#   normal de noche                                                 -> 48% (no se avisa)
#   POTENCIAL: 43%, peor que el nivel de los jugadores              -> no se avisa
# Los no avisados se siguen guardando en picks.csv para medirlos.
CUOTA_ESTRELLA = os.getenv("CUOTA_ESTRELLA", "1.30")
CUOTA_NORMAL = os.getenv("CUOTA_NORMAL", "1.55")
AVISAR_POTENCIAL = os.getenv("AVISAR_POTENCIAL", "0") == "1"


def is_night(hhmm):
    return bool(hhmm) and (hhmm >= "20:00" or hhmm < "06:00")


def rival_sets(g, rival):
    return sum((m.final[0] if m.a == rival else m.final[1])
               for m in g.matches if m.final and rival in (m.a, m.b))


def quality(r, g):
    """'estrella', 'normal' o 'noche' (solo para OFICIAL)."""
    if not r["warn"] and rival_sets(g, r["vs"]) <= 2:
        return "estrella"
    return "noche" if is_night(r["match"].time) else "normal"


def confirm_text(r, g, hh, early=False):
    m = r["match"]
    k = kind(r)
    cal = quality(r, g)
    when = f" · {m.time}" if m.time else ""
    if early:
        when += "\n🔴 YA EMPEZADO (1.er set): solo si aún puedes apostar en vivo"
    rival = "eliminado" if k == "OFICIAL" else "ya clasificado"
    if k == "OFICIAL":
        star = "⭐ " if cal == "estrella" else ""
        head = f"🟢🟢 {star}PICK OFICIAL: {r['pick'].upper()} 🟢🟢"
        cuota = CUOTA_ESTRELLA if cal == "estrella" else CUOTA_NORMAL
    else:
        head = f"🟡 PICK POTENCIAL: {r['pick'].upper()}"
        cuota = None
    hline = f"H2H {r['pick']} {hh[0]}-{hh[1]} {r['vs']}" if hh else "Sin H2H"
    txt = f"{head}\nvs {r['vs']} ({rival}){when}"
    if cuota:
        txt += f"\n💶 Apuesta solo con cuota ≥ {cuota}"
    txt += f"\n{hline}"
    if r["warn"]:
        txt += "\n⚠️ puede decidir por puntos"
    txt += f"\n🔎 Puntos: {table_line(g)} (compara con ttcup)"
    return txt


def table_line(g):
    """Puntos de la tabla tal como los calcula el bot (victoria 2, derrota 1)."""
    pts = {p: 0 for p in g.players}
    for m in g.matches:
        if m.final:
            w, l = (m.a, m.b) if m.final[0] > m.final[1] else (m.b, m.a)
            pts[w] += 2
            pts[l] += 1
    return " · ".join(f"{p.split()[0]} {v}" for p, v in
                      sorted(pts.items(), key=lambda x: -x[1]))


def register_pick(r, hh, early=False, cal="", enviado=True):
    m = r["match"]
    picks.append({"momento": "vivo" if early else "antes",
                  "calidad": cal, "enviado": "si" if enviado else "no",
                  "fecha": datetime.now(tt_feed.TZ).strftime("%Y-%m-%d"), "hora": m.time,
                  "tipo": kind(r), "pick": r["pick"], "rival": r["vs"],
                  "h2h_pick": hh[0] if hh else "", "h2h_rival": hh[1] if hh else "",
                  "aviso_puntos": "si" if r["warn"] else "no",
                  "match_id": m.mid, "resultado": "", "marcador": ""})
    save_picks()


def update_results(parsed):
    """Marca G/P en los picks cuyo partido ya ha terminado."""
    by_id = {p["id"]: p for p in parsed}
    changed = False
    for pk in picks:
        if pk.get("resultado"):
            continue
        ev = by_id.get(pk["match_id"])
        if not ev:
            continue
        if ev["final"]:
            sa, sb = ev["final"]
            pick_home = pk["pick"] == ev["a"]
            won = (sa > sb) if pick_home else (sb > sa)
            pk["resultado"] = "G" if won else "P"
            pk["marcador"] = f"{sa}-{sb}" if pick_home else f"{sb}-{sa}"
            changed = True
        elif ev["status"] not in ("notstarted", "inprogress"):
            pk["resultado"] = "N"          # cancelado / no jugado
            changed = True
    if changed:
        save_picks()


def _stats(rows):
    g = sum(1 for p in rows if p.get("resultado") == "G")
    l = sum(1 for p in rows if p.get("resultado") == "P")
    n = g + l
    pct = (100 * g / n) if n else 0
    return g, l, n, pct


def _line(label, rows):
    g, l, n, pct = _stats(rows)
    if not n:
        return f"{label}: sin picks resueltos"
    be = f" · cuota mínima rentable {100 / pct:.2f}" if pct else ""
    return f"{label}: {n} picks · {g}✅ {l}❌ ({pct:.0f}%){be}"


def summary_text(today):
    env = [p for p in picks if p.get("enviado") != "no"]          # los que te llegaron
    calla = [p for p in picks if p.get("enviado") == "no"]        # guardados sin avisar
    hoy = [p for p in env if p.get("fecha") == today]
    lines = [f"📊 RESUMEN {today[8:10]}/{today[5:7]}"]
    lines.append(_line("Hoy (avisados)", hoy))
    lines.append("")
    lines.append(_line("TOTAL avisados", env))
    lines.append(_line("  🟢 OFICIAL", [p for p in env if p.get("tipo") == "OFICIAL"]))
    lines.append(_line("  ⭐ ESTRELLA", [p for p in env if p.get("calidad") == "estrella"]))
    lines.append(_line("  🟢 normal", [p for p in env if p.get("calidad") == "normal"]))
    fav = [p for p in env if p.get("h2h_pick") not in ("", None)
           and int(p["h2h_pick"]) > int(p["h2h_rival"])]
    contra = [p for p in env if p.get("h2h_pick") not in ("", None)
              and int(p["h2h_pick"]) < int(p["h2h_rival"])]
    lines.append(_line("  H2H a favor", fav))
    lines.append(_line("  H2H en contra", contra))
    lines.append("")
    lines.append("Sin avisar (solo seguimiento):")
    lines.append(_line("  🟡 POTENCIAL", [p for p in calla if p.get("tipo") == "POTENCIAL"]))
    lines.append(_line("  🌙 oficial noche", [p for p in calla if p.get("calidad") == "noche"]))
    pend = sum(1 for p in env if not p.get("resultado"))
    if pend:
        lines.append(f"⏳ Pendientes de resultado: {pend}")
    return "\n".join(lines)


def maybe_daily_summary():
    now = datetime.now(tt_feed.TZ)
    today = now.strftime("%Y-%m-%d")
    if now.strftime("%H:%M") >= SUMMARY_HOUR and state.get("last_summary") != today:
        state["last_summary"] = today
        send(summary_text(today))


def heads_up_text(r, pm, w):
    m = r["match"]
    k = kind(r)
    sa, sb = pm.live
    cond = f"Si gana {w}"
    if r["partial"]:
        # marcador exacto, contado desde el ganador: "SOLO si gana 3-2"
        res = []
        for s in r["partial"].split(", "):
            x, y = s.split(":")
            res.append(f"{y}-{x}" if w == pm.b else f"{x}-{y}")
        cond = f"SOLO si gana {w} {' o '.join(res)}"
    return (f"⏳ ATENTO: {pm.a} {sa}-{sb} {pm.b} (3.er set o más)\n"
            f"{cond} → posible PICK {k} {r['pick'].upper()} vs {r['vs']}"
            + (f" · {m.time}" if m.time else ""))


def worth_heads_up(r, g):
    """Solo se anticipan los OFICIALES que pueden acabar avisandose."""
    if kind(r) != "OFICIAL":
        return AVISAR_POTENCIAL
    night = is_night(r["match"].time)
    # de noche solo valen los estrella; si ya no puede serlo, no se avisa
    if night and (r["warn"] or rival_sets(g, r["vs"]) > 2):
        return False
    return True


def deciding_set(pm, w, partial):
    """Solo para picks que dependen del marcador exacto: True si el set que se
    esta jugando decide (ganandolo w, el marcador final es el que hace falta)."""
    if not partial or not pm.live:
        return False
    sa, sb = pm.live
    after = (sa + 1, sb) if w == pm.a else (sa, sb + 1)
    if max(after) != 3:
        return False
    return f"{after[0]}:{after[1]}" in partial.split(", ")


def last_set_text(r, pm, w):
    m = r["match"]
    sa, sb = pm.live
    return (f"🔔 SET DECISIVO: {pm.a} {sa}-{sb} {pm.b}\n"
            f"Si {w} gana ESTE set → PICK {kind(r)} {r['pick'].upper()} vs {r['vs']}"
            + (f" · {m.time}" if m.time else "") + "\nPrepara la apuesta")


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

    update_results(parsed)
    maybe_daily_summary()

    sent = set(state["sent"])

    def once(key, text_fn):
        if key in sent:
            return
        sent.add(key)
        state["sent"].append(key)
        send(text_fn())

    valid = set()        # (match_id, pick) que siguen siendo pick ahora mismo
    checked = set()      # partidos de grupos analizados con datos fiables
    for g in groups:
        if not g.pending():
            continue
        if getattr(g, "dudoso", False):
            # algun resultado del grupo no cuadra en la API: no se avisa de nada
            print("Grupo con datos dudosos, se omite:", g.players)
            continue
        checked |= {m.mid for m in g.matches}
        results = g.analyze()
        for r in results:
            if not r["conds"]:
                valid.add((r["match"].mid, r["pick"]))
        for r in results:
            m = r["match"]
            if m.final:
                continue
            # regla: picks antes de empezar. Excepcion: si el partido acaba de
            # empezar (1.er set, 0-0 en sets y pocos puntos) se manda igual,
            # marcado como YA EMPEZADO, por si aun se puede apostar en vivo.
            early = m.live == (0, 0) and m.live_pts <= LIVE_PTS_MAX
            if m.live is not None and not early:
                continue
            if not r["conds"]:
                # pick seguro -> confirmacion (o solo registro si no se avisa)
                key = f"C|{m.mid}|{r['pick']}"
                if key not in sent:
                    k = kind(r)
                    cal = quality(r, g) if k == "OFICIAL" else ""
                    avisar = (k == "OFICIAL" and cal != "noche") or \
                             (k == "POTENCIAL" and AVISAR_POTENCIAL)
                    if avisar:
                        hh = h2h_for(m, r["pick"])
                        once(key, lambda r=r, g=g, hh=hh, e=early: confirm_text(r, g, hh, e))
                    else:
                        hh = None
                        sent.add(key)
                        state["sent"].append(key)
                    register_pick(r, hh, early, cal, avisar)
            elif len(r["conds"]) == 1 and m.live is None:
                if not worth_heads_up(r, g):
                    continue
                pm, w = r["conds"][0]
                if in_late_stage(pm):
                    once(f"H|{m.mid}|{r['pick']}|{pm.mid}|{w}|{r['partial']}",
                         lambda r=r, pm=pm, w=w: heads_up_text(r, pm, w))
                if deciding_set(pm, w, r["partial"]):
                    once(f"S|{m.mid}|{r['pick']}|{pm.mid}|{w}",
                         lambda r=r, pm=pm, w=w: last_set_text(r, pm, w))

    # anular picks ya enviados si, antes de empezar, los datos han cambiado
    status = {p["id"]: p["status"] for p in parsed}
    changed = False
    for pk in picks:
        if pk.get("resultado") or status.get(pk["match_id"]) != "notstarted":
            continue
        dud = pk["match_id"] not in checked and any(
            pk["match_id"] in {m.mid for m in g.matches} and getattr(g, "dudoso", False)
            for g in groups)
        if (pk["match_id"] in checked and (pk["match_id"], pk["pick"]) not in valid) or dud:
            pk["resultado"] = "A"
            changed = True
            if pk.get("enviado") == "no":
                continue
            send(f"❌ PICK ANULADO: {pk['pick'].upper()} vs {pk['rival']} · {pk['hora']}\n"
                 f"Los datos del grupo han cambiado. NO apostar.")
    if changed:
        save_picks()


def main():
    load_state()
    load_picks()
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
    if "--resumen" in sys.argv:          # resumen a mano: python tt_alerts.py --resumen
        load_picks()
        send(summary_text(datetime.now(tt_feed.TZ).strftime("%Y-%m-%d")))
        sys.exit()
    main()
