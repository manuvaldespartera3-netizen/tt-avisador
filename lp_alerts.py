"""
lp_alerts.py - COPIA del avisador para Czech Liga Pro en MODO SEGUIMIENTO.

Es independiente del bot de TT Cup (archivos, memoria, tarea y datos aparte):
  - avisa de los picks igual que TT Cup, marcados 'no apostar'
  - no lee tus mensajes de Telegram (tus cuotas y /pick siguen yendo a TT Cup)
  - apunta sola la cuota real en el momento del pick y al acabar el partido
  - manda un resumen al dia a las 23:50
"""
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
from datetime import datetime, timedelta
import sys
import time
import traceback
import urllib.parse
import urllib.request

import lp_feed as tt_feed
from lp_feed import fetch, build_groups, h2h

TG = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()
POLL = int(os.getenv("POLL_SECONDS", "60"))     # ritmo normal
POLL_FAST = int(os.getenv("POLL_FAST", "15"))    # ritmo rapido cuando un pick esta a punto
RUN = int(os.getenv("RUN_SECONDS", "320"))      # algo mas de 5 min: la siguiente ejecucion
                                                # entra justo al acabar (sin huecos)
HOT = {"on": False, "ids": set()}   # partidos decisivos en juego
FRESH = {}      # datos recientes de esos partidos (leidos uno a uno, mas rapidos)
POLL_EVENT = int(os.getenv("POLL_EVENT", "8"))   # segundos entre lecturas del partido decisivo
TEST = os.getenv("TEST_MODE", "1") == "1"
REPORT_EVERY = 15 * 60
STATE_FILE = os.getenv("STATE_FILE", "state.json")
VERSION = "lp3"   # al cambiarlo se reenvia el mensaje de arranque
PICKS_FILE = os.getenv("PICKS_FILE", "picks.csv")
SUMMARY_HOUR = os.getenv("SUMMARY_AT", "23:45")   # hora del resumen diario
SEGUIMIENTO = os.getenv("SEGUIMIENTO", "1") == "1"
LIGA_TAG = os.getenv("LIGA_TAG", "🇨🇿 LIGA PRO")
CUOTA_LIGA = float(os.getenv("CUOTA_LIGA", "1.60"))
URL_ODDS = os.getenv(
    "ZYLA_URL_ODDS",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/25097/get+match+odds")
FIELDS = ["cuota_aviso", "cuota_cierre", "fecha", "hora", "tipo", "pick", "rival", "h2h_pick", "h2h_rival",
          "aviso_puntos", "match_id", "resultado", "marcador", "momento",
          "calidad", "enviado", "cuota", "apuesta", "msg_id"]
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


def send(text, reply_to=None):
    """Envia un mensaje y devuelve su id (para poder responderle luego)."""
    print(text, flush=True)
    if not (TG and CHAT):
        return None
    params = {"chat_id": CHAT, "text": text[:4000]}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{TG}/sendMessage",
                                    data, timeout=15) as resp:
            return json.loads(resp.read().decode()).get("result", {}).get("message_id")
    except Exception as ex:
        print("WARN telegram", ex)
    return None


_send_real = send
if SEGUIMIENTO:
    def send(text, reply_to=None):
        """En seguimiento SI avisa (para ver como y cuando), pero cada mensaje
        va marcado como Liga Pro y como 'no apostar'."""
        return _send_real(f"{LIGA_TAG} · SEGUIMIENTO (no apostar)\n{text}")


