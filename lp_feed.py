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

from lp_engine import Group, Match

KEY = os.getenv("ZYLA_API_KEY", "").strip()
TOURNAMENT_ID = os.getenv("TT_TOURNAMENT_ID", "15005")          # TT Cup
URL_TOURNAMENT = os.getenv(
    "ZYLA_URL_TOURNAMENT",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/31095/get+scheduled+matches+from+unique+tournament")
URL_H2H = os.getenv(
    "ZYLA_URL_H2H",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/29536/get+match+h2h")
H2H_PARAM = os.getenv("ZYLA_H2H_PARAM", "match_id")
# "Obten detalles del partido": datos de UN partido, mas frescos que la lista
# del torneo. Se usa para vigilar el partido decisivo cada pocos segundos.
URL_EVENT = os.getenv(
    "ZYLA_URL_EVENT",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/EVENT_ID/get+match+details")
# Hora de Espana con cambio automatico verano/invierno (antes era +2 fijo, y en
# invierno las horas salian 1 hora adelantadas)
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Madrid"))
except Exception:
    TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "1"))))
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


def _find_event(o):
    if isinstance(o, dict):
        if "homeTeam" in o and "status" in o:
            return o
        for v in o.values():
            r = _find_event(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_event(v)
            if r:
                return r
    return None


def event(event_id):
    """Datos frescos de un partido, o None si no se pueden leer."""
    if not URL_EVENT or "EVENT_ID" in URL_EVENT:
        return None
    try:
        return _find_event(_call(URL_EVENT, match_id=event_id))
    except Exception as ex:
        print("WARN event", event_id, ex)
        return None


def h2h(event_id):
    """(victorias_local, victorias_visitante) o None si no hay datos."""
    if not URL_H2H:
        return None
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
            return (int(duel.get("homeWins", 0) or 0), int(duel.get("awayWins", 0) or 0))
    except Exception as ex:
        print("WARN h2h", ex)
    return None


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
    live_pts = 0
    dudoso = False
    if st == "finished" and max(sa, sb) == 3:
        final, points = (sa, sb), ((pa, pb) if pa + pb else None)
        # comprobacion cruzada: sets contados juego a juego y ganador oficial
        sets_a = sets_b = 0
        for i in range(1, 8):
            x, y = hs.get(f"period{i}"), as_.get(f"period{i}")
            if x is None or y is None:
                continue
            x, y = _int(x), _int(y)
            if x > y:
                sets_a += 1
            elif y > x:
                sets_b += 1
        # Solo es dudoso si los sets juego a juego describen OTRO partido completo.
        # Justo al terminar, Sofascore a veces aun no ha cargado el ultimo set
        # (p. ej. 2-2 con marcador final 3-2): eso es un retraso, no un error,
        # y no debe frenar el pick.
        if max(sets_a, sets_b) == 3 and (sets_a, sets_b) != (sa, sb):
            dudoso = True
        wc = e.get("winnerCode")
        if wc in (1, 2) and (wc == 1) != (sa > sb):
            dudoso = True
    elif st == "inprogress":
        live = (sa, sb)
        cur = sa + sb + 1
        live_pts = _int(hs.get(f"period{cur}")) + _int(as_.get(f"period{cur}"))
    elif st != "notstarted":
        return None      # cancelado, aplazado, etc.
    t = _int(e.get("startTimestamp") or e.get("timestamp"))
    return dict(id=str(e.get("id")),
                a=(e.get("homeTeam") or {}).get("name", "?"),
                b=(e.get("awayTeam") or {}).get("name", "?"),
                t=t, final=final, live=live, points=points,
                score=f"{sa}-{sb}", status=st, live_pts=live_pts, dudoso=dudoso)


def build_groups(events):
    """Forma cada grupo con los 6 PRIMEROS partidos (todos contra todos) entre
    4 jugadores. Se recorre en orden cronologico y cada partido se usa una sola
    vez, asi las 'Final games' (mismos jugadores repitiendo) nunca entran en un
    grupo."""
    parsed = [p for p in (parse(e) for e in events) if p]
    parsed.sort(key=lambda x: (x["t"], x["id"]))
    W = WINDOW_H * 3600
    used, groups = set(), []
    for m in parsed:
        if m["id"] in used:
            continue
        a, b, t = m["a"], m["b"], m["t"]
        pool = [x for x in parsed if x["id"] not in used and t <= x["t"] <= t + W]
        near = set()
        for x in pool:
            near |= {x["a"], x["b"]}
        near -= {a, b}
        found = None
        for c, d in combinations(sorted(near), 2):
            four, ms = (a, b, c, d), []
            for p, q in combinations(four, 2):
                if {p, q} == {a, b}:
                    ms.append(m)
                    continue
                cand = [x for x in pool if {x["a"], x["b"]} == {p, q}]
                if not cand:
                    break
                ms.append(min(cand, key=lambda x: x["t"]))   # el primero = de grupo
            if len(ms) == 6:
                found = (four, ms)
                break
        if not found:
            continue
        four, ms = found
        used |= {x["id"] for x in ms}
        ms.sort(key=lambda x: x["t"])
        matches = [Match(x["a"], x["b"], i + 1,
                         time=datetime.fromtimestamp(x["t"], TZ).strftime("%H:%M"),
                         final=x["final"], live=x["live"], points=x["points"], mid=x["id"],
                         live_pts=x.get("live_pts", 0))
                   for i, x in enumerate(ms)]
        grp = Group(list(four), matches)
        grp.dudoso = any(x.get("dudoso") for x in ms)
        groups.append(grp)
    return groups, parsed
