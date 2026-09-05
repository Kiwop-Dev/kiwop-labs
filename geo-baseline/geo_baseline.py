#!/usr/bin/env python3
"""
Baseline GEO de Kiwop Labs: qué agencias citan los LLM en España.

Lanza los prompts de `prompts.json` (dos sets de 15) contra los asistentes con
búsqueda web de cada proveedor y registra, por prompt y proveedor, la respuesta
íntegra, las empresas recomendadas (extraídas con salida estructurada), si Kiwop
aparece y en qué posición, y los dominios citados. Guarda un JSON versionado por
mes en public/labs/data/geo-baseline/ (dataset público) y un `latest.json`.

Uso (en el servidor, con el venv de /root/borsa):
  geo_baseline.py run [--month 2026-09] [--providers anthropic,openai,gemini,perplexity,chatgpt_web]
                      [--sets geo-agencias,ia-agencias,preguntas-reales] [--limit N]
  geo_baseline.py notify [--month 2026-09]     # DM de Slack (vía Nexo) + email con el resumen y el delta

Claves: ANTHROPIC_API_KEY sale del .env del sitio; OPENAI_API_KEY, GEMINI_API_KEY,
PERPLEXITY_API_KEY y DATAFORSEO_LOGIN/DATAFORSEO_PASSWORD de
/home/kiwop-astro/.credentials/labs.env (chmod 600, fuera de git).
Gemini y Perplexity se piden por su API directa si hay clave propia y, si no, por la
LLM Responses API de DataForSEO (misma pregunta, con búsqueda web); `chatgpt_web` es
el ChatGPT de consumo (chatgpt.com con búsqueda, localizado en España) leído por el
LLM Scraper de DataForSEO. Un proveedor sin credenciales se salta y queda en `method.skipped`.

Limitación declarada en la página: la respuesta por API con búsqueda no es idéntica a
la de la app de consumo de cada proveedor (modelo, personalización, memoria). Se
publica el modelo exacto y la fecha de cada medición.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

BASE = pathlib.Path(os.environ.get("LABS_REPO", "/home/kiwop-astro"))
PROMPTS_PATH = BASE / "scripts/labs/geo-baseline/prompts.json"
OUT_DIR = BASE / "public/labs/data/geo-baseline"
PAGE_URL = "https://www.kiwop.com/labs/geo-baseline"

DEFAULT_MODELS = {
    "anthropic": os.environ.get("LABS_ANTHROPIC_MODEL", "claude-opus-5"),
    "openai": os.environ.get("LABS_OPENAI_MODEL", "gpt-5.2"),
    "gemini": os.environ.get("LABS_GEMINI_MODEL", "gemini-3.8-flash"),
    "perplexity": os.environ.get("LABS_PERPLEXITY_MODEL", "sonar-pro"),
    "chatgpt_web": "chatgpt.com (app, con búsqueda)",
}
DFS_LOCATION_ES = 2724  # España
_REDIRECT_CACHE = {}
EXTRACT_MODEL = "claude-opus-5"


def load_env(path):
    """KEY=VALUE por línea, sin pisar variables ya presentes. No usamos `source`:
    el .env del sitio lleva valores que la shell interpreta mal."""
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)
    except FileNotFoundError:
        pass


# labs.env PRIMERO: la clave de OpenAI del .env del sitio es otra (y hoy inválida); setdefault no pisa.
load_env(BASE / ".credentials/labs.env")
load_env(BASE / ".env")


def log(msg):
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def domain_of(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def with_retries(fn, tries=3, wait=8):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"  intento {i + 1}/{tries} falló: {type(e).__name__}: {str(e)[:160]}")
            time.sleep(wait * (i + 1))
    raise last


# ----------------------------------------------------------------- proveedores

def ask_anthropic(prompt, model):
    import anthropic

    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}]
    messages = [{"role": "user", "content": prompt}]
    texts, urls = [], []
    for _ in range(4):  # pause_turn: reanudar como mucho 3 veces
        resp = client.messages.create(model=model, max_tokens=6000, tools=tools, messages=messages)
        for block in resp.content:
            b = block.model_dump()
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
                for c in b.get("citations") or []:
                    if c.get("url"):
                        urls.append(c["url"])
            elif t == "web_search_tool_result":
                content = b.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("url"):
                            urls.append(item["url"])
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if resp.stop_reason == "refusal":
            texts.append("[refusal]")
        break
    return "\n".join(texts).strip(), urls


def ask_openai(prompt, model):
    from openai import OpenAI

    client = OpenAI()
    resp = client.responses.create(model=model, tools=[{"type": "web_search"}], input=prompt)
    urls = []
    for item in getattr(resp, "output", []) or []:
        if getattr(item, "type", "") != "message":
            continue
        for part in getattr(item, "content", []) or []:
            for ann in getattr(part, "annotations", []) or []:
                if getattr(ann, "type", "") == "url_citation" and getattr(ann, "url", None):
                    urls.append(ann.url)
    return (getattr(resp, "output_text", "") or "").strip(), urls


def ask_gemini(prompt, model):
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(model=model, contents=prompt, config={"tools": [{"google_search": {}}]})
    urls = []
    try:
        for cand in resp.candidates or []:
            gm = getattr(cand, "grounding_metadata", None)
            for ch in (getattr(gm, "grounding_chunks", None) or []):
                web = getattr(ch, "web", None)
                if web is not None and getattr(web, "uri", None):
                    urls.append(web.uri)
    except Exception:  # noqa: BLE001
        pass
    return (resp.text or "").strip(), urls


def ask_perplexity(prompt, model):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        "https://api.perplexity.ai/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    text = data["choices"][0]["message"]["content"]
    urls = list(data.get("citations") or []) + [s.get("url") for s in data.get("search_results") or [] if s.get("url")]
    return text.strip(), urls


# ------------------------------------------------------------ DataForSEO
# Gemini y Perplexity sin cuenta propia, y el ChatGPT de consumo, se leen por
# DataForSEO (basic auth). Cada llamada se cobra por separado y queda en su
# `money_spent`; la cuenta es la misma que usa Nexo.

def dfs_configured():
    return bool(os.environ.get("DATAFORSEO_LOGIN") and os.environ.get("DATAFORSEO_PASSWORD"))


def dfs_post(path, payload):
    import base64

    auth = base64.b64encode(f"{os.environ['DATAFORSEO_LOGIN']}:{os.environ['DATAFORSEO_PASSWORD']}".encode()).decode()
    req = urllib.request.Request(
        "https://api.dataforseo.com/v3/" + path, data=json.dumps(payload).encode(),
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        raise RuntimeError(f"DataForSEO {task.get('status_code')}: {task.get('status_message')}")
    return (task.get("result") or [{}])[0]


def resolve_redirect(url):
    """Gemini cita a través de vertexaisearch.cloud.google.com/grounding-api-redirect/…:
    el dominio real solo se sabe siguiendo la redirección (HEAD, sin cuerpo)."""
    if "grounding-api-redirect" not in url:
        return url
    if url in _REDIRECT_CACHE:
        return _REDIRECT_CACHE[url]
    final = url
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0 (KiwopLabs baseline)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            final = r.geturl()
    except urllib.error.HTTPError as e:
        final = e.geturl() or url
    except Exception:  # noqa: BLE001
        pass
    _REDIRECT_CACHE[url] = final
    return final


def ask_dfs_llm(platform, prompt, model):
    body = {"user_prompt": prompt, "model_name": model, "web_search": True, "max_output_tokens": 4000}
    if platform != "gemini":  # Gemini rechaza el país (40501 Invalid Field); Perplexity lo acepta
        body["web_search_country_iso_code"] = "ES"
    res = dfs_post(f"ai_optimization/{platform}/llm_responses/live", [body])
    texts, urls = [], []
    for item in res.get("items") or []:
        for sec in item.get("sections") or []:
            if sec.get("text"):
                texts.append(sec["text"])
            for ann in sec.get("annotations") or []:
                if ann.get("url"):
                    urls.append(resolve_redirect(ann["url"]))
    return "\n".join(texts).strip(), urls


def ask_gemini_dfs(prompt, model):
    return ask_dfs_llm("gemini", prompt, model)


def ask_perplexity_dfs(prompt, model):
    return ask_dfs_llm("perplexity", prompt, model)


def ask_chatgpt_web(prompt, _model):
    """El ChatGPT de consumo (chatgpt.com con búsqueda), localizado en España, tal
    como lo ve un usuario sin sesión. Devuelve la respuesta en markdown y las fuentes."""
    res = dfs_post("ai_optimization/chat_gpt/llm_scraper/live/advanced", [{
        "keyword": prompt[:700], "location_code": DFS_LOCATION_ES, "language_code": "es", "web_search": True,
    }])
    text = (res.get("markdown") or "").strip()
    urls = []
    for src in res.get("sources") or []:
        if src.get("url"):
            urls.append(src["url"])
    for item in res.get("items") or []:
        for src in item.get("sources") or []:
            if src.get("url"):
                urls.append(src["url"])
        if item.get("type") == "chat_gpt_local_businesses":
            for biz in item.get("items") or []:
                if biz.get("url"):
                    urls.append(biz["url"])
    if not text:
        text = "\n".join((i.get("markdown") or "") for i in res.get("items") or []).strip()
    return text, urls


PROVIDERS = {
    # id: (cómo se decide si está activo, función, etiqueta de la herramienta)
    "anthropic": ("ANTHROPIC_API_KEY", ask_anthropic, "web_search_20260209"),
    "openai": ("OPENAI_API_KEY", ask_openai, "responses.web_search"),
    "gemini": ("GEMINI_API_KEY", ask_gemini, "google_search grounding"),
    "perplexity": ("PERPLEXITY_API_KEY", ask_perplexity, "sonar (búsqueda integrada)"),
    "chatgpt_web": ("DATAFORSEO", ask_chatgpt_web, "DataForSEO LLM Scraper (chatgpt.com, búsqueda, España)"),
}
# Sin clave propia, Gemini y Perplexity van por la LLM Responses API de DataForSEO.
DFS_FALLBACK = {
    "gemini": (ask_gemini_dfs, "DataForSEO LLM Responses (google_search)"),
    "perplexity": (ask_perplexity_dfs, "DataForSEO LLM Responses (sonar)"),
}


def resolve_provider(pid):
    """→ (activo, modelo, función, etiqueta, vía) según las credenciales presentes."""
    env_key, fn, tool = PROVIDERS[pid]
    model = DEFAULT_MODELS[pid]
    if env_key == "DATAFORSEO":
        return (dfs_configured(), model, fn, tool, "dataforseo")
    if os.environ.get(env_key):
        return (True, model, fn, tool, "api")
    if pid in DFS_FALLBACK and dfs_configured():
        fn2, tool2 = DFS_FALLBACK[pid]
        return (True, model, fn2, tool2, "dataforseo")
    return (False, model, fn, tool, None)


# ------------------------------------------------------------------ extracción

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "description": "Empresas, agencias o consultoras que la respuesta recomienda o lista como opción, en el orden en que aparecen. Nombre limpio, sin sufijos legales ni descripciones.",
            "items": {"type": "string"},
        },
        "kiwop_mentioned": {"type": "boolean", "description": "true si la respuesta nombra a Kiwop."},
    },
    "required": ["companies", "kiwop_mentioned"],
    "additionalProperties": False,
}


def extract_companies(answer):
    import anthropic

    if not answer or answer == "[refusal]":
        return [], False
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=EXTRACT_MODEL,
        max_tokens=2000,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
        messages=[{
            "role": "user",
            "content": (
                "Esta es la respuesta de un asistente de IA a una pregunta sobre qué agencia o consultora "
                "contratar en España. Extrae las empresas que recomienda o lista como opción, en su orden de "
                "aparición, y si nombra a Kiwop.\n\n<respuesta>\n" + answer[:20000] + "\n</respuesta>"
            ),
        }],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = json.loads(text)
    return [c.strip() for c in data.get("companies", []) if c and c.strip()], bool(data.get("kiwop_mentioned"))


def norm_name(name):
    n = name.casefold().strip()
    n = re.sub(r"\b(s\.?l\.?u?|s\.?a\.?|sl|sa|slu|ltd|inc|group|grupo|agency|agencia|agència)\b\.?", " ", n)
    n = re.sub(r"[^a-z0-9áéíóúàèìòùäëïöüñç ]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


# ------------------------------------------------------------------ run

def run(month, providers, sets, limit):
    spec = json.loads(PROMPTS_PATH.read_text())
    prompt_sets = [s for s in spec["sets"] if not sets or s["id"] in sets]
    active, skipped, fns = [], [], {}
    for pid in providers:
        ok, model, fn, tool, via = resolve_provider(pid)
        if ok:
            active.append({"id": pid, "model": model, "tool": tool, "via": via})
            fns[pid] = fn
        else:
            skipped.append({"id": pid, "reason": f"sin {PROVIDERS[pid][0]} ni DataForSEO"})
    log(f"Baseline GEO {month}: sets={[s['id'] for s in prompt_sets]} proveedores={[a['id'] for a in active]} saltados={[s['id'] for s in skipped]}")

    results = []
    for s in prompt_sets:
        prompts = s["prompts"][:limit] if limit else s["prompts"]
        for i, prompt in enumerate(prompts, 1):
            for prov in active:
                pid = prov["id"]
                fn = fns[pid]
                log(f"[{s['id']} #{i:02d}] {pid} · {prompt[:70]}")
                row = {"set": s["id"], "prompt_id": i, "prompt": prompt, "provider": pid, "model": prov["model"],
                       "answer": "", "companies": [], "kiwop_mentioned": False, "kiwop_position": None,
                       "cited_domains": [], "error": None}
                try:
                    answer, urls = with_retries(lambda: fn(prompt, prov["model"]))
                    row["answer"] = answer
                    doms = []
                    for u in urls:
                        d = domain_of(u)
                        if d and d not in doms:
                            doms.append(d)
                    row["cited_domains"] = doms
                    companies, kiwop = with_retries(lambda: extract_companies(answer))
                    row["companies"] = companies
                    keys = [norm_name(c) for c in companies]
                    pos = next((k + 1 for k, n in enumerate(keys) if "kiwop" in n), None)
                    row["kiwop_mentioned"] = bool(kiwop or pos or re.search(r"\bkiwop\b", answer, re.I))
                    row["kiwop_position"] = pos
                    log(f"   → {len(companies)} empresas · Kiwop={'sí' if row['kiwop_mentioned'] else 'no'}"
                        f"{' (#' + str(pos) + ')' if pos else ''} · {len(doms)} dominios")
                except Exception as e:  # noqa: BLE001
                    row["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                    log(f"   ✗ {row['error']}")
                results.append(row)
                time.sleep(1)

    out = {
        "series": "geo-baseline",
        "month": month,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "page": PAGE_URL,
        "method": {
            "prompts_version": spec["version"],
            "sets": [{"id": s["id"], "name": s["name"], "source": s["source"], "source_url": s["source_url"], "n": len(s["prompts"])} for s in prompt_sets],
            "providers": active,
            "skipped": skipped,
            "extraction": {"model": EXTRACT_MODEL, "how": "salida estructurada (json_schema) sobre la respuesta íntegra; Kiwop también se busca por texto"},
            "notes": [
                "Cada prompt se lanza una sola vez por proveedor, en sesión nueva, sin instrucciones de sistema, en castellano.",
                "La respuesta por API con búsqueda web no es idéntica a la de la app de consumo del proveedor (modelo, memoria, personalización).",
                "Se publica la respuesta íntegra de cada prompt para que cualquiera pueda verificar la extracción.",
                "Los proveedores marcados via=dataforseo se consultan a través de DataForSEO (LLM Responses API con búsqueda web, país ES); chatgpt_web es el ChatGPT de consumo (chatgpt.com con búsqueda, localizado en España, sin sesión) leído por su LLM Scraper: es lo más parecido a lo que ve un usuario.",
                "Las citas de Gemini llegan como redirecciones de Google (grounding-api-redirect) y se resuelven al dominio real siguiendo la redirección.",
            ],
        },
        "results": results,
        "aggregate": aggregate(results, prompt_sets, active),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{month}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    (OUT_DIR / "latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    months = sorted(p.stem for p in OUT_DIR.glob("20??-??.json"))
    (OUT_DIR / "index.json").write_text(json.dumps({"series": "geo-baseline", "months": months, "latest": months[-1]}, indent=1))
    log(f"Guardado {path} ({len(results)} filas, {sum(1 for r in results if r['error'])} errores)")
    return out


def aggregate(results, prompt_sets, active):
    def block(rows):
        n = len(rows)
        ok = [r for r in rows if not r["error"]]
        names = Counter()
        display = {}
        for r in ok:
            seen = set()
            for c in r["companies"]:
                k = norm_name(c)
                if not k or k in seen:
                    continue
                seen.add(k)
                names[k] += 1
                display.setdefault(k, c)
        kiwop = sum(1 for r in ok if r["kiwop_mentioned"])
        top3 = sum(1 for r in ok if r["kiwop_position"] and r["kiwop_position"] <= 3)
        doms = Counter(d for r in ok for d in r["cited_domains"])
        return {
            "prompts": n, "answered": len(ok), "errors": n - len(ok),
            "kiwop_mentions": kiwop, "kiwop_top3": top3,
            "kiwop_share": round(kiwop / len(ok), 3) if ok else None,
            "companies": [{"name": display[k], "mentions": v} for k, v in names.most_common(20)],
            "domains": [{"domain": d, "citations": v} for d, v in doms.most_common(20)],
        }

    agg = {"sets": {}, "all": block(results)}
    for s in prompt_sets:
        rows = [r for r in results if r["set"] == s["id"]]
        agg["sets"][s["id"]] = {"all": block(rows), "providers": {p["id"]: block([r for r in rows if r["provider"] == p["id"]]) for p in active}}
    agg["providers"] = {p["id"]: block([r for r in results if r["provider"] == p["id"]]) for p in active}
    return agg


# ------------------------------------------------------------------ notify

def notify(month):
    cur = json.loads((OUT_DIR / f"{month}.json").read_text())
    months = sorted(p.stem for p in OUT_DIR.glob("20??-??.json"))
    prev = None
    if month in months and months.index(month) > 0:
        prev = json.loads((OUT_DIR / f"{months[months.index(month) - 1]}.json").read_text())
    a = cur["aggregate"]["all"]
    lines = [f"Labs · baseline GEO {month} listo: {a['answered']}/{a['prompts']} respuestas de {', '.join(p['id'] for p in cur['method']['providers'])}."]
    lines.append(f"Kiwop aparece en {a['kiwop_mentions']} de {a['answered']} ({(a['kiwop_share'] or 0) * 100:.0f} %), top 3 en {a['kiwop_top3']}.")
    for sid, s in cur["aggregate"]["sets"].items():
        b = s["all"]
        lines.append(f"· {sid}: Kiwop {b['kiwop_mentions']}/{b['answered']}; más citadas: " + ", ".join(f"{c['name']} ({c['mentions']})" for c in b["companies"][:5]))
    if prev:
        pa = prev["aggregate"]["all"]
        delta = a["kiwop_mentions"] - pa["kiwop_mentions"]
        lines.append(f"Delta vs {prev['month']}: Kiwop {pa['kiwop_mentions']} → {a['kiwop_mentions']} menciones ({'+' if delta >= 0 else ''}{delta}).")
    lines.append("Dominios más citados: " + ", ".join(f"{d['domain']} ({d['citations']})" for d in a["domains"][:6]))
    if cur["method"]["skipped"]:
        lines.append("Proveedores sin clave: " + ", ".join(s["id"] for s in cur["method"]["skipped"]) + ".")
    lines.append(f"Página: {PAGE_URL} · Dataset: https://www.kiwop.com/labs/data/geo-baseline/{month}.json")
    lines.append("Lectura y nota de Labs: las decide una persona; el cron solo mide y publica el dato.")
    text = "\n".join(lines)[:2900]
    print(text)
    sent = False
    token_path = BASE / ".credentials/nexo-token"
    if token_path.exists():
        try:
            body = json.dumps({"to": "josep@kiwop.com", "text": text, "human_validated": False}).encode()
            req = urllib.request.Request(
                "https://nexo.kiwop.com/api/v1/workspaces/kiwop/slack/dm", data=body,
                headers={"Authorization": f"Bearer {token_path.read_text().strip()}", "Content-Type": "application/json", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
                log(f"Slack DM: {resp.get('summary') or resp}")
                sent = bool(resp.get("delivered", True))
        except Exception as e:  # noqa: BLE001
            log(f"Slack DM falló: {e}")
    if not sent:
        try:
            import subprocess
            msg = f"Subject: [Labs] Baseline GEO {month}\nTo: josep@kiwop.com\nContent-Type: text/plain; charset=utf-8\n\n{text}\n"
            subprocess.run(["/usr/sbin/sendmail", "-t"], input=msg.encode(), check=True)
            log("Email enviado a josep@kiwop.com")
        except Exception as e:  # noqa: BLE001
            log(f"Email falló: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--month", default=dt.date.today().strftime("%Y-%m"))
    r.add_argument("--providers", default="anthropic,openai,gemini,perplexity,chatgpt_web")
    r.add_argument("--sets", default="")
    r.add_argument("--limit", type=int, default=0, help="solo los N primeros prompts de cada set (pruebas)")
    n = sub.add_parser("notify")
    n.add_argument("--month", default=dt.date.today().strftime("%Y-%m"))
    args = ap.parse_args()
    if args.cmd == "run":
        run(args.month, [p for p in args.providers.split(",") if p], [s for s in args.sets.split(",") if s], args.limit)
    else:
        notify(args.month)


if __name__ == "__main__":
    main()
