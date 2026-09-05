#!/usr/bin/env python3
"""
Kiwop Labs · «Qué se pregunta a la IA en España»: serie mensual de volumen de búsqueda
en asistentes de IA frente a Google, para 60 términos fijos (keywords.json).

Fuente: DataForSEO. `ai_optimization/ai_keyword_data/keywords_search_volume/live` da el
volumen mensual estimado de consultas en asistentes de IA (ChatGPT y similares) por
país e idioma, con serie de los últimos meses; `keywords_data/google_ads/search_volume/live`
da el volumen de Google para los mismos términos. La comparación (IA ÷ Google) es el
dato que nadie publica por término en España. Coste: céntimos por pasada.

Uso (en el servidor, con el venv de /root/borsa):
  preguntas_ia.py run [--month 2026-09]

Salida: public/labs/data/preguntas-ia/AAAA-MM.json + latest.json + index.json (dataset
público, CC BY 4.0). Credenciales: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD en
/home/kiwop-astro/.credentials/labs.env (misma cuenta que usa Nexo).

Limitación declarada en la página: el volumen en IA es una ESTIMACIÓN del proveedor
(no hay dato oficial de ningún asistente); sirve para comparar términos entre sí y
ver la tendencia, no como cifra absoluta.
"""
import argparse
import base64
import datetime as dt
import json
import os
import pathlib
import urllib.request
from collections import defaultdict

BASE = pathlib.Path(os.environ.get("LABS_REPO", "/home/kiwop-astro"))
KEYWORDS_PATH = BASE / "scripts/labs/preguntas-ia/keywords.json"
OUT_DIR = BASE / "public/labs/data/preguntas-ia"
PAGE_URL = "https://www.kiwop.com/labs/preguntas-ia-espana"


def load_env(path):
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


