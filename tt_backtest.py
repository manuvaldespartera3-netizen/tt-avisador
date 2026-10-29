"""
tt_backtest.py - Analisis historico de los picks de TT Cup.

Descarga los ultimos dias de TT Cup (1 consulta por dia), rehace todos los
grupos y simula EXACTAMENTE lo que habria avisado el bot: cada PICK OFICIAL y
POTENCIAL, con su resultado real. Despues mide filtros y manda el informe por
Telegram (mensaje + archivos) y lo deja en la carpeta backtest/ del repositorio.

Variables:
  ZYLA_API_KEY, TELEGRAM_TOKEN, MY_CHAT_ID  (los mismos secretos del avisador)
  BT_DAYS       dias de picks a analizar (por defecto 90)
  BT_WARMUP     dias extra anteriores solo para Elo/H2H (por defecto 45)

Elo y H2H se calculan SOLO con partidos anteriores a cada pick (sin mirar el
futuro), igual que si lo hubieras consultado en ese momento.
"""
import bisect
import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

import tt_engine
import tt_feed
from tt_feed import parse, _call, _events, URL_TOURNAMENT, TOURNAMENT_ID, TZ, WINDOW_H
from tt_engine import Group, Match
from tt_tipo_b import eval_b

DAYS = int(os.getenv("BT_DAYS", "90"))
WARMUP = int(os.getenv("BT_WARMUP", "45"))
LIGAS = {"15005": "TT Cup", "19039": "Czech Liga Pro", "15004": "Setka Cup",
         "19041": "TT Elite Series", "9410": "Challenger Series"}
LIGA = LIGAS.get(str(TOURNAMENT_ID), str(TOURNAMENT_ID))
if str(TOURNAMENT_ID) == "19039":
    tt_engine.TIEBREAK = "ligapro"      # reglas de desempate de Liga Pro
# TT Cup en backtest/ (como siempre); las demas ligas en su propia carpeta
OUT = os.getenv("BT_OUT", "backtest" if str(TOURNAMENT_ID) == "15005"
                else f"backtest/{LIGA.lower().replace(' ', '_')}")
TG = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT = os.getenv("MY_CHAT_ID", "").strip()
CACHE = os.path.join(OUT, "cache")
ELO_K = 24
URL_ODDS = os.getenv(
    "ZYLA_URL_ODDS",
    "https://zylalabs.com/api/12787/sofascore+-+live+api/25097/get+match+odds")
MIN_N = 30
# pausa entre consultas: 1,2 s = 50 por minuto. Deja de sobra el resto del
# limite (120/min) para el avisador, asi los picks no se retrasan.
PACE = float(os.getenv("BT_PACE", "1.2"))            # minimo de picks para fiarse de una fila


