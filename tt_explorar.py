"""
tt_explorar.py - COMPARACION ZYLA vs BETSAPI (antes de quitar Zyla).

Para cada liga (TT Cup, Czech Liga Pro, TT Elite Series) y para AYER entero:
  1. Partidos: cuantos da cada fuente, cuantos coinciden y cuales faltan en cada una.
  2. Cuotas a ganador: en cuantos partidos hay cuota en BetsAPI (Bet365) y en Zyla
     (Sofascore), y cuanto se parecen.
  3. Cuotas ANTES del partido: de los proximos de las 3 h siguientes, cuantos
     tienen ya cuota en BetsAPI (lo que necesitan los bots que avisan 2 h antes).
  4. Historico: si BetsAPI tiene partidos de hace 90-180 dias (para los backtests).
  5. H2H en BetsAPI.
Guarda todo en backtest/exploracion.txt y manda un resumen por Telegram.
No toca ningun avisador.
"""
import json
import os
import random
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import tt_betsapi as B
import tt_feed as F
from tt_feed import TZ
from tt_backtest import tg_text

OUT = "backtest"
LIGAS = [("15005", "29097", "TT Cup"), ("19039", "22742", "Liga Pro"), ("19041", "29128", "Elite")]
URL_ODDS_ZYLA = "https://zylalabs.com/api/12787/sofascore+-+live+api/25097/get+match+odds"
random.seed(7)


def norm(n):
    s = unicodedata.normalize("NFKD", n or "").encode("ascii", "ignore").decode().lower()
    return "".join(c if c.isalpha() else " " for c in s).split()


def apellido(n):
    """'Kowal R.' -> 'kowal' · 'Velky Junior D.' -> 'velky junior'."""
    w = norm(n)
    if len(w) >= 2 and len(w[-1]) == 1:
        w = w[:-1]
    return " ".join(w)


def madrid(ts):
    return datetime.fromtimestamp(ts, TZ)


def utc_hoy():
    return datetime.now(timezone.utc)


def zyla_dia(sid, d):
    """Partidos terminados de Zyla con fecha (Madrid) d, mirando d-1, d y d+1."""
    evs = {}
    for k in (-1, 0, 1):
        dd = (d + timedelta(days=k)).strftime("%Y-%m-%d")
        try:
            for e in F._events(F._call(F.URL_TOURNAMENT, unique_tournament_id=sid, date=dd)):
                evs[str(e.get("id"))] = e
        except Exception as ex:
            print("WARN zyla", sid, dd, ex)
    out = []
    for e in evs.values():
        p = F.parse(e)
        if p and p["final"] and madrid(p["t"]).date() == d:
            out.append(p)
    return out


def bets_dia(lid, d):
    """Terminados de BetsAPI con fecha (Madrid) d (dias UTC d-1 y d)."""
    evs = {}
    for k in (-1, 0):
        dd = (d + timedelta(days=k)).strftime("%Y%m%d")
        for e in B._paginas("/v3/events/ended", sport_id=B.SPORT, league_id=lid, day=dd):
            evs[str(e["id"])] = e
    out = []
    for e in evs.values():
        p = F.parse(B.convertir(e))
        if p and p["final"] and madrid(p["t"]).date() == d:
            p["raw"] = e
            out.append(p)
    return out


def emparejar(zs, bs):
    """Empareja por los dos apellidos y hora (±20 min). Devuelve pares y sobrantes."""
    libres = list(bs)
    pares, solo_z = [], []
    for z in sorted(zs, key=lambda x: x["t"]):
        kz = {apellido(z["a"]), apellido(z["b"])}
        pz = {x.split()[0] for x in kz if x}
        mejor = None
        for b in libres:
            if abs(b["t"] - z["t"]) > 20 * 60:
                continue
            kb = {apellido(b["a"]), apellido(b["b"])}
            if kz == kb or len(pz & {x.split()[0] for x in kb if x}) == 2:
                if mejor is None or abs(b["t"] - z["t"]) < abs(mejor["t"] - z["t"]):
                    mejor = b
        if mejor:
            libres.remove(mejor)
            pares.append((z, mejor))
        else:
            solo_z.append(z)
    return pares, solo_z, libres


def cuota_bets(eid):
    """{'start': (local, visit), 'end': (local, visit)} de Bet365 a ganador."""
    d = B.api("/v2/event/odds/summary", event_id=str(eid).lstrip("b"))
    o = ((d.get("results") or {}).get("Bet365") or {}).get("odds") or {}
    res = {}
    for k in ("start", "end"):
        m = (o.get(k) or {}).get("92_1") or {}
        try:
            res[k] = (float(m["home_od"]), float(m["away_od"]))
        except (KeyError, TypeError, ValueError):
            pass
    return res


