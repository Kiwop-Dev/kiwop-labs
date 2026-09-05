#!/usr/bin/env python3
"""
Agrega una medición del estudio "Estado de la IA en el ecommerce español" (doble
pasada HTTP + headless) en un JSON con una fila por tienda (unión por señal) y
los agregados por tramo y sector. NO publica nada por sí solo: escribe en el
directorio de la medición (privado, fuera de git). Publicar el dataset es un
paso manual (--publish) que se hace con el informe, como promete /labs.

Uso:
  aggregate.py /home/kiwop-astro/labs-runs/ecommerce-ia/2026-09          # → agregado.json en ese dir
  aggregate.py <dir> --publish 2026-09                                    # copia a public/labs/data/ecommerce-ia/2026-09.json

Unión por señal (metodología cerrada el 14-jul-2026): robots.txt y llms.txt se
miden por HTTP (son ficheros para bots); home y ficha de producto se dan por
accesibles si CUALQUIERA de las dos pasadas las leyó; schema Product y chatbot
son verdaderos si cualquiera de las dos los detectó.
"""
import json
import pathlib
import sys
from collections import Counter, defaultdict

AI_BOTS_REF = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "anthropic-ai", "PerplexityBot",
               "Google-Extended", "Bytespider", "CCBot", "Applebot-Extended", "meta-externalagent"]


def load(d, name):
    p = pathlib.Path(d) / name
    return json.loads(p.read_text()) if p.exists() else None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    d = pathlib.Path(sys.argv[1])
    publish = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--publish" else None
    http = {r["dominio"]: r for r in (load(d, "piloto.json") or [])}
    headless = {r["dominio"]: r for r in (load(d, "piloto-headless.json") or [])}
    tiendas = (load(d, "tiendas-300.json") or {}).get("tiendas", [])
    if not tiendas:
        sys.exit("falta tiendas-300.json en el directorio de la medición")

    rows = []
    for t in tiendas:
        h, x = http.get(t["dominio"], {}), headless.get(t["dominio"], {})
        home_ok = (h.get("home_status") == 200) or (x.get("home_status") == 200)
        blocked = sorted(set(h.get("ia_bloqueados") or []) | set(x.get("ia_bloqueados") or []))
        explicit = sorted(set(h.get("ia_con_regla_propia") or []) | set(x.get("ia_con_regla_propia") or []))
        rows.append({
            "dominio": t["dominio"], "sector": t["sector"], "tramo": t["tramo"],
            "marketplace_global": bool(t.get("marketplace_global")),
            "accesible": bool(home_ok),
            "accesible_http": h.get("home_status") == 200,
            "accesible_headless": x.get("home_status") == 200,
            "robots_existe": bool(h.get("robots_existe")),
            "ia_bloqueados": blocked,
            "ia_regla_propia": explicit,
            "bloquea_gptbot": "GPTBot" in blocked,
            "bloquea_alguno": len(blocked) > 0,
            "llms_txt": bool(h.get("llms_txt")),
            "schema_home_n": max(len(h.get("home_jsonld_types") or []), len(x.get("home_jsonld_types") or [])),
            "producto_encontrado": bool(h.get("producto_url") or x.get("producto_url")),
            "producto_schema_product": bool(h.get("producto_tiene_Product") or x.get("producto_tiene_Product")),
            "chatbot": sorted(set(h.get("chatbot") or []) | set(x.get("chatbot") or [])),
            "buscador": sorted(set(h.get("buscador_personalizacion") or []) | set(x.get("buscador_personalizacion") or [])),
        })

    def agg(sub):
        n = len(sub)
        acc = [r for r in sub if r["accesible"]]
        prod = [r for r in acc if r["producto_encontrado"]]
        pct = lambda k, base: round(100 * sum(1 for r in base if r[k]) / len(base), 1) if base else None  # noqa: E731
        return {
            "n": n, "accesibles": len(acc), "no_medibles": n - len(acc),
            "robots_existe_pct": pct("robots_existe", sub),
            "bloquea_alguno_pct": pct("bloquea_alguno", sub),
            "bloquea_gptbot_pct": pct("bloquea_gptbot", sub),
            "llms_txt_pct": pct("llms_txt", sub),
            "con_ficha_producto": len(prod),
            "schema_product_pct_sobre_fichas": round(100 * sum(1 for r in prod if r["producto_schema_product"]) / len(prod), 1) if prod else None,
            "chatbot_pct_sobre_accesibles": round(100 * sum(1 for r in acc if r["chatbot"]) / len(acc), 1) if acc else None,
            "buscador_pct_sobre_accesibles": round(100 * sum(1 for r in acc if r["buscador"]) / len(acc), 1) if acc else None,
            "bots_mas_bloqueados": Counter(b for r in sub for b in r["ia_bloqueados"]).most_common(6),
            "chatbots_vendors": Counter(v for r in acc for v in r["chatbot"]).most_common(6),
        }

    out = {
        "series": "ecommerce-ia", "periodo": d.name, "n": len(rows),
        "metodo": "Doble pasada (HTTP identificado como KiwopResearchBot + Chromium headless con UA de Chrome normalizado), unión por señal. robots.txt y llms.txt por HTTP. Muestra: 300 tiendas por regla pública sector × tamaño (marco Semrush Trending Websites España, jun-2026).",
        "total": agg(rows),
        "por_tramo": {k: agg([r for r in rows if r["tramo"] == k]) for k in ("grande", "mediana", "pequena")},
        "por_sector": {k: agg([r for r in rows if r["sector"] == k]) for k in sorted({r["sector"] for r in rows})},
        "tiendas": rows,
    }
    (d / "agregado.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    t = out["total"]
    print(f"{d.name}: n={t['n']} accesibles={t['accesibles']} bloquea_alguno={t['bloquea_alguno_pct']}% gptbot={t['bloquea_gptbot_pct']}% llms={t['llms_txt_pct']}% schema_product={t['schema_product_pct_sobre_fichas']}% chatbot={t['chatbot_pct_sobre_accesibles']}%")
    if publish:
        pub = pathlib.Path("/home/kiwop-astro/public/labs/data/ecommerce-ia")
        pub.mkdir(parents=True, exist_ok=True)
        (pub / f"{publish}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
        print(f"publicado en {pub / (publish + '.json')} (recuerda commit + deploy)")


if __name__ == "__main__":
    main()