# ------------------------------------------------------------------ datos
def fetch_day(d):
    """Partidos de un dia; se guardan en cache para no gastar consultas dos veces."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{d}.json")
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if os.path.exists(path) and d < today:
        with open(path) as f:
            return json.load(f)
    for intento in range(6):
        try:
            evs = _events(_call(URL_TOURNAMENT, unique_tournament_id=TOURNAMENT_ID, date=d))
            break
        except Exception as ex:
            print("WARN", d, ex)
            time.sleep(10 * (intento + 1))
    else:
        return []
    with open(path, "w") as f:
        json.dump(evs, f)
    time.sleep(PACE)       # < 120 consultas/minuto
    return evs


def load_all():
    end = datetime.now(TZ).date() - timedelta(days=1)      # hoy no (incompleto)
    start = end - timedelta(days=DAYS + WARMUP - 1)
    evs = {}
    d = start
    while d <= end:
        for e in fetch_day(d.strftime("%Y-%m-%d")):
            evs[e.get("id")] = e
        d += timedelta(days=1)
    parsed = [p for p in (parse(e) for e in evs.values()) if p and p["final"]]
    parsed.sort(key=lambda x: (x["t"], x["id"]))
    first_pick_ts = datetime.combine(end - timedelta(days=DAYS - 1),
                                     datetime.min.time(), TZ).timestamp()
    return parsed, first_pick_ts, start, end


# ---------------------------------------------------------------- cuotas
def _dec(v):
    """'4/9' -> 1.44 · '1.44' -> 1.44 · None si no se entiende."""
    if v in (None, ""):
        return None
    if isinstance(v, dict):          # formato de tu API: {"decimal": 1.8, "fractional": "4/5"}
        v = v.get("decimal") or v.get("fractional")
        if v in (None, ""):
            return None
    try:
        v = str(v)
        if "/" in v:
            a, b = v.split("/")
            return round(1 + float(a) / float(b), 3)
        f = float(v)
        return round(f, 3) if f > 1 else None
    except (ValueError, ZeroDivisionError):
        return None


def _markets(o):
    """Busca la lista de mercados (dicts con 'choices') en cualquier JSON."""
    if isinstance(o, dict):
        if isinstance(o.get("markets"), list):
            return o["markets"]
        for v in o.values():
            r = _markets(v)
            if r:
                return r
    elif isinstance(o, list):
        if o and isinstance(o[0], dict) and "choices" in o[0]:
            return o
        for v in o:
            r = _markets(v)
            if r:
                return r
    return []


_odds_ejemplo = []


def fetch_odds(mid):
    """(cuota_local_inicial, cuota_local_final, cuota_visit_inicial, cuota_visit_final)."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"odds_{mid}.json")
    data = None
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
    else:
        for intento in range(4):
            try:
                data = _call(URL_ODDS, match_id=mid)
                break
            except Exception as ex:
                print("WARN odds", mid, ex)
                if "404" in str(ex):
                    data = {}
                    break
                time.sleep(8 * (intento + 1))
        time.sleep(PACE)
        with open(path, "w") as f:
            json.dump(data, f)
    if data and len(_odds_ejemplo) < 2:
        _odds_ejemplo.append(data)
    mk = _markets(data or {})
    # mercado ganador del partido: el de 2 opciones "1"/"2" (o el primero)
    best = None
    for m in mk:
        names = [str(c.get("name", "")) for c in m.get("choices", [])]
        if sorted(names) == ["1", "2"]:
            best = m
            if "full" in str(m.get("marketName", "")).lower() or m.get("marketId") == 1:
                break
    if not best:
        return None
    ch = {str(c.get("name")): c for c in best["choices"]}

    def pair(c):
        fin = _dec(c.get("fractionalValue")) or _dec(c.get("decimalValue")) or _dec(c.get("value"))
        ini = (_dec(c.get("initialFractionalValue")) or _dec(c.get("initialDecimalValue"))
               or _dec(c.get("initialValue")) or fin)
        return ini, fin
    hi, hf = pair(ch["1"])
    ai, af = pair(ch["2"])
    if not (hf and af):
        return None
    return hi, hf, ai, af


def add_odds(rows):
    for r in rows:
        o = fetch_odds(r["match_id"])
        if not o:
            r["cuota_inicial"] = r["cuota_final"] = ""
            continue
        hi, hf, ai, af = o
        ini, fin = (hi, hf) if r["pick_local"] else (ai, af)
        r["cuota_inicial"], r["cuota_final"] = ini, fin
        r["beneficio"] = round(fin - 1, 3) if r["gana"] else -1.0
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "ejemplo_cuotas.json"), "w") as f:
        json.dump(_odds_ejemplo, f, indent=1)


