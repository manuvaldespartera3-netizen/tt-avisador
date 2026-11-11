"""
tt_explorar.py - DIAGNÓSTICO de la fuente de datos (Zyla / Sofascore).

Sirve para averiguar por qué una liga deja de llegar (por ejemplo, si Sofascore
le ha cambiado el número de torneo). Hace tres cosas:
  1. Cuenta los partidos de ayer y de hoy de cada liga con su número actual.
  2. Busca partidos de jugadores conocidos de Liga Pro y mira en qué torneo
     (número y nombre) aparecen ahora.
  3. Busca torneos llamados "Liga Pro" y muestra sus números.
Envía el informe por Telegram (bot de TT Cup) y lo guarda en backtest/exploracion.txt.
No toca nada de los avisadores.
"""
import os
from collections import Counter
from datetime import datetime, timedelta

import tt_feed
from tt_feed import _call, _events, URL_TOURNAMENT, TZ
from tt_backtest import tg_text, tg_file

LIGAS = {"TT Cup": "15005", "Czech Liga Pro": "19039", "TT Elite Series": "19041",
         "Setka Cup": "15004"}
BASE = "https://zylalabs.com/api/12787/sofascore+-+live+api/"
URL_SEARCH_MATCHES = os.getenv("ZYLA_URL_SEARCH_MATCHES", BASE + "25085/search+matches")
URL_SEARCH_ALL = os.getenv("ZYLA_URL_SEARCH_ALL", BASE + "25082/search+all+entities")
URL_SEARCH_UT = BASE + "25089/search+unique+tournaments"
URL_BY_DATE = BASE + "25093/get+matches+by+date"
URL_UT_DATE = BASE + "31094/get+scheduled+unique-tournaments+for+a+specific+date"
JUGADORES_LP = ["Brozhik", "Saleny", "Zahradnik", "Dolezal", "Heczko", "Machander", "Volf"]
OUT = "backtest"


def walk(o):
    """Recorre cualquier JSON y devuelve todos los dicts que contiene."""
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def torneo_de(ev):
    t = ev.get("tournament") or {}
    ut = t.get("uniqueTournament") or {}
    cat = (t.get("category") or ut.get("category") or {}).get("name", "")
    return (str(ut.get("id", "?")), ut.get("name") or t.get("name", "?"), cat)


