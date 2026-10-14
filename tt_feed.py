"""
tt_feed.py - Lee TT Cup desde la API "SofaScore - Live API" de Zyla Labs
y reconstruye los grupos de 4 jugadores.

Las URLs de las funciones se descubren solas al arrancar (llms-full.txt de
Zyla con tu clave). Si fallara, se pueden fijar a mano con las variables
ZYLA_URL_LIVE, ZYLA_URL_DATE, ZYLA_URL_H2H (copiando la URL de ejemplo de
la documentacion; los valores de deporte/fecha/id se sustituyen solos).
"""
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from itertools import combinations

from tt_engine import Group, Match

KEY = os.getenv("ZYLA_API_KEY", "")
API_ID = os.getenv("ZYLA_API_ID", "12787")
TOURNAMENT = os.getenv("TT_TOURNAMENT", "TT Cup").lower()
SPORT = os.getenv("TT_SPORT_SLUG", "table-tennis")
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "2"))))
WINDOW_H = 3.5
UA = {"User-Agent": "Mozilla/5.0 tt-alerts"}

URLS = {"live": os.getenv("ZYLA_URL_LIVE", ""),
        "date": os.getenv("ZYLA_URL_DATE", ""),
        "h2h": os.getenv("ZYLA_URL_H2H", "")}
FOUND = []   # todas las URLs descubiertas (para el informe)

SPORT_WORDS = {"football", "soccer", "tennis", "basketball", "ice-hockey",
               "table-tennis", "volleyball", "handball", "baseball"}


# ---------------- descubrimiento de endpoints ----------------
def discover():
    """Descarga el manual de Zyla con tu clave y localiza las URLs."""
    if all(URLS.values()):
        return URLS
    url = f"https://mcp.zylalabs.com/llms-full.txt?apikey={urllib.parse.quote(KEY, safe='')}"
    txt = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read().decode("utf-8", "ignore")
    pat = re.compile(rf"https://zylalabs\.com/api/{API_ID}/[^\s'\"<>`)\]]+")
    urls = sorted(set(pat.findall(txt)))
    FOUND[:] = urls
    for u in urls:
        p = urllib.parse.unquote_plus(u.split("?")[0].lower())
        if not URLS["live"] and "live" in p and ("match" in p or "event" in p) \
                and "odds" not in p and "tournament" not in p:
            URLS["live"] = u
        elif not URLS["date"] and "date" in p and ("match" in p or "event" in p) \
                and "odds" not in p and "tournament" not in p:
            URLS["date"] = u
        elif not URLS["h2h"] and "h2h" in p and "streak" not in p:
            URLS["h2h"] = u
    return URLS


def _fill(template, event_id=None):
    """Sustituye en la URL de ejemplo: deporte -> table-tennis, fecha -> hoy, id -> event_id."""
    base, _, q = template.partition("?")
    params = urllib.parse.parse_qsl(q, keep_blank_values=True)
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    out = []
    for k, v in params:
        kl, vl = k.lower(), v.lower()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) or "date" in kl:
            v = today
        elif vl in SPORT_WORDS or "sport" in kl:
            v = SPORT
        elif event_id and ("id" in kl or v.isdigit()):
            v = str(event_id)
        out.append((k, v))
    return base + ("?" + urllib.parse.urlencode(out) if out else "")


