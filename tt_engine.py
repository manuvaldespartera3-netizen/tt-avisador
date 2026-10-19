"""
tt_engine.py - Motor de escenarios para grupos de 4 jugadores (TT Cup).

Reglas:
- Victoria = 2 pts, derrota = 1 pt -> ordenar por victorias.
- Empate de 2: enfrentamiento directo.
- Empate de 3+: mini-liga entre empatados (victorias), luego cociente de
  sets, luego cociente de puntos (ITTF). Los subgrupos que se separan se
  vuelven a resolver solo con sus partidos entre ellos.
- Se clasifican los 2 primeros.

Patron de pick: en un partido A-B, A "no se juega nada" (clasificado o
eliminado pase lo que pase) y B "lo necesita" (si gana pasa, si pierde fuera).
"""
from itertools import product, combinations
from dataclasses import dataclass, field
from typing import Optional

FINALS = [(3, 0), (3, 1), (3, 2), (0, 3), (1, 3), (2, 3)]
Q, O, A = "Q", "O", "A"   # clasificado, eliminado, ambiguo (decide por puntos)


@dataclass
class Match:
    a: str
    b: str
    order: int                      # 1..6 orden de juego
    time: str = ""                  # "18:15"
    final: Optional[tuple] = None   # (sets_a, sets_b) si terminado
    live: Optional[tuple] = None    # (sets_a, sets_b) parcial si en juego
    points: Optional[tuple] = None  # (puntos_a, puntos_b) si se conocen
    mid: str = ""                   # id externo
    live_pts: int = 0               # puntos jugados en el set actual (en vivo)

    def possible_finals(self):
        if self.final:
            return [self.final]
        if self.live:
            x, y = self.live
            return [f for f in FINALS if f[0] >= x and f[1] >= y]
        return FINALS


import os
# Reglas de desempate:
#   "ttcup"   -> mini-liga entre empatados, luego sets y puntos entre ellos
#   "ligapro" -> (Czech Liga Pro) sets ganados -> diferencia de sets ->
#                diferencia de puntos -> puntos totales (en todo el grupo) ->
#                enfrentamiento directo si quedan 2
TIEBREAK = os.getenv("TT_TIEBREAK", "ttcup")


def _rank(players, res):
    """res: dict frozenset({a,b}) -> (a, b, sa, sb, pa, pb). Devuelve lista de
    'tiers' (listas de jugadores) en orden; un tier con >1 = sin resolver."""
    wins = {p: 0 for p in players}
    for (a, b, sa, sb, _, _) in res.values():
        wins[a if sa > sb else b] += 1
    tiers = []
    for w in sorted(set(wins.values()), reverse=True):
        grp = [p for p in players if wins[p] == w]
        tiers.extend(_resolve_lp(grp, res) if TIEBREAK == "ligapro" else _resolve(grp, res))
    return tiers


def _resolve_lp(grp, res):
    """Desempate tipo Liga Pro con estadisticas de TODO el grupo."""
    if len(grp) == 1:
        return [grp]
    st = {p: [0, 0, 0, 0] for p in grp}      # sets ganados, dif sets, dif puntos, puntos
    pts_known = True
    for (a, b, sa, sb, pa, pb) in res.values():
        for p, my, ot, mp, op in ((a, sa, sb, pa, pb), (b, sb, sa, pb, pa)):
            if p in st:
                st[p][0] += my
                st[p][1] += my - ot
                if mp is None:
                    pts_known = False
                else:
                    st[p][2] += mp - op
                    st[p][3] += mp
    keys = [0, 1] + ([2, 3] if pts_known else [])

    def split(g, k):
        if k >= len(keys):
            if len(g) == 2:
                r = res.get(frozenset(g))
                if r:
                    win = r[0] if r[2] > r[3] else r[1]
                    return [[win], [g[1] if win == g[0] else g[0]]]
            return [g]
        vals = sorted({st[p][keys[k]] for p in g}, reverse=True)
        out = []
        for v in vals:
            sub = [p for p in g if st[p][keys[k]] == v]
            out.extend([sub] if len(sub) == 1 else split(sub, k + 1))
        return out
    return split(list(grp), 0)


def _sub(res, grp):
    g = set(grp)
    return {k: v for k, v in res.items() if k <= g}


def _resolve(grp, res, depth=0):
    if len(grp) == 1:
        return [grp]
    sub = _sub(res, grp)
    if len(grp) == 2:
        a, b = grp
        r = sub.get(frozenset(grp))
        if r is None:
            return [grp]
        win = r[0] if r[2] > r[3] else r[1]
        return [[win], [b if win == a else a]]
    # 3+: mini-liga por victorias
    mw = {p: 0 for p in grp}
    for (a, b, sa, sb, _, _) in sub.values():
        mw[a if sa > sb else b] += 1
    if len(set(mw.values())) > 1 and depth < 5:
        out = []
        for w in sorted(set(mw.values()), reverse=True):
            out.extend(_resolve([p for p in grp if mw[p] == w], res, depth + 1))
        return out
    # cociente de sets
    for key in ("sets", "points"):
        num = {p: 0 for p in grp}
        den = {p: 0 for p in grp}
        unknown = False
        for (a, b, sa, sb, pa, pb) in sub.values():
            if key == "sets":
                va, vb = sa, sb
            else:
                if pa is None:
                    unknown = True
                    break
                va, vb = pa, pb
            num[a] += va; den[a] += vb
            num[b] += vb; den[b] += va
        if unknown:
            return [grp]
        ratio = {p: (num[p] / den[p]) if den[p] else float("inf") for p in grp}
        vals = sorted(set(round(v, 9) for v in ratio.values()), reverse=True)
        if len(vals) > 1:
            out = []
            for v in vals:
                g2 = [p for p in grp if round(ratio[p], 9) == v]
                out.extend(_resolve(g2, res, depth + 1) if len(g2) < len(grp) else [g2])
            return out
    return [grp]


