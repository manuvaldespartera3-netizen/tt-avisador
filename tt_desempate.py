"""
tt_desempate.py - ¿QUE REGLA DE DESEMPATE USA DE VERDAD LA LIGA?

Con los partidos ya descargados del historico (cache del backtest, NO descarga
nada), rehace todos los grupos con empate a puntos en la frontera 2.º/3.º y mira
quien jugo de verdad la FINAL (los 2 primeros juegan la final y los otros dos el
3.er puesto). Compara varias reglas posibles y dice cual acierta.

Reglas que se prueban (en empates de 2 jugadores siempre manda el enfrentamiento
directo en las de "mini-liga"):
  BOT_LP     la que usa ahora el bot en Liga Pro: sets ganados, dif. sets, dif.
             puntos y puntos de TODO el grupo; directo si quedan 2
  ITTF       mini-liga entre los empatados: victorias entre ellos, cociente de
             sets entre ellos, cociente de puntos entre ellos (la de TT Cup)
  MINI_DIF   mini-liga entre los empatados con DIFERENCIA de sets y luego
             diferencia de puntos (en vez de cociente)
  TOTAL      cociente de sets de todo el grupo, luego de puntos
Variables: TT_TOURNAMENT_ID (19039 = Liga Pro), TELEGRAM_TOKEN, MY_CHAT_ID
"""
import json
import os
from collections import defaultdict

import tt_engine
from tt_feed import parse, TZ
from tt_backtest import build_groups_fast, has_fifth, tg_text, LIGA, OUT, CACHE

REGLAS = ["BOT_LP", "ITTF", "MINI_DIF", "TOTAL"]


def cargar():
    evs = {}
    if not os.path.isdir(CACHE):
        return []
    for fn in sorted(os.listdir(CACHE)):
        if fn.endswith(".json") and fn[:4].isdigit():
            try:
                with open(os.path.join(CACHE, fn)) as f:
                    for e in json.load(f):
                        evs[e.get("id")] = e
            except Exception:
                pass
    parsed = [p for p in (parse(e) for e in evs.values()) if p and p["final"]]
    parsed.sort(key=lambda x: (x["t"], x["id"]))
    return parsed


def tiers_engine(players, res, modo):
    old = tt_engine.TIEBREAK
    tt_engine.TIEBREAK = modo
    try:
        return tt_engine._rank(players, res)
    finally:
        tt_engine.TIEBREAK = old


def mini_dif(grp, res, depth=0):
    """Mini-liga entre empatados con diferencia de sets y luego de puntos."""
    if len(grp) == 1:
        return [grp]
    sub = {k: v for k, v in res.items() if k <= set(grp)}
    if len(grp) == 2:
        r = sub.get(frozenset(grp))
        if not r:
            return [grp]
        w = r[0] if r[2] > r[3] else r[1]
        return [[w], [grp[1] if w == grp[0] else grp[0]]]
    mw = {p: 0 for p in grp}
    for (a, b, sa, sb, _, _) in sub.values():
        mw[a if sa > sb else b] += 1
    if len(set(mw.values())) > 1 and depth < 5:
        out = []
        for w in sorted(set(mw.values()), reverse=True):
            out.extend(mini_dif([p for p in grp if mw[p] == w], res, depth + 1))
        return out
    for clave in ("sets", "puntos"):
        dif = {p: 0 for p in grp}
        for (a, b, sa, sb, pa, pb) in sub.values():
            if clave == "sets":
                va, vb = sa, sb
            else:
                if pa is None:
                    return [grp]
                va, vb = pa, pb
            dif[a] += va - vb
            dif[b] += vb - va
        vals = sorted(set(dif.values()), reverse=True)
        if len(vals) > 1:
            out = []
            for v in vals:
                g2 = [p for p in grp if dif[p] == v]
                out.extend(mini_dif(g2, res, depth + 1) if len(g2) < len(grp) else [g2])
            return out
    return [grp]


def tiers_mini_dif(players, res):
    wins = {p: 0 for p in players}
    for (a, b, sa, sb, _, _) in res.values():
        wins[a if sa > sb else b] += 1
    out = []
    for w in sorted(set(wins.values()), reverse=True):
        out.extend(mini_dif([p for p in players if wins[p] == w], res))
    return out


def top2(tiers):
    top, pos = [], 0
    for t in tiers:
        if pos + len(t) <= 2:
            top += t
        elif pos < 2:
            return None                   # empate sin resolver en la frontera
        pos += len(t)
    return set(top)