def _call(url):
    req = urllib.request.Request(url, headers={**UA, "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _events(obj):
    """Encuentra la lista de partidos (dicts con homeTeam) dentro de cualquier JSON."""
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and "homeTeam" in obj[0]:
            return obj
        for x in obj:
            r = _events(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _events(v)
            if r:
                return r
    return []


def _is_tt_cup(e):
    names = " ".join(str((e.get(k) or {}).get("name", "")) for k in ("tournament", "uniqueTournament"))
    return TOURNAMENT in names.lower()


def fetch():
    """Devuelve (eventos TT Cup del dia, eventos tenis de mesa en vivo totales)."""
    discover()
    evs, live_total = {}, 0
    if URLS["date"]:
        for e in _events(_call(_fill(URLS["date"]))):
            if _is_tt_cup(e):
                evs[e.get("id")] = e
    if URLS["live"]:
        live = _events(_call(_fill(URLS["live"])))
        live_total = len(live)
        for e in live:
            if _is_tt_cup(e):
                evs[e.get("id")] = e   # el vivo pisa al de la fecha (mas fresco)
    return list(evs.values()), live_total


def h2h(event_id):
    """Devuelve 'H2H a-b' o '' si no hay datos."""
    if not URLS["h2h"]:
        return ""
    try:
        d = _call(_fill(URLS["h2h"], event_id))
        duel = d.get("teamDuel") if isinstance(d, dict) else None
        if not duel:
            def find(o):
                if isinstance(o, dict):
                    if "homeWins" in o:
                        return o
                    for v in o.values():
                        r = find(v)
                        if r: return r
                elif isinstance(o, list):
                    for v in o:
                        r = find(v)
                        if r: return r
            duel = find(d)
        if duel:
            return f"H2H {duel.get('homeWins', 0)}-{duel.get('awayWins', 0)}"
    except Exception as ex:
        print("WARN h2h", ex)
    return ""


# ---------------- de evento Sofascore a partido ----------------
def parse(e):
    st = (e.get("status") or {}).get("type", "")
    hs, as_ = e.get("homeScore") or {}, e.get("awayScore") or {}
    sa, sb = int(hs.get("current", 0) or 0), int(as_.get("current", 0) or 0)
    pa = sum(int(hs.get(f"period{i}", 0) or 0) for i in range(1, 8))
    pb = sum(int(as_.get(f"period{i}", 0) or 0) for i in range(1, 8))
    final = live = points = None
    if st == "finished" and max(sa, sb) == 3:
        final, points = (sa, sb), ((pa, pb) if pa + pb else None)
    elif st == "inprogress":
        live = (sa, sb)
    elif st not in ("notstarted", ""):
        return None   # cancelado, aplazado...
    return dict(id=str(e.get("id")), a=(e.get("homeTeam") or {}).get("name", "?"),
                b=(e.get("awayTeam") or {}).get("name", "?"),
                t=int(e.get("startTimestamp", 0) or 0),
                final=final, live=live, points=points,
                score=f"{sa}-{sb}", status=st)


def build_groups(events):
    parsed = [p for p in (parse(e) for e in events) if p]
    parsed.sort(key=lambda x: x["t"])
    pair = {}
    for m in parsed:
        pair.setdefault(frozenset((m["a"], m["b"])), []).append(m)
    groups, seen = [], set()
    W = WINDOW_H * 3600
    for m in parsed:
        a, b, t = m["a"], m["b"], m["t"]
        near = set()
        for x in parsed:
            if abs(x["t"] - t) <= W:
                near |= {x["a"], x["b"]}
        near -= {a, b}
        for c, d in combinations(sorted(near), 2):
            four, ms = (a, b, c, d), []
            for p, q in combinations(four, 2):
                cand = [x for x in pair.get(frozenset((p, q)), []) if abs(x["t"] - t) <= W]
                if not cand:
                    break
                ms.append(min(cand, key=lambda x: abs(x["t"] - t)))
            if len(ms) != 6:
                continue
            ts = [x["t"] for x in ms]
            if max(ts) - min(ts) > W:
                continue
            key = frozenset(x["id"] for x in ms)
            if key in seen:
                continue
            seen.add(key)
            ms.sort(key=lambda x: x["t"])
            matches = [Match(x["a"], x["b"], i + 1,
                             time=datetime.fromtimestamp(x["t"], TZ).strftime("%H:%M"),
                             final=x["final"], live=x["live"], points=x["points"], mid=x["id"])
                       for i, x in enumerate(ms)]
            groups.append(Group(list(four), matches))
    return groups, parsed