def _status(tiers, top=2):
    st, pos = {}, 0
    for t in tiers:
        lo, hi = pos, pos + len(t)
        s = Q if hi <= top else (O if lo >= top else A)
        for p in t:
            st[p] = s
        pos = hi
    return st


class Group:
    def __init__(self, players, matches):
        self.players = list(players)
        self.matches = sorted(matches, key=lambda m: m.order)

    def pending(self):
        return [m for m in self.matches if not m.final]

    def completions(self, fixed=None):
        """Itera (asignacion, status). fixed: dict match.order -> lista finals permitidos."""
        fixed = fixed or {}
        pend = self.pending()
        opts = [fixed.get(m.order, m.possible_finals()) for m in pend]
        for combo in product(*opts):
            res = {}
            for m in self.matches:
                f = m.final
                pts = m.points
                if not f:
                    f = combo[pend.index(m)]
                    pts = None
                pa, pb = (pts if pts else (None, None))
                res[frozenset((m.a, m.b))] = (m.a, m.b, f[0], f[1], pa, pb)
            yield dict(zip([m.order for m in pend], combo)), _status(_rank(self.players, res))

    # ---------- analisis del patron ----------
    def _eval_match(self, m, fixed):
        """Para el partido m con condiciones previas 'fixed', devuelve pick o None."""
        wa = [f for f in m.possible_finals() if f[0] > f[1]]
        wb = [f for f in m.possible_finals() if f[1] > f[0]]
        def statuses(player, allowed):
            fx = dict(fixed); fx[m.order] = allowed
            return {st[player] for _, st in self.completions(fx)}
        sa_w, sa_l = statuses(m.a, wa), statuses(m.a, wb)
        sb_w, sb_l = statuses(m.b, wb), statuses(m.b, wa)
        res = None
        for (empty, ew, el, need, nw, nl) in ((m.a, sa_w, sa_l, m.b, sb_w, sb_l),
                                             (m.b, sb_w, sb_l, m.a, sa_w, sa_l)):
            allst = ew | el
            empty_ok = allst in ({Q}, {O})
            need_ok = (Q in nw) and nw <= {Q, A} and nl <= {O, A} and (O in nl)
            if empty_ok and need_ok:
                warn = (A in nw) or (A in nl)
                res = dict(pick=need, vs=empty,
                           reason=f"{empty} ya {'clasificado' if allst == {Q} else 'eliminado'}",
                           warn=warn)
        return res

    def analyze(self):
        """Devuelve lista de dicts: match, conditions (lista de (match, desc)), pick..."""
        out = []
        pend = self.pending()
        for i, m in enumerate(pend):
            before = pend[:i]
            # condiciones: ganador de cada partido previo pendiente
            choices = []
            for pm in before:
                fins = pm.possible_finals()
                choices.append([("A", [f for f in fins if f[0] > f[1]]),
                                ("B", [f for f in fins if f[1] > f[0]])])
            for combo in product(*choices) if choices else [()]:
                if any(not c[1] for c in combo):
                    continue
                fixed = {pm.order: c[1] for pm, c in zip(before, combo)}
                r = self._eval_match(m, fixed)
                conds = [(pm, pm.a if c[0] == "A" else pm.b) for pm, c in zip(before, combo)]
                if r:
                    out.append(dict(match=m, conds=conds, partial=False, **r))
                    continue
                # probar por marcador exacto (sets) del partido previo inmediato
                if before:
                    last = before[-1]
                    hits = []
                    for f in fixed[last.order]:
                        fx = dict(fixed); fx[last.order] = [f]
                        r2 = self._eval_match(m, fx)
                        if r2:
                            hits.append((f, r2))
                    if hits and len({h[1]["pick"] for h in hits}) == 1:
                        r2 = hits[0][1]
                        scores = ", ".join(f"{f[0]}:{f[1]}" for f, _ in hits)
                        out.append(dict(match=m, conds=conds, partial=scores, **r2))
        return out


def format_alerts(group, label=""):
    lines = []
    for r in group.analyze():
        m = r["match"]
        when = f" {m.time}" if m.time else ""
        head = f"✅ PICK {r['pick']} (vs {r['vs']}{when})"
        if r["conds"]:
            cond = " y ".join(f"gana {w} el {pm.a}-{pm.b}" for pm, w in r["conds"])
            if r["partial"]:
                cond += f" ({r['partial']})"
            head = f"⏳ Si {cond} → PICK {r['pick']} (vs {r['vs']}{when})"
        if r["warn"]:
            head += " ⚠️ puede decidir por puntos"
        lines.append(head)
    if not lines:
        lines.append("❌ SIN PICK")
    title = f"🏓 {label}\n" if label else ""
    return title + "\n".join(lines)
