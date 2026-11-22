"""
tt_grupos5.py - COMPROBAR LOS GRUPOS DE 5 DE LIGA PRO CON DATOS REALES.

Baja de BetsAPI los ultimos DIAS dias de Liga Pro y, dia a dia:
  1. Forma los grupos como el bot ANTES (solo grupos de 4) y AHORA (con los
     grupos de 5). Comprueba que TODOS los grupos de 4 salen exactamente iguales.
  2. Cuenta los grupos de 5 que ahora se forman (antes se descartaban).
  3. Para cada grupo de 5 terminado, mira quien jugo de verdad la FINAL (los 2
     primeros) y si el bot lo calcula igual (reglamento 2.6.1 / 2.7).
  4. Simula los PICKS OFICIALES que el bot habria dado en esos grupos de 5
     (mismo calculo que el avisador) y su resultado real, con la cuota de Bet365.

Variables: BETSAPI_TOKEN, DIAS (60), TELEGRAM_TOKEN, MY_CHAT_ID
No cambia nada del avisador: solo lee y hace el informe.
"""
import os

os.environ.setdefault("TT_TOURNAMENT_ID", "19039")
os.environ["TT_TIEBREAK"] = "ligapro"
os.environ.setdefault("STATE_FILE", "/tmp/state_g5.json")
os.environ.setdefault("PICKS_FILE", "/tmp/picks_g5.csv")
os.environ["TEST_MODE"] = "0"
os.environ["ESTRELLAS"] = "0"
os.environ["FILTRO_NOCHE"] = "0"

from collections import Counter, defaultdict
from datetime import datetime, timedelta

import tt_engine
tt_engine.TIEBREAK = "ligapro"
import tt_feed
from tt_feed import parse, TZ
import tt_alerts as A
from tt_engine import Group, Match
from tt_backtest import tg_text, OUT
from tt_desempate import bajar_betsapi

LIM = 7 * 3600          # margen para los grupos que cruzan la medianoche


def firma(g):
    return (tuple(sorted(g.players)), tuple(sorted(m.mid for m in g.matches)),
            bool(getattr(g, "dudoso", False)), getattr(g, "quinto", None))


def grupos_por_dia(parsed):
    """Grupos ANTES (solo 4) y AHORA (con 5), dia a dia (Madrid)."""
    dias = defaultdict(list)
    for x in parsed:
        dias[datetime.fromtimestamp(x["t"], TZ).date()].append(x)
    antes, ahora = [], []
    for d in sorted(dias):
        ini = datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp()
        fin = ini + 86400
        trozo = [x for x in parsed if ini <= x["t"] < fin + LIM]
        evs = [x["_ev"] for x in trozo]
        ts = {x["id"]: x["t"] for x in trozo}
        for flag, dest in ((False, antes), (True, ahora)):
            tt_feed.GRUPOS5 = flag
            gs, _ = tt_feed.build_groups(evs)
            dest.extend(g for g in gs if ini <= min(ts[m.mid] for m in g.matches) < fin)
    tt_feed.GRUPOS5 = True
    return antes, ahora


def finalistas(g, parsed):
    """Los 2 que jugaron la final de verdad (o None si no se sabe)."""
    pts = {p: 0 for p in g.players}
    for m in g.matches:
        w, l = (m.a, m.b) if m.final[0] > m.final[1] else (m.b, m.a)
        pts[w] += 2
        pts[l] += 1
    ids = {m.mid for m in g.matches}
    tfin = max(x["t"] for x in parsed if x["id"] in ids)
    despues = [x for x in parsed if tfin < x["t"] <= tfin + 3 * 3600 and x["id"] not in ids
               and x["a"] in pts and x["b"] in pts]
    pares = []
    for x in despues:
        pr = frozenset((x["a"], x["b"]))
        if all(not (pr & q) for q in pares):
            pares.append(pr)
        if len(pares) == 2:
            break
    if not pares:
        return None
    if len(pares) == 1:
        # solo se ve un partido: es la final si son los 2 de mas puntos
        return None
    s0, s1 = (sum(pts[p] for p in q) for q in pares)
    if s0 == s1:
        return None
    return set(pares[0] if s0 > s1 else pares[1])


def top2_bot(g):
    res = {frozenset((m.a, m.b)): (m.a, m.b, m.final[0], m.final[1],
                                   *(m.points if m.points else (None, None)))
           for m in g.matches}
    top, pos = [], 0
    for t in tt_engine._rank(g.players, res):
        if pos + len(t) <= 2:
            top += t
        elif pos < 2:
            return None
        pos += len(t)
    return set(top)


def picks_de(g):
    """Picks OFICIALES que el bot habria confirmado (como el avisador: se mira
    cada partido de los 3 ultimos cuando ya han acabado los anteriores)."""
    out = []
    n = len(g.matches)
    for j in range(n - 3, n):
        ms = [Match(x.a, x.b, x.order, time=x.time, final=x.final if i < j else None,
                    points=x.points if i < j else None, mid=x.mid)
              for i, x in enumerate(g.matches)]
        gx = Group(g.players, ms)
        m = gx.matches[j]
        for pick in (m.a, m.b):
            r = A.safe_pick(gx, m, pick, {})
            if r and A.kind(r) == "OFICIAL":
                real = g.matches[j]
                gana = (real.final[0] > real.final[1]) == (real.a == pick)
                out.append(dict(pick=pick, vs=r["vs"], mid=m.mid, gana=gana,
                                local=(m.a == pick), hora=m.time, j=j + 1))
    return out


