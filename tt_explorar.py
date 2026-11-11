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


def api(path, **params):
    params["token"] = TOKEN
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "tt-avisador"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
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
    fin(L)


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
