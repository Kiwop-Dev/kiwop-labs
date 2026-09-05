#!/usr/bin/env python3
"""
Kiwop Labs · Nota mensual: el agente redacta, una persona firma.

El día 1, tras medir el baseline GEO y la serie «preguntas IA», este script:
  1. `facts`  : extrae los HECHOS del mes de los datasets públicos (baseline, ranking,
                preguntas-ia y, si toca, estudio ecommerce) con sus deltas contra el mes
                anterior, y los guarda en labs-runs/notas/<mes>/facts.json.
  2. `draft`  : pide a Claude la nota en 7 idiomas SOLO a partir de esos hechos (salida
                estructurada, cifras copiadas tal cual, estilo de la casa), y escribe los
                borradores en blogs/kiwop-labs-<mes>.md (+ .en.md, .ca.md, …) en el
                formato que espera scripts/publish-blog-post.py. No publica nada.
  3. `task`   : abre una tarea en Nexo (proyecto Kiwop Web) con los hechos, el borrador
                de LinkedIn y del correo a editores, y el comando exacto para publicar;
                avisa por Slack. Publicar sigue siendo un acto humano.

Uso (en el servidor):  nota_mensual.py all [--month 2026-09]   (o facts | draft | task)
Claves: ANTHROPIC_API_KEY (.env del sitio); token de Nexo en .credentials/nexo-token.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import urllib.request

BASE = pathlib.Path(os.environ.get("LABS_REPO", "/home/kiwop-astro"))
DATA = BASE / "public/labs/data"
RUNS = BASE / "labs-runs/notas"
BLOGS = BASE / "blogs"
NEXO_PROJECT = 38          # Kiwop Web
NEXO_ASSIGNEE = 5          # Josep
HERO = "/media/labs-nota-hero.webp"
MODEL = os.environ.get("LABS_NOTA_MODEL", "claude-opus-5")
LANGS = ["es", "en", "ca", "de", "fr", "nl", "pt"]
PROVIDER_LABELS = {"anthropic": "Claude", "openai": "ChatGPT (API)", "gemini": "Gemini", "perplexity": "Perplexity", "chatgpt_web": "ChatGPT (chatgpt.com)"}
PAGES = {
    "baseline": "https://www.kiwop.com/labs/geo-baseline",
    "ranking": "https://www.kiwop.com/labs/ranking-agencias-ia-espana",
    "preguntas": "https://www.kiwop.com/labs/preguntas-ia-espana",
    "ecommerce": "https://www.kiwop.com/labs/ia-ecommerce-espana",
    "labs": "https://www.kiwop.com/labs",
}


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


def prev_month(month):
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def load(series, month):
    p = DATA / series / f"{month}.json"
    return json.loads(p.read_text()) if p.exists() else None


def norm(name):
    n = name.casefold().strip()
    n = re.sub(r"\b(s\.?l\.?u?|s\.?a\.?|sl|sa|slu|ltd|inc|group|grupo|agency|agencia|agència)\b\.?", " ", n)
    n = re.sub(r"[^a-z0-9áéíóúàèìòùäëïöüñç ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


NOT_AGENCIES = {"aesia", "red es", "kit digital", "acelera pyme", "gobierno de españa", "enisa", "cdti", "ine", "sepe", "kit consulting"}


def ranking(baseline, set_id, top=5):
    rows = [r for r in baseline["results"] if r["set"] == set_id and not r["error"]]
    acc = {}
    for r in rows:
        seen = set()
        for i, c in enumerate(r["companies"]):
            k = norm(c)
            if not k or k in seen or k in NOT_AGENCIES:
                continue
            seen.add(k)
            a = acc.setdefault(k, {"name": c, "mentions": 0, "pos": 0})
            a["mentions"] += 1
            a["pos"] += i + 1
    out = sorted(acc.values(), key=lambda a: (-a["mentions"], a["pos"] / a["mentions"]))
    return [{"name": a["name"], "mentions": a["mentions"]} for a in out[:top]], len(rows)


# ------------------------------------------------------------------ facts

def facts(month):
    f = {"month": month, "prev_month": prev_month(month), "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "pages": PAGES}
    b, bp = load("geo-baseline", month), load("geo-baseline", prev_month(month))
    if b:
        a = b["aggregate"]["all"]
        fb = {
            "answers": a["answered"], "providers": [PROVIDER_LABELS.get(p["id"], p["id"]) for p in b["method"]["providers"]],
            "providers_note": "Nombra a los asistentes exactamente así: " + ", ".join(PROVIDER_LABELS.get(p["id"], p["id"]) for p in b["method"]["providers"]) + ". ChatGPT (API) es la respuesta por API; ChatGPT (chatgpt.com) es la app de consumo con búsqueda.",
            "kiwop_mentions": a["kiwop_mentions"], "kiwop_share_pct": round(100 * (a["kiwop_share"] or 0), 1), "kiwop_top3": a["kiwop_top3"],
            "kiwop_where": [{"set": r["set"], "prompt": r["prompt"], "provider": PROVIDER_LABELS.get(r["provider"], r["provider"]), "position": r["kiwop_position"]} for r in b["results"] if r["kiwop_mentioned"]],
            "top_domains": [d["domain"] for d in a["domains"][:8]],
            "sets": {},
        }
        for s in b["method"]["sets"]:
            top, n = ranking(b, s["id"])
            sb = b["aggregate"]["sets"][s["id"]]["all"]
            fb["sets"][s["id"]] = {"name": s["name"], "answers": n, "kiwop_mentions": sb["kiwop_mentions"], "top": top}
        if bp:
            ap = bp["aggregate"]["all"]
            fb["delta_vs_prev"] = {"kiwop_mentions": a["kiwop_mentions"] - ap["kiwop_mentions"], "answers_prev": ap["answered"],
                                   "comparable": sorted(p["id"] for p in b["method"]["providers"]) == sorted(p["id"] for p in bp["method"]["providers"]) and b["method"]["prompts_version"] == bp["method"]["prompts_version"]}
            for sid, sd in fb["sets"].items():
                if sid in bp["aggregate"]["sets"]:
                    prev_top, _ = ranking(bp, sid)
                    prev_names = {norm(x["name"]) for x in prev_top}
                    sd["new_in_top"] = [x["name"] for x in sd["top"] if norm(x["name"]) not in prev_names]
        f["baseline"] = fb
    q, qp = load("preguntas-ia", month), load("preguntas-ia", prev_month(month))
    if q:
        A = q["aggregate"]
        fq = {
            "terms": q["method"]["n"], "total_ai": A["total_ai_volume"], "total_google": A["total_google_volume"], "ai_per_1000_google": A["ai_per_1000_google"],
            "terms_with_ai": A["terms_with_ai_volume"],
            "groups": {k: {"name": v.get("name", k), "ai": v["ai_volume"], "google": v["google_volume"], "ai_per_1000_google": v["ai_per_1000_google"]} for k, v in A["groups"].items()},
            "top_ai": [{"keyword": r["keyword"], "ai": r["ai_volume"], "google": r["google_volume"]} for r in A["top_ai"][:8]],
            "top_ratio": [{"keyword": r["keyword"], "ai_per_1000_google": r["ai_per_1000_google"]} for r in A["top_ratio"][:5]],
            "ai_series_last": A["ai_series"][-6:],
        }
        if qp:
            prev = {r["keyword"]: r["ai_volume"] or 0 for r in qp["keywords"]}
            movers = sorted([{"keyword": r["keyword"], "ai": r["ai_volume"] or 0, "prev": prev.get(r["keyword"], 0), "delta": (r["ai_volume"] or 0) - prev.get(r["keyword"], 0)} for r in q["keywords"]], key=lambda x: -abs(x["delta"]))
            fq["movers"] = [m for m in movers if m["delta"] != 0][:6]
            fq["delta_total_ai"] = A["total_ai_volume"] - qp["aggregate"]["total_ai_volume"]
        f["preguntas"] = fq
    e = load("ecommerce-ia", month)
    if e:
        T = e["total"]
        f["ecommerce"] = {"n": T["n"], "accesibles": T["accesibles"], "bloquea_alguno_pct": T["bloquea_alguno_pct"], "bloquea_gptbot_pct": T["bloquea_gptbot_pct"],
                          "llms_txt_verificado_pct": T.get("llms_txt_verificado_pct"), "llms_txt_propio_pct": T.get("llms_txt_propio_pct"), "llms_txt_plataforma_pct": T.get("llms_txt_plataforma_pct"),
                          "schema_product_pct": T["schema_product_pct_sobre_fichas"], "chatbot_pct": T["chatbot_pct_sobre_accesibles"]}
    out = RUNS / month
    out.mkdir(parents=True, exist_ok=True)
    (out / "facts.json").write_text(json.dumps(f, ensure_ascii=False, indent=1))
    log(f"facts → {out / 'facts.json'} (baseline={'sí' if b else 'no'}, preguntas={'sí' if q else 'no'}, ecommerce={'sí' if e else 'no'})")
    return f


# ------------------------------------------------------------------ draft

SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "object",
            "properties": {lang: {
                "type": "object",
                "properties": {
                    "title": {"type": "string"}, "seo_title": {"type": "string"}, "meta_description": {"type": "string"},
                    "slug": {"type": "string"}, "excerpt": {"type": "string"}, "body_markdown": {"type": "string"},
                },
                "required": ["title", "seo_title", "meta_description", "slug", "excerpt", "body_markdown"],
                "additionalProperties": False,
            } for lang in LANGS},
            "required": LANGS, "additionalProperties": False,
        },
        "linkedin_es": {"type": "string"},
        "email_editores_es": {"type": "string"},
    },
    "required": ["posts", "linkedin_es", "email_editores_es"],
    "additionalProperties": False,
}

STYLE = """Escribes para Kiwop (agencia digital de Reus) como una persona del equipo, en primera persona del plural.
Reglas innegociables:
- Cada cifra que escribas tiene que estar en los HECHOS tal cual (misma unidad, mismo redondeo). No inventes ninguna cifra, nombre de empresa, fecha ni causa. Si un hecho no está, no lo afirmes.
- Nada de em-dash (—) ni de " - " como conector. Frases cortas, una idea por frase. Directo al dato en la primera línea. Sin preámbulos, sin "en este artículo", sin cursilerías, sin listas con dos puntos tipo informe, sin emojis. Prosa con algún cabo suelto, como escribe una persona con oficio.
- Tono honesto: si Kiwop sale mal, se dice. Nunca vendas servicios; la única llamada a la acción es leer los datos.
- Longitud del cuerpo: 450 a 650 palabras en cada idioma. Estructura: un párrafo de apertura con el dato del mes, dos o tres subtítulos (##) con lo que cambió y lo que significa, y un último apartado "## Datos y método" con enlaces a las páginas de Labs (usa las URL de HECHOS.pages tal cual, en markdown [texto](url); las URL no se traducen).
- Idiomas: es (castellano), en (inglés británico), ca (catalán normativo, tuteo), de (alemán con Sie), fr (francés con vouvoiement), nl (neerlandés con u), pt (portugués europeo). Cada versión es una redacción natural, no una traducción palabra por palabra; las búsquedas literales medidas (p. ej. «agencia seo») se dejan en castellano entre comillas.
- title: sentence case, 50-70 caracteres, con el mes y el año. seo_title ≤ 65 caracteres. meta_description ≤ 155 caracteres. excerpt: una o dos frases (≤ 200 caracteres). slug: ASCII, minúsculas, guiones, empieza por "kiwop-labs-" y termina en el mes AAAA-MM (p. ej. kiwop-labs-que-cambio-2026-09); localizado en cada idioma.
- linkedin_es: post de LinkedIn en castellano para Josep Purroy (primera persona del singular, 600-900 caracteres, sin hashtags, sin emojis, un dato fuerte al principio, termina con la URL de la página de Labs más relevante).
- email_editores_es: correo corto en castellano (≤ 10 líneas, tutea, cierra con "Gracias") para el editor de un ranking de agencias, con el dato del mes y los dos enlaces (ranking y nota). Empieza con "Hola [nombre],"."""


def month_label(month, lang="es"):
    names = {
        "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
    }
    y, m = map(int, month.split("-"))
    return f"{names['es'][m - 1]} de {y}"


def draft(month):
    import anthropic

    fpath = RUNS / month / "facts.json"
    f = json.loads(fpath.read_text()) if fpath.exists() else facts(month)
    client = anthropic.Anthropic()
    prompt = (
        f"Redacta la nota mensual de Kiwop Labs de {month_label(month)} ({month}) en los 7 idiomas, más el post de LinkedIn y el correo a editores, "
        f"SOLO a partir de estos HECHOS (JSON). Si `baseline.delta_vs_prev.comparable` es false o no existe, no compares con el mes anterior: di que es el primer mes de la serie o que el método cambió.\n\n"
        f"<hechos>\n{json.dumps(f, ensure_ascii=False, indent=1)}\n</hechos>"
    )
    log(f"draft: pidiendo la nota a {MODEL}…")
    # Salida larga (7 idiomas): el SDK exige streaming por encima de ~10 min de tope.
    with client.messages.stream(
        model=MODEL, max_tokens=24000, system=STYLE,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        resp = stream.get_final_message()
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    out = json.loads(text)
    (RUNS / month / "draft.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))

    # Ficheros en el formato de publish-blog-post.py (ES = maestro, sin sufijo).
    BLOGS.mkdir(exist_ok=True)
    slug_es = out["posts"]["es"]["slug"]
    files = []
    for lang in LANGS:
        p = out["posts"][lang]
        body = p["body_markdown"].strip()
        if "—" in body or "—" in p["title"]:
            body = body.replace(" — ", ": ").replace("—", ",")
            p["title"] = p["title"].replace("—", ":")
        md = (
            f"# {p['title']}\n\n<!--\nTitle: {p['seo_title']}\nMeta Description: {p['meta_description']}\nSlug: {p['slug']}\n"
            f"Excerpt: {p['excerpt']}\nFeatured image: {HERO}\nKeywords principales: Kiwop Labs, IA, GEO, asistentes de IA, España\n-->\n\n{body}\n"
        )
        name = f"{slug_es}.md" if lang == "es" else f"{slug_es}.{lang}.md"
        (BLOGS / name).write_text(md)
        files.append(str(BLOGS / name))
    (RUNS / month / "linkedin_es.txt").write_text(out["linkedin_es"])
    (RUNS / month / "email_editores_es.txt").write_text(out["email_editores_es"])
    log(f"draft → {len(files)} ficheros en blogs/ (slug ES: {slug_es}); LinkedIn y correo en {RUNS / month}")
    return out, slug_es


# ------------------------------------------------------------------ task

def nexo(path, payload=None, method=None):
    tok = (BASE / ".credentials/nexo-token").read_text().strip()
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("https://nexo.kiwop.com/api/v1" + path, data=body, method=method or ("POST" if body else "GET"),
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def task(month):
    f = json.loads((RUNS / month / "facts.json").read_text())
    d = json.loads((RUNS / month / "draft.json").read_text())
    slug = d["posts"]["es"]["slug"]
    label = month_label(month)
    lines = [f"Borrador de la nota mensual de Labs ({label}), redactado por el agente a partir de los datos publicados. Revisar, corregir lo que haga falta y publicar. Hasta que se publique no existe.", ""]
    lines += ["**Hechos del mes**"]
    if "baseline" in f:
        b = f["baseline"]
        lines.append(f"- Baseline GEO: Kiwop en {b['kiwop_mentions']} de {b['answers']} respuestas ({b['kiwop_share_pct']} %), top 3 en {b['kiwop_top3']}. Fuentes más citadas: {', '.join(b['top_domains'][:5])}.")
        for sid, s in b["sets"].items():
            lines.append(f"  - {s['name']}: Kiwop {s['kiwop_mentions']}/{s['answers']}; arriba {', '.join(x['name'] + ' (' + str(x['mentions']) + ')' for x in s['top'][:3])}.")
    if "preguntas" in f:
        q = f["preguntas"]
        lines.append(f"- Preguntas a la IA: {q['total_ai']} consultas/mes en IA frente a {q['total_google']} en Google ({q['ai_per_1000_google']} por 1.000); {q['terms_with_ai']}/{q['terms']} términos con volumen. Grupo precios: {q['groups'].get('precios', {}).get('ai_per_1000_google')} por 1.000; local: {q['groups'].get('local', {}).get('ai_per_1000_google')}.")
    if "ecommerce" in f:
        e = f["ecommerce"]
        lines.append(f"- Estudio ecommerce ({e['n']} tiendas): bloquean algún crawler IA {e['bloquea_alguno_pct']} %, llms.txt verificado {e['llms_txt_verificado_pct']} % (propio {e['llms_txt_propio_pct']} %), schema Product {e['schema_product_pct']} %.")
    lines += ["", "**Ficheros (7 idiomas, ya en el repo en `blogs/`)**", f"- `blogs/{slug}.md` y `blogs/{slug}.{{en,ca,de,fr,nl,pt}}.md`", "",
              "**Publicar (cuando esté revisado)**", "```", f"cd /home/kiwop-astro && python3 scripts/publish-blog-post.py {slug} --category-id 110", "```",
              "Luego purgar cachés (Payload → Redis → nginx → CF) y pingar IndexNow con las 7 URLs.", "",
              "**LinkedIn (borrador para Josep)**", "", d["linkedin_es"].strip(), "",
              "**Correo a editores de rankings (borrador)**", "", d["email_editores_es"].strip(), "",
              f"Hechos completos: `labs-runs/notas/{month}/facts.json`. Páginas: {PAGES['baseline']} · {PAGES['ranking']} · {PAGES['preguntas']}."]
    payload = {"title": f"Nota Labs de {label}: revisar y publicar", "description": "\n".join(lines), "priority": "normal",
               "due_date": (dt.date.today() + dt.timedelta(days=7)).isoformat(), "estimated_minutes": 90, "assignee_ids": [NEXO_ASSIGNEE]}
    r = nexo(f"/projects/{NEXO_PROJECT}/tasks", payload)
    t = (r.get("data") or {}).get("task") or r.get("data") or r
    tid = t.get("id") if isinstance(t, dict) else None
    url = t.get("url") if isinstance(t, dict) else None
    log(f"task → Nexo #{tid} {url or ''}")
    text = f"Labs · nota de {label}: borrador en 7 idiomas listo para revisar y publicar. Tarea Nexo #{tid}{(' ' + url) if url else ''}. Publicar: publish-blog-post.py {slug}."
    try:
        nexo("/workspaces/kiwop/slack/dm", {"to": "josep@kiwop.com", "text": text, "human_validated": False})
        log("Slack DM enviado")
    except Exception as e:  # noqa: BLE001
        log(f"Slack DM falló: {e}")
    (RUNS / month / "task.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
    return tid


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["facts", "draft", "task", "all"])
    ap.add_argument("--month", default=dt.date.today().strftime("%Y-%m"))
    args = ap.parse_args()
    if args.cmd in ("facts", "all"):
        facts(args.month)
    if args.cmd in ("draft", "all"):
        draft(args.month)
    if args.cmd in ("task", "all"):
        task(args.month)


if __name__ == "__main__":
    main()