# ---------------------------------------------------------- grupos (rapido)
def build_groups_fast(parsed):
    """Misma regla que el avisador: los 6 PRIMEROS partidos todos-contra-todos
    entre 4 jugadores dentro de 3,5 h (las Final games nunca entran)."""
    W = WINDOW_H * 3600
    ts = [x["t"] for x in parsed]
    used, groups = set(), []
    for i, m in enumerate(parsed):
        if m["id"] in used:
            continue
        a, b, t = m["a"], m["b"], m["t"]
        hi = bisect.bisect_right(ts, t + W)
        pool = [x for x in parsed[i:hi] if x["id"] not in used]
        first = {}
        opp = defaultdict(set)
        for x in pool:
            k = frozenset((x["a"], x["b"]))
            if k not in first:
                first[k] = x
            opp[x["a"]].add(x["b"])
            opp[x["b"]].add(x["a"])
        cands = sorted((opp[a] & opp[b]) - {a, b})
        found = None
        for ci, c in enumerate(cands):
            for d in cands[ci + 1:]:
                if frozenset((c, d)) in first:
                    found = (c, d)
                    break
            if found:
                break
        if not found:
            continue
        c, d = found
        four = (a, b, c, d)
        ms = [m if {p, q} == {a, b} else first[frozenset((p, q))]
              for p, q in ((a, b), (a, c), (a, d), (b, c), (b, d), (c, d))]
        used |= {x["id"] for x in ms}
        ms.sort(key=lambda x: (x["t"], x["id"]))
        groups.append((list(four), ms))
    return groups


# ------------------------------------------------------------- Elo y H2H
def elo_snapshots(parsed):
    """Elo de cada jugador JUSTO ANTES de cada partido (id -> (elo_a, elo_b))."""
    r = defaultdict(lambda: 1500.0)
    played = defaultdict(int)
    snap = {}
    for x in parsed:
        a, b = x["a"], x["b"]
        snap[x["id"]] = (r[a], r[b], played[a], played[b])
        ea = 1 / (1 + 10 ** ((r[b] - r[a]) / 400))
        sa = 1.0 if x["final"][0] > x["final"][1] else 0.0
        r[a] += ELO_K * (sa - ea)
        r[b] -= ELO_K * (sa - ea)
        played[a] += 1
        played[b] += 1
    return snap


def h2h_before(index, p, q, t):
    """Victorias de p y de q entre ellos antes del instante t."""
    wp = wq = 0
    for x in index.get(frozenset((p, q)), []):
        if x["t"] >= t:
            break
        w = x["a"] if x["final"][0] > x["final"][1] else x["b"]
        if w == p:
            wp += 1
        else:
            wq += 1
    return wp, wq


# ------------------------------------------------------- simulacion de picks
def to_match(x, order, finished):
    return Match(x["a"], x["b"], order,
                 time=datetime.fromtimestamp(x["t"], TZ).strftime("%H:%M"),
                 final=x["final"] if finished else None,
                 points=x["points"] if finished else None, mid=x["id"])


def simulate(players, ms):
    """Devuelve los picks que el avisador habria confirmado en este grupo."""
    # Con 3 o menos partidos jugados nadie puede estar ya clasificado/eliminado,
    # asi que solo se miran los partidos 4, 5 y 6 (como hace el avisador: el
    # pick se confirma cuando ha terminado el partido anterior).
    out = []
    for j in range(3, 6):
        g = Group(players, [to_match(x, i + 1, i < j) for i, x in enumerate(ms)])
        m = g.matches[j]
        r = g._eval_match(m, {})
        if not r:
            rb = eval_b(g, m)                 # OFICIAL B (solo para medir)
            if rb:
                out.append(dict(j=j, r=rb, antic=False, tipo="OFICIAL B"))
            continue
        # anticipado = el pick ya era seguro antes de acabar el partido anterior,
        # ganase quien ganase ese partido
        g0 = Group(players, [to_match(x, i + 1, i < j - 1) for i, x in enumerate(ms)])
        pm = g0.matches[j - 1]
        antic = True
        for side in (0, 1):
            fins = [f for f in pm.possible_finals() if (f[0] > f[1]) == (side == 0)]
            r0 = g0._eval_match(g0.matches[j], {pm.order: fins})
            if not r0 or r0["pick"] != r["pick"]:
                antic = False
                break
        out.append(dict(j=j, r=r, antic=antic, tipo=None))
    return out