def odds_now(mid, pick_home):
    """Cuota del pick ahora mismo segun Sofascore (o '' si no hay)."""
    try:
        data = tt_feed._call(URL_ODDS, match_id=mid)
    except Exception as ex:
        print("WARN odds", ex)
        return ""
    def markets(o):
        if isinstance(o, dict):
            if isinstance(o.get("markets"), list):
                return o["markets"]
            for v in o.values():
                r = markets(v)
                if r:
                    return r
        elif isinstance(o, list):
            if o and isinstance(o[0], dict) and "choices" in o[0]:
                return o
            for v in o:
                r = markets(v)
                if r:
                    return r
        return []
    for m in markets(data):
        ch = {str(c.get("name")): c for c in m.get("choices", [])}
        if set(ch) == {"1", "2"}:
            c = ch["1" if pick_home else "2"]
            v = c.get("value") or {}
            dec = v.get("decimal") if isinstance(v, dict) else None
            if dec:
                return f"{float(dec):.2f}"
            fr = c.get("fractionalValue") or (v.get("fractional") if isinstance(v, dict) else None)
            if fr and "/" in str(fr):
                a, b = str(fr).split("/")
                return f"{1 + float(a) / float(b):.2f}"
    return ""


# ------------------------------------------------ cuotas que apuntas tu
APUESTA_DEFECTO = float(os.getenv("APUESTA_DEFECTO", "120"))


def _num(t):
    try:
        return float(t.replace(",", ".").replace("€", ""))
    except ValueError:
        return None


def manual_pick(text, parsed, reply_to):
    """'/pick Apellido 1.88 [120]' -> registra un pick que has apostado tu.
    Busca el partido de hoy de ese jugador (el que no ha terminado, o el
    ultimo que ha terminado) y el bot apunta solo si gana o pierde."""
    words = text.split()[1:]
    nums = [w for w in words if _num(w) is not None]
    name = " ".join(w for w in words if _num(w) is None).strip().lower()
    if not name or not nums or not (1.01 <= _num(nums[0]) <= 50):
        send("❓ Escríbelo así: /pick Apellido 1.88   (o /pick Apellido 1.88 120)", reply_to)
        return False
    cuota = _num(nums[0])
    apuesta = _num(nums[1]) if len(nums) > 1 else APUESTA_DEFECTO
    cands = [p for p in parsed if name in p["a"].lower() or name in p["b"].lower()]
    if not cands:
        send(f"❓ No encuentro ningún partido de hoy de '{name}'. Revisa el apellido.", reply_to)
        return False
    vivos = [p for p in cands if not p["final"]]
    if vivos:
        m = min(vivos, key=lambda p: p["t"])           # el proximo o el que se juega
    else:
        m = max(cands, key=lambda p: p["t"])           # el ultimo terminado
    pick = m["a"] if name in m["a"].lower() else m["b"]
    rival = m["b"] if pick == m["a"] else m["a"]
    if any(p.get("match_id") == m["id"] and p.get("pick") == pick for p in picks):
        # ya estaba registrado (por ejemplo, el bot lo mando): solo se pone la cuota
        pk = next(p for p in picks if p.get("match_id") == m["id"] and p.get("pick") == pick)
        pk["cuota"], pk["apuesta"], pk["enviado"] = f"{cuota:.2f}", f"{apuesta:.0f}", "si"
    else:
        picks.append({"fecha": datetime.now(tt_feed.TZ).strftime("%Y-%m-%d"),
                      "hora": datetime.fromtimestamp(m["t"], tt_feed.TZ).strftime("%H:%M"),
                      "tipo": "OFICIAL", "pick": pick, "rival": rival,
                      "h2h_pick": "", "h2h_rival": "", "aviso_puntos": "",
                      "match_id": m["id"], "resultado": "", "marcador": "",
                      "momento": "manual", "calidad": "manual", "enviado": "si",
                      "cuota": f"{cuota:.2f}", "apuesta": f"{apuesta:.0f}", "msg_id": ""})
    send(f"✅ Registrado a mano: {pick} vs {rival} · "
         f"{datetime.fromtimestamp(m['t'], tt_feed.TZ).strftime('%H:%M')} @ {cuota:.2f} · "
         f"{apuesta:.0f} €\nEl resultado se apuntará solo.", reply_to)
    return True