def cuota_zyla(mid):
    """(local, visitante) cuota de Sofascore o None."""
    try:
        data = F._call(URL_ODDS_ZYLA, match_id=mid)
    except Exception:
        return None

    def markets(o):
        if isinstance(o, dict):
            if isinstance(o.get("markets"), list):
                return o["markets"]
            for v in o.values():
                r = markets(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = markets(v)
                if r:
                    return r
        return []

    def dec(v):
        try:
            v = str(v)
            if "/" in v:
                a, b = v.split("/")
                return 1 + float(a) / float(b)
            return float(v)
        except Exception:
            return None
    for m in markets(data):
        ch = {str(c.get("name")): c for c in m.get("choices", [])}
        if set(ch) == {"1", "2"}:
            h = dec(ch["1"].get("fractionalValue") or ch["1"].get("decimalValue"))
            a = dec(ch["2"].get("fractionalValue") or ch["2"].get("decimalValue"))
            if h and a:
                return h, a
    return None


def liga(sid, lid, nom, ayer, L, R):
    L.append(f"======== {nom} · {ayer:%d/%m} ========")
    try:
        zs = zyla_dia(sid, ayer)
    except Exception as ex:
        zs = []
        L.append(f"Zyla: ❌ {str(ex)[:100]}")
    try:
        bs = bets_dia(lid, ayer)
    except Exception as ex:
        bs = []
        L.append(f"BetsAPI: ❌ {str(ex)[:100]}")
    pares, solo_z, solo_b = emparejar(zs, bs)
    L.append(f"1) Terminados: Zyla {len(zs)} · BetsAPI {len(bs)} · coinciden {len(pares)} · "
             f"solo en Zyla {len(solo_z)} · solo en BetsAPI {len(solo_b)}")
    mal = sum(1 for z, b in pares if (z["final"][0] > z["final"][1]) !=
              ((b["final"][0] > b["final"][1]) == (apellido(b["a"]) == apellido(z["a"]))))
    L.append(f"   Ganador distinto entre las dos fuentes: {mal} de {len(pares)}")
    for x in solo_z[:8]:
        L.append(f"   falta en BetsAPI: {madrid(x['t']):%H:%M} {x['a']} – {x['b']} {x['score']}")
    for x in solo_b[:5]:
        L.append(f"   falta en Zyla:    {madrid(x['t']):%H:%M} {x['a']} – {x['b']} {x['score']}")
    horas = sorted({madrid(x["t"]).hour for x in solo_z})
    if horas:
        L.append(f"   horas de lo que falta en BetsAPI: {horas}")
    L.append(f"   BetsAPI con id de Bet365: {sum(1 for b in bs if b['raw'].get('bet365_id'))} de {len(bs)}")

    # 2) cuotas en terminados (muestra)
    mu = random.sample(pares, min(20, len(pares)))
    cb = cz = 0
    dif = []
    for z, b in mu:
        o = cuota_bets(b["id"])
        ob = o.get("end") or o.get("start")
        oz = cuota_zyla(z["id"])
        cb += bool(ob)
        cz += bool(oz)
        if ob and oz:
            bh = ob[0] if apellido(b["a"]) == apellido(z["a"]) else ob[1]
            dif.append(abs(bh - oz[0]))
    L.append(f"2) Cuotas (muestra de {len(mu)} terminados): BetsAPI {cb} · Zyla {cz}"
             + (f" · diferencia media cuando hay las dos {sum(dif) / len(dif):.2f}" if dif else ""))

    # 3) cuotas de los proximos (3 h)
    ahora = time.time()
    prox = []
    try:
        for k in (0, 1):
            dd = (utc_hoy() + timedelta(days=k)).strftime("%Y%m%d")
            prox += B._paginas("/v3/events/upcoming", sport_id=B.SPORT, league_id=lid, day=dd)
    except Exception as ex:
        L.append(f"3) próximos: ❌ {str(ex)[:80]}")
    prox = [e for e in prox if 0 < int(e.get("time") or 0) - ahora <= 3 * 3600]
    mu = random.sample(prox, min(15, len(prox)))
    conc = 0
    for e in mu:
        o = cuota_bets(e["id"])
        conc += bool(o.get("end") or o.get("start"))
    L.append(f"3) Próximos 3 h: {len(prox)} · con cuota ya en BetsAPI: {conc} de {len(mu)} mirados")
    if nom == "TT Cup" and prox:
        try:
            h = B.api("/v1/event/history", event_id=prox[0]["id"]).get("results") or {}
            L.append("5) H2H BetsAPI: " + ", ".join(f"{k}: {len(v) if isinstance(v, list) else v}"
                                                 for k, v in h.items()))
        except Exception as ex:
            L.append(f"5) H2H BetsAPI: ❌ {str(ex)[:80]}")

    # 4) historico
    hist = []
    for dias in (180, 150, 90):
        dd = (utc_hoy() - timedelta(days=dias)).strftime("%Y%m%d")
        try:
            d = B.api("/v3/events/ended", sport_id=B.SPORT, league_id=lid, day=dd, page=1)
            hist.append(f"hace {dias} d: {(d.get('pager') or {}).get('total', len(d.get('results') or []))}")
        except Exception as ex:
            hist.append(f"hace {dias} d: ❌ {str(ex)[:40]}")
    L.append("4) Histórico BetsAPI (partidos ese día): " + " · ".join(hist))
    L.append("")
    R.append(f"{nom}: Zyla {len(zs)} · BetsAPI {len(bs)} · coinciden {len(pares)} · "
             f"faltan en BetsAPI {len(solo_z)} · cuotas B/Z {cb}/{cz} · próximos con cuota {conc}/{len(mu)}")


def main():
    L = ["🔎 COMPARACIÓN ZYLA vs BETSAPI", ""]
    if not B.TOKEN:
        L.append("❌ Falta BETSAPI_TOKEN")
        return fin(L, L)
    ayer = (datetime.now(TZ) - timedelta(days=1)).date()
    R = [f"🔎 ZYLA vs BETSAPI · {ayer:%d/%m}"]
    n0 = B.LLAMADAS[0]
    for sid, lid, nom in LIGAS:
        try:
            liga(sid, lid, nom, ayer, L, R)
        except Exception as ex:
            L.append(f"{nom}: ❌ {type(ex).__name__} {str(ex)[:100]}")
    L.append(f"Consultas a BetsAPI usadas: {B.LLAMADAS[0] - n0}")
    R.append("Detalle en backtest/exploracion.txt (pásaselo a Claude)")
    fin(L, R)


def fin(L, R):
    txt = "\n".join(L)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "exploracion.txt"), "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
    tg_text("\n".join(R)[:3900])


if __name__ == "__main__":
    main()