def group_state(players, ms, j):
    wins = {p: 0 for p in players}
    sets = {p: 0 for p in players}
    for x in ms[:j]:
        sa, sb = x["final"]
        wins[x["a"] if sa > sb else x["b"]] += 1
        sets[x["a"]] += sa
        sets[x["b"]] += sb
    return wins, sets


def run():
    parsed, first_pick_ts, start, end = load_all()
    print(f"{len(parsed)} partidos terminados del {start} al {end}")
    snap = elo_snapshots(parsed)
    index = defaultdict(list)
    for x in parsed:
        index[frozenset((x["a"], x["b"]))].append(x)
    groups = build_groups_fast(parsed)
    rows, dudosos, n_groups = [], 0, 0
    for players, ms in groups:
        if ms[0]["t"] < first_pick_ts:
            continue
        n_groups += 1
        if any(x.get("dudoso") for x in ms):
            dudosos += 1
            continue
        for s in simulate(players, ms):
            r, j = s["r"], s["j"]
            x = ms[j]
            pick, rival = r["pick"], r["vs"]
            pick_home = pick == x["a"]
            sa, sb = x["final"]
            sp, sr = (sa, sb) if pick_home else (sb, sa)
            ea, eb, na, nb = snap[x["id"]]
            ep, er = (ea, eb) if pick_home else (eb, ea)
            npk, nrv = (na, nb) if pick_home else (nb, na)
            hp, hr = h2h_before(index, pick, rival, x["t"])
            wins, sets = group_state(players, ms, j)
            dt = datetime.fromtimestamp(x["t"], TZ)
            rows.append(dict(
                fecha=dt.strftime("%Y-%m-%d"), hora=dt.strftime("%H:%M"),
                tipo=s.get("tipo") or ("OFICIAL" if "eliminado" in r["reason"] else "POTENCIAL"),
                pick=pick, rival=rival, partido=j + 1, pick_local=1 if pick_home else 0,
                gana=1 if sp > sr else 0, marcador=f"{sp}-{sr}",
                gana_por_2=1 if sp == 3 and sr <= 1 else 0,
                aviso_puntos=1 if r["warn"] else 0, anticipado=1 if s["antic"] else 0,
                elo_pick=round(ep), elo_rival=round(er), elo_dif=round(ep - er),
                prob_elo=round(1 / (1 + 10 ** ((er - ep) / 400)), 3),
                partidos_previos_pick=npk, partidos_previos_rival=nrv,
                h2h_pick=hp, h2h_rival=hr,
                victorias_pick_grupo=wins[pick], victorias_rival_grupo=wins[rival],
                sets_rival_grupo=sets[rival], match_id=x["id"],
                dif_puntos=(((x["points"][0] - x["points"][1]) * (1 if pick_home else -1))
                            if x.get("points") else "")))
    rows.sort(key=lambda z: (z["fecha"], z["hora"]))
    return rows, dict(partidos=len(parsed), grupos=n_groups, dudosos=dudosos,
                      desde=(end - timedelta(days=DAYS - 1)), hasta=end)


# ------------------------------------------------------------- estadistica
def wilson_low(g, n, z=1.96):
    if not n:
        return 0.0
    p = g / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def stat(rows, key="gana"):
    n = len(rows)
    g = sum(r[key] for r in rows)
    return n, g, (g / n if n else 0.0), wilson_low(g, n)