def main():
    L = ["🔎 DIAGNÓSTICO DE DATOS (Zyla/Sofascore)", ""]
    hoy = datetime.now(TZ).date()
    dias = [(hoy - timedelta(days=k)).strftime("%Y-%m-%d") for k in (2, 1, 0)]

    L.append("1) Partidos por liga (número de torneo actual):")
    for nombre, tid in LIGAS.items():
        cuentas, nombres = [], Counter()
        for d in dias:
            try:
                evs = _events(_call(URL_TOURNAMENT, unique_tournament_id=tid, date=d))
                cuentas.append(str(len(evs)))
                for e in evs:
                    nombres[(e.get("tournament") or {}).get("name", "?")] += 1
            except Exception as ex:
                cuentas.append(f"error ({str(ex)[:60]})")
        L.append(f"   {nombre} ({tid}): {' / '.join(cuentas)}  [antes de ayer / ayer / hoy]")
        if nombres:
            L.append("      torneos: " + ", ".join(f"{n} ×{c}" for n, c in nombres.most_common(3)))

    L.append("")
    L.append("2) ¿En qué torneo salen ahora jugadores de Liga Pro?")
    vistos = Counter()
    ejemplos = {}
    for j in JUGADORES_LP:
        try:
            data = _call(URL_SEARCH_MATCHES, q=j)
        except Exception as ex:
            L.append(f"   {j}: error ({str(ex)[:80]})")
            continue
        n = 0
        for ev in walk(data):
            if "homeTeam" in ev and "tournament" in ev:
                ts = ev.get("startTimestamp") or 0
                if ts and datetime.fromtimestamp(ts, TZ).date() < hoy - timedelta(days=3):
                    continue
                key = torneo_de(ev)
                vistos[key] += 1
                ejemplos.setdefault(key, f"{(ev.get('homeTeam') or {}).get('name')} – "
                                         f"{(ev.get('awayTeam') or {}).get('name')} "
                                         f"{datetime.fromtimestamp(ts, TZ).strftime('%d/%m %H:%M') if ts else ''}")
                n += 1
        L.append(f"   {j}: {n} partidos recientes")
    if vistos:
        L.append("   Torneos donde aparecen (número · nombre · país):")
        for (tid, nom, cat), c in vistos.most_common(8):
            L.append(f"   ▶ {tid} · {nom} · {cat} ×{c}   p. ej. {ejemplos[(tid, nom, cat)]}")
    else:
        L.append("   (la búsqueda no devolvió partidos)")

    L.append("")
    L.append("3) Torneos que se llaman 'Liga Pro':")
    try:
        data = _call(URL_SEARCH_ALL, q="liga pro")
        tors = {}
        for o in walk(data):
            ut = o.get("uniqueTournament") if isinstance(o.get("uniqueTournament"), dict) else None
            for t in ([ut] if ut else []) + ([o] if o.get("slug") and "id" in o and
                                               "name" in o and o.get("category") else []):
                sport = ((t.get("category") or {}).get("sport") or {}).get("name", "")
                tors[str(t.get("id"))] = f"{t.get('name')} · {(t.get('category') or {}).get('name', '')} · {sport}"
        for tid, desc in list(tors.items())[:12]:
            L.append(f"   ▶ {tid} · {desc}")
        if not tors:
            L.append("   (sin resultados)")
    except Exception as ex:
        L.append(f"   error ({str(ex)[:80]})")

    L.append("")
    L.append("4) Torneos de tenis de mesa programados HOY (número · nombre):")
    try:
        data = _call(URL_UT_DATE, sport_slug="table-tennis", date=dias[-1])
        vistos4 = {}
        for o in walk(data):
            if "id" in o and "name" in o and ("slug" in o or "category" in o):
                vistos4[str(o["id"])] = o.get("name")
        filtro = [(k, v) for k, v in vistos4.items()
                  if any(w in str(v).lower() for w in ("liga pro", "tt cup", "elite", "setka"))]
        for k, v in (filtro or list(vistos4.items()))[:15]:
            L.append(f"   ▶ {k} · {v}")
        if not vistos4:
            L.append("   (sin resultados)")
    except Exception as ex:
        L.append(f"   error ({str(ex)[:80]})")

    L.append("")
    L.append("5) Búsqueda de torneos 'Liga Pro':")
    try:
        data = _call(URL_SEARCH_UT, q="liga pro")
        n5 = 0
        for o in walk(data):
            if "id" in o and "name" in o and "liga pro" in str(o.get("name")).lower():
                cat = (o.get("category") or {}).get("name", "")
                sp = ((o.get("category") or {}).get("sport") or {}).get("slug", "")
                L.append(f"   ▶ {o['id']} · {o['name']} · {cat} · {sp}")
                n5 += 1
                if n5 >= 10:
                    break
        if not n5:
            L.append("   (sin resultados)")
    except Exception as ex:
        L.append(f"   error ({str(ex)[:80]})")

    L.append("")
    L.append("6) TODOS los partidos de tenis de mesa de HOY, por torneo:")
    try:
        data = _call(URL_BY_DATE, sport_slug="table-tennis", date=dias[-1])
        por = Counter()
        for ev in walk(data):
            if "homeTeam" in ev and "tournament" in ev:
                por[torneo_de(ev)] += 1
        for (tid, nom, cat), c in por.most_common(12):
            L.append(f"   ▶ {tid} · {nom} · {cat}: {c} partidos")
        if not por:
            L.append("   (sin resultados)")
    except Exception as ex:
        L.append(f"   error ({str(ex)[:80]})")

    L.append("")
    L.append("7) Pruebas de otras vías para leer Liga Pro hoy:")
    hoy_s = dias[-1]

    def prueba(nombre, url, **kw):
        try:
            data = _call(url, **kw)
            evs = [e for e in walk(data) if "homeTeam" in e and "awayTeam" in e]
            lp = [e for e in evs if "liga pro" in str((e.get("tournament") or {}).get("name", "")).lower()]
            L.append(f"   {nombre}: OK · {len(evs)} partidos · de Liga Pro {len(lp)}")
            return data
        except Exception as ex:
            L.append(f"   {nombre}: {str(ex)[:70]}")
            return None

    for sl in ("table-tennis", "table_tennis", "tabletennis"):
        prueba(f"por fecha sport_slug={sl}", URL_BY_DATE, sport_slug=sl, date=hoy_s)
    prueba("por fecha date=today", URL_BY_DATE, sport_slug="table-tennis", date="today")
    prueba("en vivo (table-tennis)", BASE + "25092/get+live+matches", sport_slug="table-tennis")
    prueba("torneo 99075 por fecha", URL_TOURNAMENT, unique_tournament_id="99075", date=hoy_s)
    seas = prueba("temporadas de 19039", BASE + "25108/get+unique+tournament+seasons",
                  unique_tournament_id="19039")
    if seas:
        ss = [o for o in walk(seas) if "id" in o and ("year" in o or "name" in o)][:3]
        L.append("      temporadas: " + ", ".join(f"{o.get('id')} ({o.get('year') or o.get('name')})" for o in ss))
        if ss:
            rd = prueba("rondas temporada actual", BASE + "25114/get+unique+tournament+season+rounds",
                        unique_tournament_id="19039", season_id=ss[0]["id"])
            if rd:
                cur = next((o.get("currentRound") for o in walk(rd) if "currentRound" in o), None)
                L.append(f"      ronda actual: {cur}")
                rnd = (cur or {}).get("round") if isinstance(cur, dict) else cur
                if rnd:
                    prueba("partidos de la ronda actual", BASE + "25113/get+unique+tournament+season+round+matches",
                           unique_tournament_id="19039", season_id=ss[0]["id"], round=rnd)
    try:
        tm = _call(BASE + "25083/search+teams", q="Brozhik")
        tid = next((o.get("id") for o in walk(tm) if "id" in o and "brozhik" in str(o.get("name", "")).lower()), None)
        L.append(f"   jugador Brozhik: id {tid}")
        if tid:
            prueba("próximos partidos de Brozhik", BASE + "26349/get+team+upcoming+matches", team_id=tid)
            prueba("últimos partidos de Brozhik", BASE + "26348/get+team+finished+matches", team_id=tid)
    except Exception as ex:
        L.append(f"   jugador Brozhik: {str(ex)[:70]}")

    txt = "\n".join(L)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "exploracion.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
    tg_text(txt[:3900])
    tg_file(path)


if __name__ == "__main__":
    main()
