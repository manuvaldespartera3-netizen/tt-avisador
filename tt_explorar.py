"""
tt_explorar.py - DIAGNÓSTICO DE BETSAPI (con tu token, secreto BETSAPI_TOKEN).

Comprueba, sin tocar los avisadores:
  1. que el token funciona y qué número de deporte es el tenis de mesa
  2. qué ligas de tenis de mesa hay (y sus números): TT Cup, Czech Liga Pro...
  3. cuántos partidos de HOY da cada una: terminados, en juego y próximos
  4. cómo vienen los sets y los puntos (un partido de ejemplo)
Lo guarda en backtest/exploracion.txt y avisa por Telegram (bot de TT Cup).
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

from tt_feed import TZ
from tt_backtest import tg_text, tg_file

TOKEN = os.getenv("BETSAPI_TOKEN", "").strip()
BASE = "https://api.b365api.com"
OUT = "backtest"
BUSCAR = ("tt cup", "liga pro", "setka", "elite")


HOSTS = ["https://api.b365api.com", "https://api.betsapi.com"]


def api(path, **params):
    params["token"] = TOKEN
    ultimo = None
    for host in HOSTS:
        url = f"{host}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "tt-avisador"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as ex:
            try:
                cuerpo = ex.read().decode()[:200]
            except Exception:
                cuerpo = ""
            ultimo = RuntimeError(f"{host.split('//')[1]}: HTTP {ex.code} {cuerpo}")
    else:
        raise ultimo
    if isinstance(data, dict) and data.get("success") in (0, "0", False):
        raise RuntimeError(f"BetsAPI: {str(data)[:200]}")
    return data


def todas(path, **params):
    """Todas las páginas de una consulta (máx. 10)."""
    out = []
    for page in range(1, 11):
        d = api(path, page=page, **params)
        res = d.get("results") or []
        out += res
        pager = d.get("pager") or {}
        tot, per = int(pager.get("total", 0) or 0), int(pager.get("per_page", 0) or 0)
        if not res or not per or page * per >= tot:
            break
        time.sleep(0.3)
    return out


def main():
    L = ["🔎 DIAGNÓSTICO BETSAPI", ""]
    if not TOKEN:
        L.append("❌ No encuentro el secreto BETSAPI_TOKEN en GitHub (o el workflow no lo pasa).")
        return fin(L)
    hoy = datetime.now(TZ).strftime("%Y%m%d")
    raro = any(c.isspace() for c in os.getenv("BETSAPI_TOKEN", "")) or not TOKEN.replace("-", "").isalnum()
    L.append(f"Token: {len(TOKEN)} caracteres" + (" · ⚠️ tiene espacios o símbolos raros" if raro else ""))
    L.append("")

    # 1) deporte: probar el 92 (tenis de mesa en BetsAPI) y comprobar por los nombres
    sport = None
    for sid in (92, 91, 90):
        try:
            vivos = api("/v3/events/inplay", sport_id=sid).get("results") or []
            ligas = Counter((e.get("league") or {}).get("name", "?") for e in vivos)
            L.append(f"1) sport_id={sid}: {len(vivos)} en juego · ligas: "
                     + ", ".join(f"{n} ×{c}" for n, c in ligas.most_common(6)))
            if any(any(b in n.lower() for b in BUSCAR) for n in ligas):
                sport = sid
                break
        except Exception as ex:
            L.append(f"1) sport_id={sid}: {str(ex)[:120]}")
    if not sport:
        sport = 92
        L.append("   (no he podido confirmar el deporte por los nombres; uso 92)")
    L.append(f"   → tenis de mesa = sport_id {sport}")
    L.append("")

    # 2) ligas: con los partidos de hoy (terminados + próximos + en juego)
    try:
        terminados = todas("/v3/events/ended", sport_id=sport, day=hoy)
    except Exception as ex:
        terminados = []
        L.append(f"2) terminados de hoy: {str(ex)[:120]}")
    try:
        proximos = todas("/v3/events/upcoming", sport_id=sport, day=hoy)
    except Exception as ex:
        proximos = []
        L.append(f"2) próximos de hoy: {str(ex)[:120]}")
    try:
        vivos = api("/v3/events/inplay", sport_id=sport).get("results") or []
    except Exception:
        vivos = []
    por = {}
    for tipo, lista in (("terminados", terminados), ("en juego", vivos), ("próximos", proximos)):
        for e in lista:
            lg = e.get("league") or {}
            k = (str(lg.get("id")), lg.get("name", "?"))
            por.setdefault(k, Counter())[tipo] += 1
    L.append(f"2) Ligas de tenis de mesa HOY (número · nombre: terminados / en juego / próximos):")
    orden = sorted(por.items(), key=lambda kv: -sum(kv[1].values()))
    for (lid, nom), c in orden[:25]:
        marca = " ◀" if any(b in nom.lower() for b in BUSCAR) else ""
        L.append(f"   {lid} · {nom}: {c['terminados']} / {c['en juego']} / {c['próximos']}{marca}")
    L.append("")

    # 3) ejemplo de un partido terminado de TT Cup o Liga Pro, con sus sets
    ej = next((e for e in terminados if any(b in (e.get("league") or {}).get("name", "").lower()
                                             for b in ("tt cup", "liga pro"))), None)
    if ej:
        L.append("3) Ejemplo de partido terminado (así vienen los datos):")
        L.append("   " + json.dumps(ej, ensure_ascii=False)[:1500])
        try:
            v = api("/v1/event/view", event_id=ej["id"])
            L.append("   Detalle (event/view): " + json.dumps(v.get("results", v), ensure_ascii=False)[:1500])
        except Exception as ex:
            L.append(f"   event/view: {str(ex)[:100]}")
    ej2 = next((e for e in vivos if any(b in (e.get("league") or {}).get("name", "").lower()
                                         for b in ("tt cup", "liga pro"))), None)
    if ej2:
        L.append("   Ejemplo de partido EN JUEGO: " + json.dumps(ej2, ensure_ascii=False)[:1200])
    L.append("")
    prueba_lector(L)
    L.append("")
    prueba_rating(L)
    fin(L)


def _nuxt(html):
    """Datos que lleva dentro la web oficial de Liga Pro (formato Nuxt)."""
    import re
    m = re.search(r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    d = json.loads(m.group(1))
    memo = {}
    env = ("Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "EmptyShallowRef", "NuxtError")

    def r(i):
        if i in memo:
            return memo[i]
        v = d[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in env:
                out = r(v[1]) if len(v) > 1 else None
            elif v and isinstance(v[0], str) and v[0] == "Set":
                out = [r(x) for x in v[1:]]
            elif v and isinstance(v[0], str) and v[0] == "Date":
                out = v[1]
            else:
                memo[i] = out = []
                out.extend(r(x) if isinstance(x, int) else x for x in v)
                return out
        elif isinstance(v, dict):
            memo[i] = out = {}
            for k, x in v.items():
                out[k] = r(x) if isinstance(x, int) else x
            return out
        else:
            out = v
        memo[i] = out
        return out
    return r(0)


def _web(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def prueba_rating(L):
    """5) Para el canal RATING LIGA PRO: ¿hay en directo (a) cuotas en BetsAPI
    antes del partido y (b) el rating oficial de los torneos que aun no han empezado?"""
    import re
    from datetime import timedelta
    L.append("5) PARA EL CANAL RATING LIGA PRO:")
    hoy = datetime.now(TZ)
    # (a) cuotas de BetsAPI de partidos de Liga Pro que aun no han empezado
    try:
        prox = todas("/v3/events/upcoming", sport_id=92, league_id=22742, day=hoy.strftime("%Y%m%d"))
        if hoy.hour >= 20:
            prox += todas("/v3/events/upcoming", sport_id=92, league_id=22742,
                          day=(hoy + timedelta(days=1)).strftime("%Y%m%d"))
        L.append(f"   a) próximos de Liga Pro en BetsAPI: {len(prox)}")
        hechos = 0
        for e in sorted(prox, key=lambda x: int(x.get("time") or 0))[:3]:
            cuando = datetime.fromtimestamp(int(e["time"]), TZ).strftime("%d/%m %H:%M")
            nom = f"{(e.get('home') or {}).get('name')} – {(e.get('away') or {}).get('name')} ({cuando})"
            for path in ("/v2/event/odds/summary", "/v2/event/odds"):
                try:
                    d = api(path, event_id=e["id"])
                    res = d.get("results") or {}
                    txt = json.dumps(res, ensure_ascii=False)
                    L.append(f"      {path} {nom}: {txt[:500]}")
                    hechos += 1
                except Exception as ex:
                    L.append(f"      {path} {nom}: ❌ {str(ex)[:150]}")
        if not prox:
            L.append("      (ahora no hay próximos: lánzalo de día)")
    except Exception as ex:
        L.append(f"   a) ❌ {str(ex)[:150]}")
    # (b) rating oficial en la web de Liga Pro para los torneos de hoy/mañana
    try:
        d0 = hoy.date()
        html = _web(f"https://tt.league-pro.com/en/tournaments?date_from={d0}&date_to={d0 + timedelta(days=1)}")
        root = _nuxt(html)
        texto = html + (json.dumps(root) if root else "")
        ids = sorted({int(x) for x in re.findall(r"/tournaments/(\d{5})(?!\d)", texto)}
                     | {int(x) for x in re.findall(r'"id":\s*(\d{5})(?!\d)', texto)})
        L.append(f"   b) web oficial: {len(ids)} torneos de hoy/mañana · ids {ids[:3]}…{ids[-3:]}")
        vistos = 0
        for tid in reversed(ids):
            try:
                pd = _nuxt(_web(f"https://tt.league-pro.com/en/tournaments/{tid}"))["data"]["tournament-page"]["pageData"]
            except Exception:
                continue
            t, w = pd["tournament"], pd["widgets"]
            lados = [(((s.get("player") or {}).get("surname_en")), s.get("rating_before_tournament"))
                     for s in (w.get("sides") or [])]
            ms = w.get("matches") or []
            sin = sum(1 for m in ms if m.get("status") != 3)
            L.append(f"      torneo {tid} '{t.get('name_en')}' empieza {t.get('start_date')} · "
                     f"jugadores {lados} · partidos {len(ms)} ({sin} sin jugar) · "
                     f"ej. partido: {json.dumps(ms[0], ensure_ascii=False)[:250] if ms else '-'}")
            vistos += 1
            if vistos >= 2:
                break
    except Exception as ex:
        L.append(f"   b) ❌ {str(ex)[:150]}")


def prueba_lector(L):
    """4) El lector NUEVO del bot (tt_betsapi) con datos reales: grupos que forma."""
    import tt_betsapi
    from tt_feed import build_groups, TZ as tz
    hoy = datetime.now(tz).strftime("%Y-%m-%d")
    L.append("4) PRUEBA DEL LECTOR NUEVO (lo que verá el bot con BetsAPI):")
    for sid, nom in (("19039", "Liga Pro"), ("15005", "TT Cup")):
        try:
            t0 = time.time()
            n0 = tt_betsapi.LLAMADAS[0]
            evs = tt_betsapi.eventos(sid, [hoy])
            groups, parsed = build_groups(evs)
            vivos = [p for p in parsed if p["status"] == "inprogress"]
            fin_ = sum(1 for p in parsed if p["final"])
            L.append(f"   {nom}: {len(parsed)} partidos ({fin_} terminados, {len(vivos)} en juego) · "
                     f"{len(groups)} grupos · {tt_betsapi.LLAMADAS[0] - n0} consultas · "
                     f"{time.time() - t0:.1f} s")
            for p in vivos[:3]:
                L.append(f"     🔴 {p['a']} {p['score']} {p['b']} (puntos set: {p['live_pts']})")
            abiertos = [g for g in groups if g.pending()]
            for g in abiertos[-4:]:
                hechos = sum(1 for m in g.matches if m.final)
                L.append(f"     • {g.matches[0].time} {' · '.join(g.players)}: {hechos}/6 jugados"
                         + (" ⚠️ dudoso" if getattr(g, "dudoso", False) else ""))
        except Exception as ex:
            L.append(f"   {nom}: ❌ {str(ex)[:150]}")


def fin(L):
    txt = "\n".join(L)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "exploracion.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)
    tg_text(txt[:3900])


if __name__ == "__main__":
    main()