def main():
    parsed = cargar()
    L = [f"⚖️ ¿QUÉ DESEMPATE USA DE VERDAD {LIGA.upper()}?", ""]
    if not parsed:
        L.append("❌ No encuentro el histórico descargado (cache del backtest).")
        return fin(L)
    groups = build_groups_fast(parsed)
    by_t = parsed
    st = {r: [0, 0] for r in REGLAS}            # aciertos, casos decididos
    tri = {r: [0, 0] for r in REGLAS}           # solo triples empates
    dist = defaultdict(int)
    casos = triples = 0
    ejemplos = []
    for players, ms in groups:
        if has_fifth(players, ms, by_t):
            continue
        pts = {p: 0 for p in players}
        sw = defaultdict(int); sl = defaultdict(int); pw = defaultdict(int); pl = defaultdict(int)
        res = {}
        for x in ms:
            sa, sb = x["final"]
            w, l = (x["a"], x["b"]) if sa > sb else (x["b"], x["a"])
            pts[w] += 2; pts[l] += 1
            sw[x["a"]] += sa; sl[x["a"]] += sb; sw[x["b"]] += sb; sl[x["b"]] += sa
            pa, pb = x["points"] if x.get("points") else (None, None)
            if pa is not None:
                pw[x["a"]] += pa; pl[x["a"]] += pb; pw[x["b"]] += pb; pl[x["b"]] += pa
            res[frozenset((x["a"], x["b"]))] = (x["a"], x["b"], sa, sb, pa, pb)
        orden = sorted(players, key=lambda p: -pts[p])
        if pts[orden[1]] != pts[orden[2]]:
            continue
        ids = {x["id"] for x in ms}
        fin_ = [x for x in by_t if ms[-1]["t"] < x["t"] <= ms[-1]["t"] + 3 * 3600
                and x["id"] not in ids and {x["a"], x["b"]} <= set(players)]
        pares = []
        for x in fin_:
            pr = frozenset((x["a"], x["b"]))
            if all(not (pr & q) for q in pares):
                pares.append(pr)
            if len(pares) == 2:
                break
        if len(pares) == 1:
            pares.append(frozenset(players) - pares[0])
        if len(pares) != 2:
            continue
        s0, s1 = (sum(pts[p] for p in q) for q in pares)
        if s0 == s1:
            continue
        real = set(pares[0] if s0 > s1 else pares[1])
        empatados = [p for p in players if pts[p] == pts[orden[1]]]
        es_triple = len(empatados) >= 3
        casos += 1
        triples += es_triple
        pred = {
            "BOT_LP": top2(tiers_engine(players, res, "ligapro")),
            "ITTF": top2(tiers_engine(players, res, "ttcup")),
            "MINI_DIF": top2(tiers_mini_dif(players, res)),
        }
        def key(p):
            return (pts[p], (sw[p] / sl[p]) if sl[p] else 99, (pw[p] / pl[p]) if pl[p] else 99)
        tt = sorted(players, key=key, reverse=True)
        pred["TOTAL"] = set(tt[:2]) if key(tt[1]) != key(tt[2]) else None
        for r in REGLAS:
            if pred[r] is None:
                continue
            ok = pred[r] == real
            st[r][0] += ok; st[r][1] += 1
            if es_triple:
                tri[r][0] += ok; tri[r][1] += 1
        if pred["BOT_LP"] and pred["ITTF"] and pred["BOT_LP"] != pred["ITTF"]:
            dist["casos"] += 1
            dist["bot"] += pred["BOT_LP"] == real
            dist["ittf"] += pred["ITTF"] == real
            if len(ejemplos) < 8:
                from datetime import datetime
                ejemplos.append(f"   {datetime.fromtimestamp(ms[0]['t'], TZ):%d/%m %H:%M} · "
                                + " · ".join(f"{p} {pts[p]}" for p in orden)
                                + f" → final real: {' y '.join(sorted(real))} · BOT_LP "
                                + ("✅" if pred['BOT_LP'] == real else "❌") + " · ITTF "
                                + ("✅" if pred['ITTF'] == real else "❌"))
    L.append(f"Grupos con empate en la frontera 2.º/3.º: {casos} (triples empates: {triples})")
    L.append("")
    L.append("Acierto de cada regla (sobre los grupos que esa regla decide):")
    for r in REGLAS:
        a, n = st[r]
        ta, tn = tri[r]
        L.append(f"   {r:9s} {a}/{n} = {100 * a / max(1, n):.1f}%  ·  en triples: "
                 f"{ta}/{tn} = {100 * ta / max(1, tn):.1f}%")
    L.append("")
    L.append(f"Grupos donde BOT_LP e ITTF dicen distinto: {dist['casos']} · "
             f"acierta BOT_LP {dist['bot']} · acierta ITTF {dist['ittf']}")
    L += ejemplos
    mejor = max(REGLAS, key=lambda r: st[r][0] / max(1, st[r][1]))
    L.append("")
    L.append(f"CONCLUSIÓN: la regla que mejor encaja con la liga es {mejor} "
             f"({100 * st[mejor][0] / max(1, st[mejor][1]):.1f}%).")
    fin(L)


def fin(L):
    txt = "\n".join(L)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "desempate.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    print(txt)
    tg_text(txt[:3900])


if __name__ == "__main__":
    main()