def main():
    L = ["👥 GRUPOS DE 5 · LIGA PRO (datos reales)", ""]
    evs, origen = bajar_betsapi()
    parsed = []
    for e in evs:
        p = parse(e)
        if p:
            p["_ev"] = e
            parsed.append(p)
    parsed.sort(key=lambda x: (x["t"], x["id"]))
    if not parsed:
        L.append(f"❌ No hay datos ({origen}).")
        return fin(L)
    L.append(f"Datos: {origen} · {len(parsed)} partidos")
    antes, ahora = grupos_por_dia(parsed)

    # 1) los grupos de 4 no cambian
    f_antes = {firma(g) for g in antes if not getattr(g, "quinto", None)}
    f_ahora = {firma(g) for g in ahora if not getattr(g, "cinco", False)}
    iguales = f_antes <= f_ahora
    L.append("")
    L.append(f"1) Grupos de 4: {len(f_antes)} antes · "
             + ("✅ TODOS EXACTAMENTE IGUALES ahora" if iguales else
                f"❌ cambian {len(f_antes - f_ahora)} (avisa a Claude)"))

    # 2) grupos de 5
    cincos = [g for g in ahora if getattr(g, "cinco", False)]
    dud_antes = sum(1 for g in antes if getattr(g, "quinto", None))
    dud_ahora = sum(1 for g in ahora if getattr(g, "quinto", None))
    L.append(f"2) Grupos de 5 formados: {len(cincos)} "
             f"(los grupos 'dudosos por 5.º jugador' pasan de {dud_antes} a {dud_ahora})")
    if cincos:
        dur = sorted((max(x["t"] for x in parsed if x["id"] in {m.mid for m in g.matches})
                      - min(x["t"] for x in parsed if x["id"] in {m.mid for m in g.matches})) / 3600
                     for g in cincos)
        L.append(f"   Duración típica de un grupo de 5: {dur[len(dur) // 2]:.1f} h")

    # 3) desempate / quien pasa
    ok = tot = emp_ok = emp_tot = sin = 0
    for g in cincos:
        if any(not m.final for m in g.matches):
            continue
        real = finalistas(g, parsed)
        bot = top2_bot(g)
        if real is None or bot is None:
            sin += 1
            continue
        pts = Counter()
        for m in g.matches:
            w, l = (m.a, m.b) if m.final[0] > m.final[1] else (m.b, m.a)
            pts[w] += 2
            pts[l] += 1
        orden = sorted(pts.values(), reverse=True)
        empate = orden[1] == orden[2]
        tot += 1
        ok += bot == real
        if empate:
            emp_tot += 1
            emp_ok += bot == real
    L.append(f"3) ¿Quién pasa a la final? El bot acierta {ok}/{tot}"
             + (f" ({100 * ok / tot:.0f}%)" if tot else "")
             + f" · con empate a puntos en el corte: {emp_ok}/{emp_tot}"
             + (f" · sin final clara en los datos: {sin}" if sin else ""))

    # 4) picks
    import tt_betsapi as B
    picks = []
    for g in cincos:
        if any(not m.final for m in g.matches):
            continue
        for p in picks_de(g):
            p["fecha"] = datetime.fromtimestamp(
                min(x["t"] for x in parsed if x["id"] == p["mid"]), TZ).strftime("%d/%m")
            try:
                c = B.cuota_prepartido(p["mid"])
            except Exception:
                c = None
            p["cuota"] = (c[0] if p["local"] else c[1]) if c else None
            picks.append(p)
    n = len(picks)
    g_ = sum(p["gana"] for p in picks)
    con = [p for p in picks if p["cuota"]]
    u = sum((p["cuota"] - 1) if p["gana"] else -1 for p in con)
    L.append(f"4) PICKS OFICIALES que se habrían dado en grupos de 5: {n}"
             + (f" · aciertos {g_}/{n} ({100 * g_ / n:.0f}%)" if n else ""))
    if con:
        L.append(f"   Con cuota de Bet365 ({len(con)}): {u:+.2f} u · yield {100 * u / len(con):+.0f}%"
                 f" · cuota media {sum(p['cuota'] for p in con) / len(con):.2f}")
    for p in picks[-12:]:
        L.append(f"   {p['fecha']} {p['hora']} {'✅' if p['gana'] else '❌'} {p['pick']} vs {p['vs']}"
                 + (f" @{p['cuota']:.2f}" if p["cuota"] else "") + f" (partido {p['j']}/10)")
    L.append("")
    if iguales:
        L.append("CONCLUSIÓN: los grupos de 4 no cambian nada; los de 5 se suman a Liga Pro.")
    else:
        L.append("CONCLUSIÓN: ⚠️ algo cambia en los grupos de 4: NO activar todavía.")
    fin(L)


def fin(L):
    txt = "\n".join(L)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "grupos5.txt"), "w", encoding="utf-8") as f:
        f.write(txt + "\n")
    tg_text(txt[:3900])


if __name__ == "__main__":
    main()
