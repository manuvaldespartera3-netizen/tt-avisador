"""
tt_explorar.py - Averigua con datos reales como funciona cada liga.

Para cada liga descarga los ultimos dias y responde:
  - cuantos jugadores tiene cada grupo (todos contra todos)
  - si despues del grupo hay mas partidos y entre quienes
    (1.º vs 2.º = final, 3.º vs 4.º = tercer puesto, etc.)
  - cuantos grupos hay al dia y a que horas
Asi se sabe si el patron (eliminado vs el que lo necesita) existe en esa liga.

Envia el informe por Telegram y lo guarda en backtest/exploracion.txt.
"""
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import tt_feed
from tt_feed import parse, TZ
from tt_backtest import fetch_day, tg_text, tg_file

LIGAS = {
    "TT Cup": "15005",
    "Czech Liga Pro": "19039",
    "TT Elite Series": "19041",
    "Setka Cup": "15004",
}
DIAS = int(os.getenv("EX_DAYS", "4"))
OUT = os.getenv("BT_OUT", "backtest")
GAP = 3 * 3600       # partidos de un mismo grupo: como mucho 3 h entre el primero y el ultimo


def blocks(parsed):
    """Busca grupos todos-contra-todos: empieza con un partido y va anadiendo
    jugadores que se hayan enfrentado a TODOS los del grupo en la ventana."""
    used, out = set(), []
    for i, m in enumerate(parsed):
        if m["id"] in used:
            continue
        t0 = m["t"]
        pool = [x for x in parsed[i:] if x["id"] not in used and x["t"] <= t0 + GAP]
        first = {}
        for x in pool:
            first.setdefault(frozenset((x["a"], x["b"])), x)
        grp = [m["a"], m["b"]]
        cands = []
        for x in pool:
            for p in (x["a"], x["b"]):
                if p not in grp and p not in cands:
                    cands.append(p)
        for c in cands:
            if all(frozenset((c, g)) in first for g in grp):
                grp.append(c)
        n = len(grp)
        if n < 3:
            continue
        rr = [first[frozenset((p, q))] for i2, p in enumerate(grp) for q in grp[i2 + 1:]]
        used |= {x["id"] for x in rr}
        end = max(x["t"] for x in rr)
        # partidos posteriores entre jugadores del grupo (finales, 3.er puesto...)
        extra = [x for x in pool if x["id"] not in used and x["t"] > end - 60
                 and x["a"] in grp and x["b"] in grp and x["t"] <= end + 2 * 3600]
        used |= {x["id"] for x in extra}
        out.append((grp, rr, extra))
    return out


def standings(grp, rr):
    wins = {p: 0 for p in grp}
    sets = {p: 0 for p in grp}
    for x in rr:
        if not x["final"]:
            return None
        sa, sb = x["final"]
        wins[x["a"] if sa > sb else x["b"]] += 1
        sets[x["a"]] += sa - sb
        sets[x["b"]] += sb - sa
    order = sorted(grp, key=lambda p: (-wins[p], -sets[p]))
    return {p: i + 1 for i, p in enumerate(order)}


def explore(name, tid):
    tt_feed.TOURNAMENT_ID = tid
    import tt_backtest
    tt_backtest.TOURNAMENT_ID = tid
    end = datetime.now(TZ).date() - timedelta(days=1)
    evs = {}
    for k in range(DIAS):
        d = (end - timedelta(days=k)).strftime("%Y-%m-%d")
        tt_backtest.CACHE = os.path.join(OUT, "cache", tid)
        for e in fetch_day(d):
            evs[e.get("id")] = e
    parsed = [p for p in (parse(e) for e in evs.values()) if p]
    parsed.sort(key=lambda x: (x["t"], x["id"]))
    L = [f"🏓 {name} (id {tid}) · últimos {DIAS} días"]
    if not parsed:
        L.append("   ❌ La API no devuelve partidos para esta liga")
        return L
    L.append(f"   {len(parsed)} partidos ({len(parsed) // DIAS} al día)")
    bl = blocks(parsed)
    sizes = Counter(len(g) for g, _, _ in bl)
    L.append("   Grupos todos-contra-todos: " +
             ", ".join(f"{n} jugadores ×{c}" for n, c in sorted(sizes.items())))
    post = Counter()
    n_extra = Counter()
    for grp, rr, extra in bl:
        st = standings(grp, rr)
        n_extra[(len(grp), len(extra))] += 1
        if not st:
            continue
        for x in extra:
            a, b = sorted((st[x["a"]], st[x["b"]]))
            post[(len(grp), f"{a}.º vs {b}.º")] += 1
    for n in sorted(sizes):
        tot = sizes[n]
        ex = ", ".join(f"{k} partidos extra ×{c}" for (nn, k), c in sorted(n_extra.items()) if nn == n)
        L.append(f"   · Grupos de {n}: {ex}")
        pairs = [(k, c) for (nn, k), c in post.items() if nn == n]
        for k, c in sorted(pairs, key=lambda z: -z[1])[:5]:
            L.append(f"       después del grupo: {k} en {c} de {tot} grupos")
    hours = Counter(datetime.fromtimestamp(x["t"], TZ).hour for x in parsed)
    if hours:
        L.append(f"   Horario: de {min(hours)}:00 a {max(hours)}:59")
    return L


def main():
    tg_text("🔎 Exploro el formato de las ligas. Tardará un par de minutos.")
    lines = ["EXPLORACIÓN DE LIGAS", ""]
    for name, tid in LIGAS.items():
        try:
            lines += explore(name, tid)
        except Exception as ex:
            lines.append(f"🏓 {name}: error {ex}")
        lines.append("")
    txt = "\n".join(lines)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "exploracion.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
    tg_text(txt[:3900])
    tg_file(path)


if __name__ == "__main__":
    main()