def read_replies(parsed=()):
    if SEGUIMIENTO:
        return          # no toca tus mensajes: son del bot de TT Cup
    """Lee lo que escribes al bot:
       - respondiendo a un pick: '1.45'  o  '1.45 120'  o  'no'
       - sin responder: se aplica al ultimo pick avisado que no tenga cuota
       - '/pick Apellido 1.88': registra a mano un pick que has apostado tu
       - '/resumen': manda el resumen al momento"""
    if not (TG and CHAT):
        return
    url = (f"https://api.telegram.org/bot{TG}/getUpdates?timeout=0"
           f"&offset={state.get('tg_offset', 0)}")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            ups = json.loads(resp.read().decode()).get("result", [])
    except Exception as ex:
        print("WARN getUpdates", ex)
        return
    changed = False
    for u in ups:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT:
            continue
        text = (msg.get("text") or "").strip().lower()
        if not text:
            continue
        if text.startswith("/resumen"):
            send(summary_text(datetime.now(tt_feed.TZ).strftime("%Y-%m-%d")))
            continue
        if text.startswith("/pick"):
            if manual_pick(text, parsed, msg.get("message_id")):
                changed = True
            continue
        target = None
        rep = (msg.get("reply_to_message") or {}).get("message_id")
        if rep:
            target = next((p for p in picks if str(p.get("msg_id")) == str(rep)), None)
        if target is None:
            cand = [p for p in picks if p.get("enviado") != "no" and p.get("msg_id")
                    and not p.get("cuota")]
            target = cand[-1] if cand else None
        if target is None:
            send("❓ No sé a qué pick se refiere. Responde (deslizando) al mensaje del pick.",
                 msg.get("message_id"))
            continue
        if text in ("no", "no apostado", "x", "-"):
            target["cuota"], target["apuesta"] = "no", ""
            send(f"👍 Apuntado: {target['pick']} NO apostado", msg.get("message_id"))
            changed = True
            continue
        nums = [n for n in (_num(t) for t in text.split()) if n is not None]
        if not nums or not (1.01 <= nums[0] <= 50):
            send("❓ Escribe la cuota así: 1.45   (o 1.45 120 si cambias la cantidad)",
                 msg.get("message_id"))
            continue
        cuota = nums[0]
        apuesta = nums[1] if len(nums) > 1 else APUESTA_DEFECTO
        target["cuota"], target["apuesta"] = f"{cuota:.2f}", f"{apuesta:.0f}"
        send(f"✅ Apuntado: {target['pick']} @ {cuota:.2f} · {apuesta:.0f} €", msg.get("message_id"))
        changed = True
    if changed:
        save_picks()


def euros(rows):
    """(apostado, beneficio, n) de los picks con cuota apuntada y resultado."""
    ap = ben = 0.0
    n = 0
    for p in rows:
        c, a, res = p.get("cuota"), p.get("apuesta"), p.get("resultado")
        if not c or c == "no" or not a or res not in ("G", "P"):
            continue
        c, a = float(c), float(a)
        ap += a
        ben += a * (c - 1) if res == "G" else -a
        n += 1
    return ap, ben, n


def euro_line(label, rows):
    ap, ben, n = euros(rows)
    if not n:
        return f"{label}: sin cuotas apuntadas"
    return (f"{label}: {n} apuestas · {ap:.0f} € apostados · "
            f"{'+' if ben >= 0 else ''}{ben:.0f} € ({100 * ben / ap:+.1f}%)")


def startup_report():
    if SEGUIMIENTO:
        _send_real(f"{LIGA_TAG} · modo seguimiento activo\n"
                   f"Te avisará de los picks igual que TT Cup, marcados 'no apostar',\n"
                   f"y apunta sola la cuota real. Resumen diario a las 23:50.\n"
                   f"🚫 No respondas a sus mensajes. El bot de TT Cup sigue igual.")
        return
    lines = ["🟢 Avisador TT Cup activo (v14)" + (" (MODO PRUEBA)" if TEST else ""),
             "Solo OFICIALES · ⭐ = alta calidad · cada pick trae su cuota mínima",
             "✍️ Responde a cada pick con tu cuota (ej. 1.45) · /resumen = resumen al momento",
             "✍️ /pick Apellido 1.88 = registrar a mano un pick que apostaste"]
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
    if SEGUIMIENTO:
        return "normal"      # en Liga Pro los filtros de TT Cup no sirven: todos iguales
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
    txt += f"\n🔎 Puntos: {table_line(g)} (compara con la web de la liga)"
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