load_env(BASE / ".credentials/labs.env")
load_env(BASE / ".env")


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def dfs_post(path, payload):
    auth = base64.b64encode(f"{os.environ['DATAFORSEO_LOGIN']}:{os.environ['DATAFORSEO_PASSWORD']}".encode()).decode()
    req = urllib.request.Request(
        "https://api.dataforseo.com/v3/" + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        raise RuntimeError(f"DataForSEO {task.get('status_code')}: {task.get('status_message')}")
    return (task.get("result") or []), data.get("cost", 0)


def ai_volumes(keywords, loc, lang):
    res, cost = dfs_post("ai_optimization/ai_keyword_data/keywords_search_volume/live",
                         [{"keywords": keywords, "location_code": loc, "language_code": lang}])
    out = {}
    first = res[0] if res else {}
    for it in first.get("items") or []:
        hist = sorted([{"year": m.get("year"), "month": m.get("month"), "volume": m.get("ai_search_volume")}
                       for m in (it.get("ai_monthly_searches") or [])], key=lambda x: (x["year"] or 0, x["month"] or 0))
        out[it["keyword"].casefold()] = {"volume": it.get("ai_search_volume"), "monthly": hist}
    return out, cost


def google_volumes(keywords, loc, lang):
    res, cost = dfs_post("keywords_data/google_ads/search_volume/live",
                         [{"keywords": keywords, "location_code": loc, "language_code": lang, "search_partners": False}])
    out = {}
    # el endpoint de google_ads devuelve un item por keyword directamente en result[]
    for it in res:
        if not isinstance(it, dict) or not it.get("keyword"):
            continue
        hist = sorted([{"year": m.get("year"), "month": m.get("month"), "volume": m.get("search_volume")}
                       for m in (it.get("monthly_searches") or [])], key=lambda x: (x["year"] or 0, x["month"] or 0))
        out[it["keyword"].casefold()] = {"volume": it.get("search_volume"), "cpc": it.get("cpc"), "competition": it.get("competition"), "monthly": hist}
    return out, cost


def run(month):
    spec = json.loads(KEYWORDS_PATH.read_text())
    kws = [k["keyword"] for k in spec["keywords"]]
    loc, lang = spec["location_code"], spec["language_code"]
    log(f"Preguntas IA {month}: {len(kws)} términos, loc={loc} lang={lang}")
    ai, cost_ai = ai_volumes(kws, loc, lang)
    log(f"  IA: {len(ai)} términos con dato ({cost_ai} $)")
    goog, cost_g = {}, 0
    try:
        goog, cost_g = google_volumes(kws, loc, lang)
        log(f"  Google: {len(goog)} términos con dato ({cost_g} $)")
    except Exception as e:  # noqa: BLE001
        log(f"  Google falló (se publica sin comparación): {e}")

    rows = []
    for k in spec["keywords"]:
        key = k["keyword"].casefold()
        a = ai.get(key) or {}
        g = goog.get(key) or {}
        av, gv = a.get("volume"), g.get("volume")
        rows.append({
            "keyword": k["keyword"], "group": k["group"],
            "ai_volume": av, "ai_monthly": a.get("monthly") or [],
            "google_volume": gv, "google_monthly": g.get("monthly") or [], "google_cpc": g.get("cpc"),
            "ai_per_1000_google": (round(1000 * av / gv, 1) if av is not None and gv else None),
        })

    groups = defaultdict(lambda: {"terms": 0, "ai_volume": 0, "google_volume": 0, "ai_with_volume": 0})
    for r in rows:
        gr = groups[r["group"]]
        gr["terms"] += 1
        gr["ai_volume"] += r["ai_volume"] or 0
        gr["google_volume"] += r["google_volume"] or 0
        gr["ai_with_volume"] += 1 if (r["ai_volume"] or 0) > 0 else 0
    for gr in groups.values():
        gr["ai_per_1000_google"] = round(1000 * gr["ai_volume"] / gr["google_volume"], 1) if gr["google_volume"] else None

    total_ai = sum(r["ai_volume"] or 0 for r in rows)
    total_g = sum(r["google_volume"] or 0 for r in rows)
    with_ai = sum(1 for r in rows if (r["ai_volume"] or 0) > 0)
    top_ai = sorted([r for r in rows if r["ai_volume"]], key=lambda r: -r["ai_volume"])[:20]
    top_ratio = sorted([r for r in rows if r["ai_per_1000_google"] is not None and (r["google_volume"] or 0) >= 50], key=lambda r: -r["ai_per_1000_google"])[:10]

    # Serie agregada de la IA (suma de los términos con histórico), mes a mes.
    series = defaultdict(int)
    for r in rows:
        for m in r["ai_monthly"]:
            if m.get("year") and m.get("month") and m.get("volume") is not None:
                series[f"{m['year']:04d}-{m['month']:02d}"] += m["volume"]
    ai_series = [{"month": k, "ai_volume": v} for k, v in sorted(series.items())]

    out = {
        "series": "preguntas-ia",
        "month": month,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "page": PAGE_URL,
        "license": "CC BY 4.0",
        "method": {
            "keywords_version": spec["version"], "n": len(rows), "location_code": loc, "language_code": lang,
            "source": spec["source"],
            "provider": "DataForSEO",
            "ai_endpoint": "ai_optimization/ai_keyword_data/keywords_search_volume/live",
            "google_endpoint": "keywords_data/google_ads/search_volume/live",
            "cost_usd": round((cost_ai or 0) + (cost_g or 0), 4),
            "notes": [
                "El volumen en asistentes de IA es una estimación del proveedor (no existe dato oficial de ChatGPT ni de ningún asistente): sirve para comparar términos entre sí y ver tendencias, no como cifra absoluta.",
                "El volumen de Google es el de Google Ads (medias mensuales, búsqueda exacta, España, castellano).",
                "ai_per_1000_google = consultas en IA por cada 1.000 búsquedas en Google del mismo término.",
                "Los 60 términos están congelados y versionados; el histórico mensual es el que devuelve el proveedor en cada pasada.",
            ],
        },
        "aggregate": {
            "total_ai_volume": total_ai, "total_google_volume": total_g,
            "ai_per_1000_google": round(1000 * total_ai / total_g, 1) if total_g else None,
            "terms_with_ai_volume": with_ai,
            "groups": {k: dict(v, name=spec["groups"].get(k, k)) for k, v in groups.items()},
            "top_ai": [{"keyword": r["keyword"], "group": r["group"], "ai_volume": r["ai_volume"], "google_volume": r["google_volume"], "ai_per_1000_google": r["ai_per_1000_google"]} for r in top_ai],
            "top_ratio": [{"keyword": r["keyword"], "group": r["group"], "ai_volume": r["ai_volume"], "google_volume": r["google_volume"], "ai_per_1000_google": r["ai_per_1000_google"]} for r in top_ratio],
            "ai_series": ai_series,
        },
        "keywords": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{month}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    (OUT_DIR / "latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    months = sorted(p.stem for p in OUT_DIR.glob("20??-??.json"))
    (OUT_DIR / "index.json").write_text(json.dumps({"series": "preguntas-ia", "months": months, "latest": months[-1]}, indent=1))
    log(f"Guardado {OUT_DIR / (month + '.json')}: IA total {total_ai}/mes, Google {total_g}/mes, {with_ai}/{len(rows)} términos con volumen en IA")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--month", default=dt.date.today().strftime("%Y-%m"))
    args = ap.parse_args()
    run(args.month)


if __name__ == "__main__":
    main()
