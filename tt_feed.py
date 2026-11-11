"""
tt_feed.py - Lee la liga desde BetsAPI (si FUENTE_DATOS=betsapi; ver tt_betsapi.py)
o, si no, desde "SofaScore - Live API" (Zyla Labs) usando la
funcion "partidos programados de un torneo unico" (TT Cup = 15005) y
reconstruye los grupos de 4 jugadores.
"""
import http.client
import json
import time
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from itertools import combinations

import tt_betsapi
from tt_engine import Group, Match

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
    "https://zylalabs.com/api/12787/sofascore+-+live+api/25094/get+match+details")
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
    for intento in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            break
        except (http.client.IncompleteRead, http.client.RemoteDisconnected,
                ConnectionResetError, json.JSONDecodeError):
            if intento == 2:            # tras 3 intentos, que lo gestione el bot
                raise
            time.sleep(2)              # la respuesta llego cortada: repetir al momento
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


# Si la consulta por torneo devuelve casi nada (le ha pasado a Liga Pro), se
# piden TODOS los partidos de tenis de mesa del dia y se filtran por el torneo.
URL_BY_DATE = os.getenv(
    "ZYLA_URL_BY_DATE",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/25093/get+matches+by+date")
NOMBRES = {"15005": "tt cup", "19039": "czech liga pro", "19041": "tt elite series",
           "15004": "setka cup"}
TORNEO_NOMBRE = os.getenv("TORNEO_NOMBRE", NOMBRES.get(str(TOURNAMENT_ID), "")).lower()
MIN_POR_TORNEO = int(os.getenv("MIN_POR_TORNEO", "20"))
FUENTE = {"modo": "torneo"}           # para /grupos: de donde salen los datos


def _es_del_torneo(e):
    t = e.get("tournament") or {}
    ut = t.get("uniqueTournament") or {}
    if str(ut.get("id", "")) == str(TOURNAMENT_ID):
        return True
    nombre = (ut.get("name") or t.get("name") or "").lower()
    return bool(TORNEO_NOMBRE) and nombre.startswith(TORNEO_NOMBRE)


def _todos_del_dia(d):
    data = _call(URL_BY_DATE, sport_slug="table-tennis", date=d)
    return [e for e in _events(data) if _es_del_torneo(e)]


# ---------------------------------------------------------------------------
# RESPALDO "EN VIVO": si la consulta por torneo no da partidos (le pasa a Liga
# Pro desde el 10/11), se leen los partidos EN JUEGO de tenis de mesa cada vuelta
# y se van guardando. Cuando uno deja de estar en juego se pide su resultado.
# Con los partidos ya vistos se deducen los grupos y se crean los partidos que
# faltan por jugar (mismos 4 jugadores) con hora estimada, para poder avisar.
# ---------------------------------------------------------------------------
URL_LIVE = os.getenv(
    "ZYLA_URL_LIVE",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/25092/get+live+matches")
ACUM = {}         # lo rellena el avisador con su memoria: {"ev": {...}, "sint": {...}}
SLOT = 30 * 60    # tiempo entre partidos de un grupo (para estimar la hora)


def _slim(e):
    t = e.get("tournament") or {}
    ut = t.get("uniqueTournament") or {}
    keep = {k: e.get(k) for k in ("id", "status", "homeScore", "awayScore",
                                   "startTimestamp", "winnerCode")}
    keep["homeTeam"] = {"name": (e.get("homeTeam") or {}).get("name")}
    keep["awayTeam"] = {"name": (e.get("awayTeam") or {}).get("name")}
    keep["tournament"] = {"name": t.get("name"),
                          "uniqueTournament": {"id": ut.get("id"), "name": ut.get("name")}}
    return keep


def _pair(e):
    return tuple(sorted(((e.get("homeTeam") or {}).get("name") or "",
                         (e.get("awayTeam") or {}).get("name") or "")))


def _sint_id(d, pair):
    import hashlib
    return "s" + hashlib.md5(f"{d}|{pair[0]}|{pair[1]}".encode()).hexdigest()[:11]


def _respaldo_vivo(days):
    evs = ACUM.setdefault("ev", {})
    sint = ACUM.setdefault("sint", {})          # id sintetico -> [jugador, jugador, hora]
    data = _call(URL_LIVE, sport_slug="table-tennis")
    vivos = {str(e.get("id")): e for e in _events(data) if _es_del_torneo(e)}
    for k, e in vivos.items():
        evs[k] = _slim(e)
    # los que estaban en juego y ya no aparecen: han terminado -> pedir el resultado
    pend = [k for k, e in evs.items() if (e.get("status") or {}).get("type") == "inprogress"
            and k not in vivos]
    for k in pend[:10]:
        try:
            ev = _find_event(_call(URL_EVENT, match_id=k))
            if ev:
                evs[k] = _slim(ev)
        except Exception as ex:
            print("WARN resultado", k, ex)
    # limpiar lo de hace mas de 30 h
    limite = time.time() - 30 * 3600
    for k in [k for k, e in evs.items() if (e.get("startTimestamp") or 0) < limite]:
        evs.pop(k)
    for k in [k for k, v in sint.items() if v[2] < limite]:
        sint.pop(k)
    reales = list(evs.values())
    por_par = {}
    for e in sorted(reales, key=lambda e: e.get("startTimestamp") or 0):
        por_par.setdefault(_pair(e), []).append(e)
    # el partido real que llega para un par ya "inventado" se queda con el id inventado
    out = []
    ocupados = set()
    for sid, (a, b, t) in sint.items():
        cand = [e for e in por_par.get(tuple(sorted((a, b))), [])
                if abs((e.get("startTimestamp") or 0) - t) <= 2 * 3600]
        if cand:
            e = dict(cand[0])
            ALIAS[sid] = str(e["id"])
            e["id"] = sid
            ocupados.add(str(cand[0]["id"]))
            out.append(e)
    out += [e for e in reales if str(e["id"]) not in ocupados]
    # deducir grupos de 4 con al menos 3 partidos vistos y crear los que faltan
    vistos = sorted(out, key=lambda e: e.get("startTimestamp") or 0)
    hechos = set()
    for i, m1 in enumerate(vistos):
        A, B = _pair(m1)
        t1 = m1.get("startTimestamp") or 0
        for m2 in vistos[i + 1:]:
            t2 = m2.get("startTimestamp") or 0
            if t2 - t1 > 3600:
                break
            C, D = _pair(m2)
            four = frozenset((A, B, C, D))
            if len(four) != 4 or four in hechos:
                continue
            en_tramo = [e for e in vistos if set(_pair(e)) <= four
                        and t1 - 600 <= (e.get("startTimestamp") or 0) <= t1 + 3.5 * 3600]
            pares = {}
            for e in en_tramo:
                pares.setdefault(_pair(e), e)
            if len(pares) < 3:
                continue
            hechos.add(four)
            ult = max(e.get("startTimestamp") or 0 for e in pares.values())
            jug = sorted(four)
            # orden habitual: en cada ronda juega antes el que abrió el grupo
            primero = min(pares.values(), key=lambda e: e.get("startTimestamp") or 0)
            abre = (primero.get("homeTeam") or {}).get("name")
            faltan = [(jug[x], jug[y]) for x in range(4) for y in range(x + 1, 4)
                      if (jug[x], jug[y]) not in pares]
            faltan.sort(key=lambda pr: 0 if abre in pr else 1)
            for k, pr in enumerate(faltan, 1):
                    d = datetime.fromtimestamp(t1, TZ).strftime("%Y-%m-%d")
                    sid = _sint_id(d, pr)
                    if sid not in sint:
                        sint[sid] = [pr[0], pr[1], ult + k * SLOT]
                    if not any(e.get("id") == sid for e in out):
                        out.append({"id": sid, "status": {"type": "notstarted"},
                                    "homeTeam": {"name": pr[0]}, "awayTeam": {"name": pr[1]},
                                    "homeScore": {}, "awayScore": {},
                                    "startTimestamp": sint[sid][2],
                                    "tournament": {"name": TORNEO_NOMBRE}})
    ds = set(days)
    return [e for e in out
            if datetime.fromtimestamp(e.get("startTimestamp") or 0, TZ).strftime("%Y-%m-%d") in ds]


ALIAS = {}        # id inventado -> id real (cuando el partido ya ha aparecido)


FUENTE_DATOS = os.getenv("FUENTE_DATOS", "zyla").strip().lower()   # "betsapi" o "zyla"


def usa_betsapi():
    return FUENTE_DATOS == "betsapi" and bool(tt_betsapi.TOKEN)


def eventos_dia(d):
    """Partidos de la liga de un dia concreto (para apuntar picks atrasados)."""
    if usa_betsapi():
        return tt_betsapi.eventos(TOURNAMENT_ID, [d], vivos=False)
    return _events(_call(URL_TOURNAMENT, unique_tournament_id=TOURNAMENT_ID, date=d))


def fetch():
    """Partidos de la liga de hoy (y de ayer si es de madrugada)."""
    now = datetime.now(TZ)
    days = [now.strftime("%Y-%m-%d")]
    if now.hour < 4:
        days.insert(0, (now - timedelta(days=1)).strftime("%Y-%m-%d"))
    if usa_betsapi():
        try:
            lista = tt_betsapi.eventos(TOURNAMENT_ID, days)
            FUENTE["modo"] = "BetsAPI"
            ACUM["betsapi_ok"] = time.time()
            live = sum(1 for e in lista if e["status"]["type"] == "inprogress")
            return lista, live
        except Exception as ex:
            # un fallo suelto: se salta esta vuelta (cambiar de fuente cambia los
            # numeros de partido y podria repetir avisos). Solo si BetsAPI lleva
            # mas de 10 min caida se tira de Zyla.
            if not KEY or time.time() - ACUM.get("betsapi_ok", 0) < 600:
                raise
            print("WARN BetsAPI caida, uso Zyla", ex)
            FUENTE["modo"] = f"Zyla (BetsAPI falla: {str(ex)[:60]})"
    evs = {}
    pocos = False
    for d in days:
        lista = _events(_call(URL_TOURNAMENT, unique_tournament_id=TOURNAMENT_ID, date=d))
        if len(lista) < MIN_POR_TORNEO and (now.hour >= 6 or d != days[-1]):
            pocos = True
        for e in lista:
            evs[str(e.get("id"))] = e
    if not FUENTE["modo"].startswith("Zyla (BetsAPI"):
        FUENTE["modo"] = "consulta por torneo"
    if pocos:
        # la consulta normal casi no da partidos: respaldo con los partidos en juego
        try:
            extra = _respaldo_vivo(days)
            FUENTE["modo"] = (f"partidos en juego, acumulados (respaldo): "
                              f"{len(ACUM.get('ev', {}))} vistos")
            pares_lista = {_pair(e) for e in evs.values()}
            reales_ids = {str(k) for k in evs}
            for e in extra:
                k = str(e.get("id"))
                if k in evs or ALIAS.get(k) in reales_ids:
                    continue
                if k.startswith("s") and k not in ALIAS and _pair(e) in pares_lista:
                    continue          # ese partido ya viene en la consulta normal
                evs[k] = e
        except Exception as ex:
            print("WARN respaldo en vivo", ex)
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
    if not str(event_id).startswith("b") and (not URL_EVENT or "EVENT_ID" in URL_EVENT):
        return None
    real = ALIAS.get(str(event_id), str(event_id))
    if real.startswith("b"):
        try:
            return tt_betsapi.evento(real)      # partido de BetsAPI
        except Exception as ex:
            print("WARN event BetsAPI", event_id, ex)
            return None
    if real.startswith("s"):
        return None                 # partido aun no visto (solo deducido)
    try:
        ev = _find_event(_call(URL_EVENT, match_id=real))
        if ev and real != str(event_id):
            ev = dict(ev, id=event_id)
        return ev
    except Exception as ex:
        print("WARN event", event_id, ex)
        return None


def h2h(event_id):
    """(victorias_local, victorias_visitante) o None si no hay datos."""
    if not URL_H2H:
        return None
    try:
        real = ALIAS.get(str(event_id), str(event_id))
        if real.startswith(("s", "b")):
            return None             # deducido o de BetsAPI (sin historial aqui)
        d = _call(URL_H2H, **{H2H_PARAM: real})

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
    final = live = points = done_pts = None
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
        # puntos de los sets YA terminados (para calcular desempates por puntos)
        done_pts = (sum(_int(hs.get(f"period{i}")) for i in range(1, cur)),
                    sum(_int(as_.get(f"period{i}")) for i in range(1, cur)))
    elif st != "notstarted":
        return None      # cancelado, aplazado, etc.
    t = _int(e.get("startTimestamp") or e.get("timestamp"))
    return dict(id=str(e.get("id")),
                a=(e.get("homeTeam") or {}).get("name", "?"),
                b=(e.get("awayTeam") or {}).get("name", "?"),
                t=t, final=final, live=live, points=points,
                score=f"{sa}-{sb}", status=st, live_pts=live_pts, dudoso=dudoso,
                done_pts=done_pts)


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
                         time=datetime.fromtimestamp(x["t"], TZ).strftime("%H:%M")
                         + ("≈" if str(x["id"]).startswith("s") else ""),   # hora estimada
                         final=x["final"], live=x["live"], points=x["points"], mid=x["id"],
                         live_pts=x.get("live_pts", 0))
                   for i, x in enumerate(ms)]
        for mm, x in zip(matches, ms):
            mm.done_pts = x.get("done_pts")      # puntos de sets terminados (en juego)
        grp = Group(list(four), matches)
        grp.dudoso = any(x.get("dudoso") for x in ms)
        # ¿grupo de 5? (Liga Pro los tiene): un 5.º jugador que se enfrenta en este
        # mismo tramo a 3 o mas de los 4. Entonces la tabla de 4 seria falsa.
        rivales = {}
        t_ini, t_fin = ms[0]["t"], ms[-1]["t"]      # SOLO mientras dura este grupo
        for x in pool:
            if not (t_ini <= x["t"] <= t_fin):
                continue                           # el torneo siguiente no cuenta
            for p_, q_ in ((x["a"], x["b"]), (x["b"], x["a"])):
                if p_ in four and q_ not in four:
                    rivales.setdefault(q_, set()).add(p_)
        grp.quinto = next((q_ for q_, v in rivales.items() if len(v) >= 3), None)
        if grp.quinto:
            grp.dudoso = True
        groups.append(grp)
    return groups, parsed