def fmt_row(label, rows, half_split, key="gana", expl=""):
    """Dos lineas cortas (se leen bien en el movil)."""
    head = f"▶ {label}" + (f" — {expl}" if expl else "")
    n, g, p, lo = stat(rows, key)
    if not n:
        return f"{head}\n   sin casos"
    a = [r for r in rows if r["fecha"] < half_split]
    b = [r for r in rows if r["fecha"] >= half_split]
    pa, pb = stat(a, key)[2], stat(b, key)[2]
    if n < MIN_N:
        estable = "pocos casos, no fiable"
    elif len(a) >= 15 and len(b) >= 15:
        estable = "estable ✅" if abs(pa - pb) <= 0.07 else "inestable ⚠️"
    else:
        estable = ""
    exp = sum(r["prob_elo"] for r in rows) / n
    txt = (f"{head}\n   {n} picks · {g}✅ · {p*100:.0f}% (Elo {exp*100:.0f}%) · "
           f"cuota ≥{1/p if p else 0:.2f} · segura ≥{1/lo if lo else 99:.2f}\n"
           f"   mitades {pa*100:.0f}% / {pb*100:.0f}% {estable}")
    if key == "gana":
        txt += "\n" + money_line(rows)
    return txt


def money(rows):
    """Apostando 1 unidad a cada pick con la cuota de justo antes de empezar."""
    con = [r for r in rows if r.get("cuota_final")]
    if not con:
        return None
    ben = sum(r["beneficio"] for r in con)
    media = sum(r["cuota_final"] for r in con) / len(con)
    return len(con), media, ben, ben / len(con)


def money_line(rows):
    m = money(rows)
    if not m:
        return "   💶 sin cuotas"
    n, media, ben, roi = m
    ic = "🟢" if roi > 0.03 else ("🔴" if roi < -0.03 else "⚪")
    return (f"   💶 {ic} cuota media {media:.2f} · beneficio {ben:+.1f} u · "
            f"rentabilidad {roi*100:+.1f}% ({n} con cuota)")


FILTERS = [
    ("Todos", "todos los picks de este tipo", lambda r: True),
    ("Sin aviso de puntos", "quitando los que podían decidirse por puntos", lambda r: not r["aviso_puntos"]),
    ("Pick favorito por Elo", "el pick tiene más Elo que el rival", lambda r: r["elo_dif"] > 0),
    ("Pick NO favorito por Elo", "el pick tiene menos Elo que el rival", lambda r: r["elo_dif"] <= 0),
    ("Elo pick +100 o más", "el pick es claramente mejor jugador", lambda r: r["elo_dif"] >= 100),
    ("Elo pick -100 o menos", "el pick es claramente peor jugador", lambda r: r["elo_dif"] <= -100),
    ("H2H a favor", "el pick ganó más enfrentamientos previos", lambda r: r["h2h_pick"] > r["h2h_rival"]),
    ("H2H en contra", "el rival ganó más enfrentamientos previos", lambda r: r["h2h_pick"] < r["h2h_rival"]),
    ("Sin H2H / empatado", "no hay historial o está igualado", lambda r: r["h2h_pick"] == r["h2h_rival"]),
    ("Rival con 0 victorias", "el rival no ha ganado nada en el grupo", lambda r: r["victorias_rival_grupo"] == 0),
    ("Rival con 1+ victorias", "el rival ha ganado algún partido", lambda r: r["victorias_rival_grupo"] >= 1),
    ("Rival ≤2 sets en el grupo", "el rival llega muy flojo (casi sin sets)", lambda r: r["sets_rival_grupo"] <= 2),
    ("Pick anticipado", "el pick se sabía un partido antes (tiempo de sobra)", lambda r: r["anticipado"]),
    ("Pick de último momento", "se decidió con el partido justo anterior", lambda r: not r["anticipado"]),
    ("Último partido del grupo", "el pick es el 6.º partido", lambda r: r["partido"] == 6),
    ("Mañana (antes de 14:00)", "", lambda r: r["hora"] < "14:00"),
    ("Tarde (14:00-19:59)", "", lambda r: "14:00" <= r["hora"] < "20:00"),
    ("Noche (20:00 o más)", "", lambda r: r["hora"] >= "20:00"),
]