def register_pick(r, hh, early=False, cal="", enviado=True, msg_id=None):
    m = r["match"]
    cuota = odds_now(m.mid, r["pick"] == m.a) if SEGUIMIENTO else ""
    picks.append({"cuota_aviso": cuota,"momento": "vivo" if early else "antes", "msg_id": msg_id or "",
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
            if SEGUIMIENTO:
                pk["cuota_cierre"] = odds_now(pk["match_id"], pick_home)
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
    # con pocos picks la cifra no significa nada: no se muestra hasta tener 30
    if n >= 30:
        be = f" · cuota mínima rentable {100 / pct:.2f}" if pct else ""
    else:
        be = " · pocos picks todavía"
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
    lines.append("")
    lines.append("💶 TU DINERO (cuotas que has apuntado)")
    lines.append(euro_line("  Hoy", hoy))
    lines.append(euro_line("  Total", env))
    lines.append(euro_line("  ⭐ estrella", [p for p in env if p.get("calidad") == "estrella"]))
    lines.append(euro_line("  🟢 normal", [p for p in env if p.get("calidad") == "normal"]))
    sin = sum(1 for p in env if p.get("msg_id") and not p.get("cuota"))
    if sin:
        lines.append(f"  ✍️ Picks sin cuota apuntada: {sin}")
    pend = sum(1 for p in env if not p.get("resultado"))
    if pend:
        lines.append(f"⏳ Pendientes de resultado: {pend}")
    return "\n".join(lines)


def liga_summary(today):
    of = [p for p in picks if p.get("tipo") == "OFICIAL" and p.get("resultado") in ("G", "P")]
    hoy = [p for p in of if p.get("fecha") == today]
    L = [f"{LIGA_TAG} · SEGUIMIENTO {today[8:10]}/{today[5:7]} (no se ha apostado)"]
    for lab, rows in (("Hoy", hoy), ("Total", of)):
        if not rows:
            L.append(f"{lab}: sin picks oficiales resueltos")
            continue
        g = sum(p["resultado"] == "G" for p in rows)
        L.append(f"{lab}: {len(rows)} picks · {g}✅ {len(rows) - g}❌ ({100 * g / len(rows):.0f}%)")
    con = [p for p in of if p.get("cuota_aviso")]
    if con:
        media = sum(float(p["cuota_aviso"]) for p in con) / len(con)
        altas = [p for p in con if float(p["cuota_aviso"]) >= CUOTA_LIGA]
        ben = sum((float(p["cuota_aviso"]) - 1) if p["resultado"] == "G" else -1 for p in altas)
        L.append(f"💶 Cuota media al avisar: {media:.2f} ({len(con)} con cuota)")
        L.append(f"   Con cuota ≥ {CUOTA_LIGA:.2f}: {len(altas)} picks → "
                 f"{ben:+.1f} u" + (f" ({100 * ben / len(altas):+.0f}%)" if altas else ""))
        mov = [p for p in con if p.get("cuota_cierre")]
        if mov:
            d = sum(float(p["cuota_cierre"]) - float(p["cuota_aviso"]) for p in mov) / len(mov)
            L.append(f"   La cuota se movió de media {d:+.2f} hasta el inicio")
    return "\n".join(L)


def maybe_daily_summary():
    if SEGUIMIENTO:
        now = datetime.now(tt_feed.TZ)
        today = now.strftime("%Y-%m-%d")
        ayer = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if now.strftime("%H:%M") >= "23:40" and state.get("last_summary") != today:
            state["last_summary"] = today
            _send_real(liga_summary(today))
        elif now.hour < 12 and state.get("last_summary") not in (ayer, today):
            # el bot no llego a ejecutarse a tiempo anoche: se manda el de ayer
            state["last_summary"] = ayer
            _send_real(liga_summary(ayer))
        return
    now = datetime.now(tt_feed.TZ)
    today = now.strftime("%Y-%m-%d")
    if now.strftime("%H:%M") >= SUMMARY_HOUR and state.get("last_summary") != today:
        state["last_summary"] = today
        send(summary_text(today))


def score_detail(r, g, pm, w):
    """Para el aviso: con que marcadores de 'pm' el pick queda seguro y con
    cuales dependeria de los puntos (o no habria pick)."""
    if g is None or not w:
        return ""
    seguro, puntos, nada = [], [], []
    for f in pm.possible_finals():
        if (f[0] > f[1]) != (w == pm.a):
            continue
        r2 = g._eval_match(r["match"], {pm.order: [f]})
        txt = f"{f[0]}-{f[1]}" if w == pm.a else f"{f[1]}-{f[0]}"
        if not r2 or r2["pick"] != r["pick"]:
            nada.append(txt)
        elif r2["warn"]:
            puntos.append(txt)
        else:
            seguro.append(txt)
    if not (puntos or nada):
        return ""
    out = []
    if seguro:
        out.append(f"✅ {w} gana {' o '.join(seguro)} → pick seguro")
    if puntos:
        out.append(f"⚠️ {w} gana {' o '.join(puntos)} → decide por puntos (dudoso)")
    if nada:
        out.append(f"❌ {w} gana {' o '.join(nada)} → no hay pick seguro (puede decidir por puntos)")
    return "\n" + "\n".join(out)


def heads_up_text(r, pm, w, g=None):
    m = r["match"]
    k = kind(r)
    sa, sb = pm.live
    cond = f"Si gana {w}" if w else "Gane quien gane"
    if r["partial"] and w:
        # marcador exacto, contado desde el ganador: "SOLO si gana 3-2"
        res = []
        for s in r["partial"].split(", "):
            x, y = s.split(":")
            res.append(f"{y}-{x}" if w == pm.b else f"{x}-{y}")
        cond = f"SOLO si gana {w} {' o '.join(res)}"
    return (f"⏳ ATENTO: {pm.a} {sa}-{sb} {pm.b} (3.er set o más)\n"
            f"{cond} → posible PICK {k} {r['pick'].upper()} vs {r['vs']}"
            + (f" · {m.time}" if m.time else "")
            + ("" if r["partial"] else score_detail(r, g, pm, w)))


def worth_heads_up(r, g):
    """Solo se anticipan los OFICIALES que pueden acabar avisandose."""
    if kind(r) != "OFICIAL":
        return AVISAR_POTENCIAL
    if SEGUIMIENTO:
        return True
    night = is_night(r["match"].time)
    # de noche solo valen los estrella; si ya no puede serlo, no se avisa
    if night and (r["warn"] or rival_sets(g, r["vs"]) > 2):
        return False
    return True


def outcome(r, g, pm, f):
    """Que pasa con el pick si el partido decisivo 'pm' acaba exactamente 'f'.
    Devuelve (simbolo, texto corto)."""
    m = r["match"]
    r2 = g._eval_match(m, {pm.order: [f]})
    if not r2 or r2["pick"] != r["pick"]:
        return "❌", "no hay pick seguro"
    if r2["warn"]:
        return "❌", "lo deciden los puntos"
    if kind(r2) != "OFICIAL":
        return "❌", "solo potencial"
    rs = rival_sets(g, r2["vs"]) + ((f[0] if pm.a == r2["vs"] else f[1])
                                     if r2["vs"] in (pm.a, pm.b) else 0)
    star = rs <= 2 and not SEGUIMIENTO      # en Liga Pro no hay ⭐ ni filtro de noche
    if not star and is_night(m.time) and not SEGUIMIENTO:
        return "❌", "oficial de noche sin ⭐"
    return "✅", ("⭐ " if star else "") + f"pick (cuota ≥ {CUOTA_ESTRELLA if star else CUOTA_NORMAL})"


def full_heads_up_text(r, pm, g):
    """ATENTO con cada resultado posible del partido decisivo revisado."""
    m = r["match"]
    sa, sb = pm.live
    lines = [f"⏳ ATENTO (aún NO apuestes): {pm.a} {sa}-{sb} {pm.b}",
             f"Posible PICK OFICIAL {r['pick'].upper()} vs {r['vs']} · {m.time}"]
    for p in (pm.a, pm.b):
        parts = []
        for f in pm.possible_finals():
            if (f[0] > f[1]) != (p == pm.a):
                continue
            sym, txt = outcome(r, g, pm, f)
            parts.append(f"{sym} {max(f)}-{min(f)} {txt}")
        if parts:
            lines.append(f"Si gana {p}:  " + "  ·  ".join(parts))
    lines.append("Espera al 🔔 SET DECISIVO: ahí te digo si apostar.")
    return "\n".join(lines)


def closing_set_text(r, pm, g):
    """Mensaje para el set que puede terminar el partido decisivo: para cada
    jugador que puede cerrarlo, si con ESE resultado hay pick seguro o no."""
    m = r["match"]
    sa, sb = pm.live
    lines = [f"🔔 SET DECISIVO: {pm.a} {sa}-{sb} {pm.b}"]
    for p in (pm.a, pm.b):
        mine = sa if p == pm.a else sb
        if mine != 2:
            nxt = (sa + 1, sb) if p == pm.a else (sa, sb + 1)
            lines.append(f"↔️ Si gana {p} este set → {nxt[0]}-{nxt[1]}, sigue el partido (te aviso)")
            continue
        f = (3, sb) if p == pm.a else (sa, 3)
        res = f"{max(f)}-{min(f)}"
        sym, txt = outcome(r, g, pm, f)
        if sym == "✅":
            lines.append(f"✅ Si gana {p} ({res}) → {txt.replace('pick', 'PICK OFICIAL ' + r['pick'].upper())}"
                         + (" · aquí sería el momento de apostar" if SEGUIMIENTO else " · apuesta YA"))
        else:
            lines.append(f"❌ Si gana {p} ({res}) → NO apuestes ({txt})")
    lines.append(f"Partido del pick: {m.time}")
    return "\n".join(lines)


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


def progress(e):
    """Cuanto ha avanzado un partido: estado, sets y puntos jugados."""
    if not e:
        return -1
    rank = {"notstarted": 0, "inprogress": 1, "finished": 2}.get((e.get("status") or {}).get("type"), 0)
    hs, as_ = e.get("homeScore") or {}, e.get("awayScore") or {}
    def n(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    sets = n(hs.get("current")) + n(as_.get("current"))
    pts = sum(n(hs.get(f"period{i}")) + n(as_.get(f"period{i}")) for i in range(1, 8))
    return rank * 100000 + sets * 1000 + pts


def tick():
    HOT["on"] = False
    HOT["ids"] = set()
    events, live_total = fetch()
    if FRESH:
        # lo leido partido a partido es mas reciente que la lista del torneo
        by = {str(e.get("id")): e for e in events}
        for mid, ev in FRESH.items():
            # solo se usa si va IGUAL o MAS ADELANTADO que la lista (nunca un dato viejo)
            if progress(ev) >= progress(by.get(mid)):
                by[mid] = ev
        events = list(by.values())
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
    read_replies(parsed)
    maybe_daily_summary()

    sent = set(state["sent"])

    def once(key, text_fn):
        if key in sent:
            return
        sent.add(key)
        state["sent"].append(key)
        send(text_fn())

    valid = set()        # (match_id, pick) que siguen siendo pick ahora mismo
    cands = set()        # "match|pick" que todavia pueden ser pick
    alerted = state.setdefault("alerted", {})   # avisos ATENTO pendientes de resolver
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
            cands.add(f"{r['match'].mid}|{r['pick']}")
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
                    sent.add(key)
                    state["sent"].append(key)
                    hh, mid_tg = None, None
                    if avisar:
                        hh = h2h_for(m, r["pick"])
                        extra = "\n✍️ Responde con tu cuota (ej. 1.45) o 'no'"
                        if SEGUIMIENTO:
                            c_now = odds_now(m.mid, r["pick"] == m.a)
                            extra = ((f"\n📈 Cuota ahora: {c_now}" if c_now else "\n📈 Sin cuota todavía")
                                     + "\n🚫 No respondas a este mensaje (es solo seguimiento)")
                        mid_tg = send(confirm_text(r, g, hh, early) + extra)
                    register_pick(r, hh, early, cal, avisar, mid_tg)
            elif len(r["conds"]) == 1 and m.live is None:
                if not worth_heads_up(r, g):
                    continue
                pm, w = r["conds"][0]
                if pm.live is not None:
                    HOT["on"] = True      # partido decisivo en juego: mirar cada 15 s
                    HOT["ids"].add(pm.mid)
                if in_late_stage(pm) and max(pm.live) < 2:
                    # Un solo aviso por partido decisivo, con TODOS los resultados
                    # posibles revisados uno a uno (nunca un "gane quien gane" a medias).
                    # Con 2 sets de alguno ya va el SET DECISIVO, que es aun mas exacto.
                    alerted[f"{m.mid}|{r['pick']}"] = {"pm": pm.mid, "a": pm.a, "b": pm.b,
                        "pick": r["pick"], "hora": m.time}
                    once(f"H|{m.mid}|{r['pick']}|{pm.mid}",
                         lambda r=r, pm=pm, g=g: full_heads_up_text(r, pm, g))
                if pm.live and max(pm.live) == 2:
                    # set que puede cerrar el partido: respuesta exacta por resultado
                    alerted[f"{m.mid}|{r['pick']}"] = {"pm": pm.mid, "a": pm.a, "b": pm.b,
                        "pick": r["pick"], "hora": m.time}
                    once(f"F|{m.mid}|{r['pick']}|{pm.mid}|{pm.live[0]}-{pm.live[1]}",
                         lambda r=r, pm=pm, g=g: closing_set_text(r, pm, g))

    # si se avisó "ATENTO" y ese pick ya no es posible, decirlo claro
    pstat = {p["id"]: p for p in parsed}
    for key, al in list(alerted.items()):
        if f"C|{key}" in sent or any(x.startswith(f"C|{key}") for x in sent):
            alerted.pop(key)                 # ya se confirmó el pick
            continue
        if key in cands:
            continue                         # todavía puede salir
        pmx = pstat.get(al["pm"])
        mid_pick = key.split("|")[0]
        if pmx and pstat.get(mid_pick, {}).get("status") == "notstarted":
            sc = f"{pmx['score']}" if pmx.get("score") else ""
            once(f"X|{key}", lambda al=al, sc=sc:
                 f"❌ OLVIDA EL AVISO: {al['a']} {sc} {al['b']}\n"
                 f"Ya no hay pick seguro para {al['pick'].upper()} ({al['hora']}). NO apuestes.")
        alerted.pop(key)

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
        wait = POLL_FAST if HOT["on"] else POLL
        if "--loop" not in sys.argv and time.time() + wait > end:
            break
        if HOT["ids"] and watch_hot(wait):
            continue                # el partido decisivo acaba de terminar: analizar YA
        time.sleep(wait)


def watch_hot(seconds):
    """Vigila los partidos decisivos uno a uno cada POLL_EVENT segundos.
    Devuelve True en cuanto alguno termina (para confirmar el pick al instante)."""
    stop = time.time() + seconds
    while time.time() < stop:
        for mid in list(HOT["ids"]):
            ev = tt_feed.event(mid)
            if ev is None:
                return False        # funcion no disponible: ritmo normal
            FRESH[str(mid)] = ev
            if (ev.get("status") or {}).get("type") == "finished":
                print("Partido decisivo terminado:", mid)
                return True
        time.sleep(POLL_EVENT)
    return False


if __name__ == "__main__":
    if "--reset" in sys.argv:
        state["started"] = False
    if "--resumen" in sys.argv:          # resumen a mano: python tt_alerts.py --resumen
        load_picks()
        send(summary_text(datetime.now(tt_feed.TZ).strftime("%Y-%m-%d")))
        sys.exit()
    main()
