"""
tt_feed.py - Lee TT Cup desde "SofaScore - Live API" (Zyla Labs) usando la
funcion "partidos programados de un torneo unico" (TT Cup = 15005) y
reconstruye los grupos de 4 jugadores.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from itertools import combinations

from tt_engine import Group, Match

KEY = os.getenv("ZYLA_API_KEY", "")
TOURNAMENT_ID = os.getenv("TT_TOURNAMENT_ID", "15005")          # TT Cup
URL_TOURNAMENT = os.getenv(
    "ZYLA_URL_TOURNAMENT",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/31095/get+scheduled+matches+from+unique+tournament")
URL_H2H = os.getenv("ZYLA_URL_H2H", "")        # se rellena cuando tengamos la direccion
H2H_PARAM = os.getenv("ZYLA_H2H_PARAM", "match_id")
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "2"))))
WINDOW_H = 3.5
UA = {"User-Agent": "Mozilla/5.0 tt-alerts"}

# compatibilidad con tt_alerts.py
URLS = {"live": URL_TOURNAMENT, "date": URL_TOURNAMENT, "h2h": URL_H2H}
FOUND = []
API_ID = "12787"


def discover():
    return URLS


def _call(url, **params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={**UA, "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"Zyla: {data.get('message', '')[:150]}")
    return data


def _events(obj):
    """Encuentra la lista de partidos (dicts con homeTeam) en cualquier JSON."""
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


def fetch():
    """Partidos de TT Cup de hoy (y de ayer si es de madrugada)."""
    now = datetime.now(TZ)
    days = [now.strftime("%Y-%m-%d")]
    if now.hour < 4:
        days.insert(0, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    evs = {}
    for d in days:
        for e in _events(_call(URL_TOURNAMENT, unique_tournament_id=TOURNAMENT_ID, date=d)):
            evs[e.get("id")] = e
    live = sum(1 for e in evs.values() if (e.get("status") or {}).get("type") == "inprogress")
    return list(evs.values()), live


def h2h(event_id):
    """'H2H a-b' o '' si no hay datos o aun no tenemos la direccion."""
    if not URL_H2H:
        return ""
    try:
        d = _call(URL_H2H, **{H2H_PARAM: event_id})

        def find(o):
            if isinstance(o, dict):
                if "homeWins" in o:
                    return o
                for v in o.values():
                    r = find(v)
                    if r:
                        return r
            elif isinstance(o, list):
                for v in o:
                    r = find(v)
                    if r:
                        return r
        duel = find(d)
        if duel:
            return f"H2H {duel.get('homeWins', 0)}-{duel.get('awayWins', 0)}"
    except Exception as ex:
        print("WARN h2h", ex)
    return ""


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse(e):
    st = (e.get("status") or {}).get("type", "")
    hs, as_ = e.get("homeScore") or {}, e.get("awayScore") or {}
    sa = _int(hs.get("current", hs.get("display")))
    sb = _int(as_.get("current", as_.get("display")))
    pa = sum(_int(hs.get(f"period{i}")) for i in range(1, 8))
    pb = sum(_int(as_.get(f"period{i}")) for i in range(1, 8))
    final = live = points = None
    if st == "finished" and max(sa, sb) == 3:
        final, points = (sa, sb), ((pa, pb) if pa + pb else None)
    elif st == "inprogress":
        live = (sa, sb)
    elif st != "notstarted":
        return None      # cancelado, aplazado, etc.
    t = _int(e.get("startTimestamp") or e.get("timestamp"))
    return dict(id=str(e.get("id")),
                a=(e.get("homeTeam") or {}).get("name", "?"),
                b=(e.get("awayTeam") or {}).get("name", "?"),
                t=t, final=final, live=live, points=points,
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