def best_combos(rows, split):
    """Combinaciones de 2 filtros: se eligen con el periodo 1 y se comprueban en el 2."""
    train = [r for r in rows if r["fecha"] < split]
    test = [r for r in rows if r["fecha"] >= split]
    out = []
    fl = FILTERS[1:]
    for i in range(len(fl)):
        for k in range(i + 1, len(fl)):
            f = lambda r, a=fl[i][2], b=fl[k][2]: a(r) and b(r)
            tr = [r for r in train if f(r)]
            te = [r for r in test if f(r)]
            if len(tr) < MIN_N or len(te) < 10:
                continue
            ntr, gtr, ptr, lotr = stat(tr)
            nte, gte, pte, _ = stat(te)
            out.append((lotr, f"{fl[i][0]} + {fl[k][0]}", ntr, ptr, nte, pte))
    out.sort(reverse=True)
    return out[:6]


def report(rows, info):
    fechas = sorted({r["fecha"] for r in rows})
    half = fechas[len(fechas) // 2] if fechas else ""
    split = fechas[int(len(fechas) * 0.6)] if fechas else ""
    L = []
    L.append(f"INFORME HISTÓRICO {LIGA.upper()}")
    L.append(f"Periodo: {info['desde']} a {info['hasta']} · {info['grupos']} grupos analizados"
             f" · {info['dudosos']} descartados por datos dudosos")
    L.append("")
    L.append("CÓMO SE LEE")
    L.append("• % = acierto real. (Elo X%) = lo que se esperaría solo por nivel.")
    L.append("  Si el real supera al Elo, la motivación aporta algo más.")
    L.append("• cuota ≥ = cuota media mínima para no perder dinero.")
    L.append("• segura ≥ = cuota mínima con margen (95%): cobrando eso o más,")
    L.append("  es muy probable que sea rentable.")
    L.append(f"• mitades = acierto antes / después del {half}.")
    L.append("  estable ✅ = se repite · inestable ⚠️ = puede ser casualidad.")
    L.append(f"• Menos de {MIN_N} picks = no fiable.")
    for tipo in ("OFICIAL", "OFICIAL B", "POTENCIAL"):
        rs = [r for r in rows if r["tipo"] == tipo]
        L.append("")
        L.append("=" * 60)
        L.append({"OFICIAL": "🟢🟢 PICK OFICIAL", "OFICIAL B": "🔵 PICK OFICIAL B (rival eliminado, ganar no asegura pasar)",
                  "POTENCIAL": "🟡 PICK POTENCIAL"}[tipo])
        L.append("=" * 60)
        for name, expl, f in FILTERS:
            L.append(fmt_row(name, [r for r in rs if f(r)], half, expl=expl))
        L.append(fmt_row("Mercado hándicap -1,5 sets", rs, half, key="gana_por_2",
                         expl="el pick gana 3-0 o 3-1 (el % Elo no aplica aquí)"))
        combos = best_combos(rs, split)
        if combos:
            L.append("")
            L.append(f"  Mejores combinaciones (elegidas hasta {split}, comprobadas después):")
            for lo, name, ntr, ptr, nte, pte in combos:
                ok = "✅" if pte >= ptr - 0.05 else "❌ no se mantiene"
                L.append(f"   · {name}: {ptr*100:.0f}% ({ntr}) → después {pte*100:.0f}% ({nte}) {ok}")
    L.append("")
    L.extend(handicap_report(rows))
    L.append("")
    L.append("VEREDICTO")
    L.extend(verdict(rows))
    return "\n".join(L)


# ------------------------------------------------------ handicap de puntos
LINEAS = (1.5, 2.5, 3.5, 4.5, 5.5, 6.5)


def _hc_rows(rows):
    return [r for r in rows if r.get("dif_puntos") not in ("", None)]


def hc_line(label, rows, ln):
    """% de picks que cubren -ln puntos y cuota minima para que renten."""
    rs = _hc_rows(rows)
    n = len(rs)
    if n < 10:
        return f"   {label} -{ln}: pocos casos ({n})"
    g = sum(1 for r in rs if r["dif_puntos"] > ln)
    p, lo = g / n, wilson_low(g, n)
    eq = f"{1 / p:.2f}" if p else "—"
    seg = f"{1 / lo:.2f}" if lo else "—"
    return f"   -{ln} puntos: {p * 100:.0f}% ({g}/{n}) · cuota ≥{eq} · segura ≥{seg}"


def hc_compare(rows, cuota_hc=1.66, ln=4.5, umbral=1.40):
    """Picks con cuota de ganador < umbral: ¿mejor ganador o handicap -ln a cuota_hc?"""
    rs = [r for r in _hc_rows(rows) if r.get("cuota_final") and float(r["cuota_final"]) < umbral]
    u = f"{umbral:.2f}".replace(".", ",")
    if len(rs) < 10:
        return [f"   Picks con cuota < {u}: pocos casos ({len(rs)}) para comparar"]
    n = len(rs)
    gw = sum(r["gana"] for r in rs)
    media = sum(float(r["cuota_final"]) for r in rs) / n
    ben_w = sum((float(r["cuota_final"]) - 1) if r["gana"] else -1 for r in rs)
    gh = sum(1 for r in rs if r["dif_puntos"] > ln)
    ben_h = gh * (cuota_hc - 1) - (n - gh)
    return [f"   Picks con cuota de ganador < {u}: {n}",
            f"   · Apostando a ganador (cuota media {media:.2f}): {gw}/{n} = {gw / n * 100:.0f}% → "
            f"{ben_w:+.1f} u ({ben_w / n * 100:+.1f}%)",
            f"   · Apostando -{ln} puntos a {cuota_hc:.2f}: {gh}/{n} = {gh / n * 100:.0f}% → "
            f"{ben_h:+.1f} u ({ben_h / n * 100:+.1f}%)"]


def handicap_report(rows):
    L = ["=" * 60, "🎯 HÁNDICAP DE PUNTOS (solo OFICIAL)", "=" * 60,
         "• -4,5 = el pick tiene que sacar 5 puntos o más de diferencia",
         "  sumando todos los sets. Si pierde el partido, casi nunca cubre.",
         "• cuota ≥ = la que necesitas como mínimo en ese hándicap."]
    of = [r for r in rows if r["tipo"] == "OFICIAL"]
    star = [r for r in of if not r["aviso_puntos"] and r["sets_rival_grupo"] <= 2]
    norm = [r for r in of if r not in star and "06:00" <= r["hora"] < "20:00"]
    for label, rs in (("Todos los OFICIAL", of), ("⭐ estrella", star), ("Normal de día", norm)):
        L.append(f"▶ {label} ({len(_hc_rows(rs))} con puntos)")
        for ln in LINEAS:
            L.append(hc_line(label, rs, ln))
    L.append("▶ ¿Compensa cambiar a -4,5 puntos cuando la cuota es baja?")
    L.extend(hc_compare(of))
    L.extend(hc_compare(of, umbral=1.50))
    return L


def verdict(rows):
    out = []
    for tipo in ("OFICIAL", "OFICIAL B", "POTENCIAL"):
        rs = [r for r in rows if r["tipo"] == tipo]
        n, g, p, lo = stat(rs)
        if n < MIN_N:
            out.append(f"  {tipo}: muestra insuficiente ({n} picks).")
            continue
        exp = sum(r["prob_elo"] for r in rs) / n
        extra = (p - exp) * 100
        out.append(f"  {tipo}: {n} picks, aciertas {p*100:.0f}%. Solo por nivel (Elo) "
                   f"se esperaría {exp*100:.0f}% → efecto motivación {extra:+.0f} puntos.")
        out.append(f"     Sigue SOLO si tu cuota media es ≥ {1/lo:.2f} (seguro) o al menos ≥ {1/p:.2f}"
                   f" (punto de equilibrio). Por debajo de {1/p:.2f} pierdes dinero a la larga.")
        m = money(rs)
        if m:
            nn, media, ben, roi = m
            res = "GANA dinero" if roi > 0.03 else ("PIERDE dinero" if roi < -0.03 else "queda en tablas")
            out.append(f"     Con las cuotas reales de Sofascore (media {media:.2f}) {res}: "
                       f"{ben:+.1f} unidades en {nn} picks ({roi*100:+.1f}% por apuesta).")
    return out


def telegram_summary(rows, info):
    L = [f"📈 HISTÓRICO {LIGA.upper()} ({info['desde'].strftime('%d/%m')}–{info['hasta'].strftime('%d/%m')})",
         f"{info['grupos']} grupos analizados"]
    for tipo, ic in (("OFICIAL", "🟢"), ("OFICIAL B", "🔵"), ("POTENCIAL", "🟡")):
        rs = [r for r in rows if r["tipo"] == tipo]
        n, g, p, lo = stat(rs)
        if not n:
            L.append(f"{ic} {tipo}: sin picks")
            continue
        exp = sum(r["prob_elo"] for r in rs) / n
        L.append(f"{ic} {tipo}: {n} picks · {p*100:.0f}% (Elo {exp*100:.0f}%)\n"
                 f"   cuota mínima {1/p:.2f} · segura {1/lo:.2f}")
        m = money(rs)
        if m:
            nn, media, ben, roi = m
            L.append(f"   💶 cuota media real {media:.2f} → {ben:+.1f} u ({roi*100:+.1f}%)")
        for cal, f in (("⭐ estrella", lambda r: not r["aviso_puntos"] and r["sets_rival_grupo"] <= 2),):
            if tipo != "OFICIAL":
                continue
            sub = [r for r in rs if f(r)]
            m2 = money(sub)
            if sub and m2:
                L.append(f"   {cal}: {len(sub)} · {stat(sub)[2]*100:.0f}% · cuota media {m2[1]:.2f} "
                         f"→ {m2[2]:+.1f} u ({m2[3]*100:+.1f}%)")
    of = _hc_rows([r for r in rows if r["tipo"] == "OFICIAL"])
    if len(of) >= 30:
        g = sum(1 for r in of if r["dif_puntos"] > 4.5)
        L.append(f"🎯 OFICIAL -4,5 puntos: {g / len(of) * 100:.0f}% · cuota mínima {len(of) / max(g, 1):.2f}")
        L.extend(hc_compare([r for r in rows if r["tipo"] == "OFICIAL"]))
    L.append("Informe completo y lista de picks en los archivos 👇")
    return "\n".join(L)


# ---------------------------------------------------------------- Telegram
def tg_text(text):
    print(text)
    if not (TG and CHAT):
        return
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG}/sendMessage", data, timeout=20)
    except Exception as ex:
        print("WARN telegram", ex)


def tg_file(path):
    if not (TG and CHAT):
        return
    bnd = uuid.uuid4().hex
    with open(path, "rb") as f:
        content = f.read()
    body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{CHAT}\r\n"
            f"--{bnd}\r\nContent-Disposition: form-data; name=\"document\"; "
            f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            ).encode() + content + f"\r\n--{bnd}--\r\n".encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG}/sendDocument", body,
                                 {"Content-Type": f"multipart/form-data; boundary={bnd}"})
    try:
        urllib.request.urlopen(req, timeout=60)
    except Exception as ex:
        print("WARN telegram file", ex)


def main():
    tg_text(f"⏳ Empiezo el análisis histórico de {LIGA} ({DAYS} días). Tardará unos minutos.")
    rows, info = run()
    add_odds(rows)
    os.makedirs(OUT, exist_ok=True)
    csv_path = os.path.join(OUT, "historico_picks.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        if rows:
            keys = list(dict.fromkeys(k for r in rows for k in r))
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
    txt = report(rows, info)
    rep_path = os.path.join(OUT, "informe_historico.txt")
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
    tg_text(telegram_summary(rows, info))
    tg_file(rep_path)
    tg_file(csv_path)


if __name__ == "__main__":
    main()
