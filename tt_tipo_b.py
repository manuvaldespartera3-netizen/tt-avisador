"""
tt_tipo_b.py - Deteccion del tipo "OFICIAL B" (solo para seguimiento).

OFICIAL B: el rival esta ELIMINADO pase lo que pase (como en el oficial) y el
pick esta obligado a ganar (si pierde, fuera), pero ganar NO le asegura pasar:
segun otros resultados o el marcador podria quedarse fuera igualmente.

No cambia nada del motor ni de los avisos: solo se usa para apuntar estos
casos y medir si aciertan tanto como los oficiales.
"""
from tt_engine import Q, O, A


def eval_b(g, m, fixed=None):
    """Para el partido m (el siguiente por jugar), devuelve {pick, vs} si es
    OFICIAL B, o None."""
    fixed = fixed or {}
    wa = [f for f in m.possible_finals() if f[0] > f[1]]
    wb = [f for f in m.possible_finals() if f[1] > f[0]]
    if not wa or not wb:
        return None

    def st(player, allowed):
        fx = dict(fixed)
        fx[m.order] = allowed
        return {s[player] for _, s in g.completions(fx)}

    for empty, need, w_need, l_need in ((m.a, m.b, wb, wa), (m.b, m.a, wa, wb)):
        if st(empty, wa + wb) != {O}:
            continue                        # el rival tiene que estar eliminado
        nw, nl = st(need, w_need), st(need, l_need)
        if Q in nw and O in nw and nl <= {O, A} and O in nl:
            return dict(pick=need, vs=empty, reason=f"{empty} ya eliminado (B)", warn=True)
    return None
