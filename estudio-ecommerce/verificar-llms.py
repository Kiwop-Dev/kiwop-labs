#!/usr/bin/env python3
"""
Verificación posterior del estudio (paso 3, antes de publicar): re-lee cada llms.txt
detectado y clasifica su ORIGEN, y detecta la plataforma de cada tienda por su home.

Por qué existe: la medición del 2-sep-2026 daba 86/300 tiendas con llms.txt y, al
leerlos, 39 eran la plantilla automática de Shopify («# Agent Instructions — <tienda>»),
no una decisión de la tienda. Publicar "el 29 % ya habla con la IA" sin esa distinción
sería falso. Este paso deja el dato en tres cifras: llms.txt verificado, de plantilla
de plataforma y propio.

Uso (en el servidor, tras aggregate.py y antes de --publish):
  verificar-llms.py /home/kiwop-astro/labs-runs/ecommerce-ia/2026-09

Escribe <dir>/verificacion.json y enriquece <dir>/agregado.json (por tienda:
`llms_txt_verificado`, `llms_txt_origen`, `plataforma`; por bloque: `llms_txt_verificado_pct`,
`llms_txt_plataforma_pct`, `llms_txt_propio_pct`, `plataformas`). Idempotente.
"""
import concurrent.futures as cf
import datetime as dt
import json
import pathlib
import ssl
import sys
import urllib.request
from collections import Counter

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36 KiwopResearchBot/1.0 (+https://www.kiwop.com/labs)"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# Firmas de plataforma por la home (indicativas: una tienda que no responde al
# fetch identificado queda como "sin respuesta", nunca se adivina).
SIGNATURES = [
    ("Shopify", lambda h, b: "shopify" in h or "cdn.shopify.com" in b),
    ("Salesforce Commerce Cloud", lambda h, b: "demandware" in b or "salesforce" in h),
    ("PrestaShop", lambda h, b: "prestashop" in b or "/modules/ps_" in b or "/themes/classic/" in b),
    ("Magento / Adobe Commerce", lambda h, b: "magento" in b or "/static/version" in b or "requirejs-config" in b),
    ("WooCommerce", lambda h, b: "woocommerce" in b),
    ("VTEX", lambda h, b: "vtex" in b or "vtex" in h),
    ("BigCommerce", lambda h, b: "bigcommerce" in b),
    ("Shopware", lambda h, b: "shopware" in b),
    ("SAP Commerce", lambda h, b: "hybris" in b or "/_ui/responsive/" in b),
    ("commercetools", lambda h, b: "commercetools" in b),
]


def get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
        body = r.read(limit).decode("utf-8", "ignore")
        hdr = " ".join(f"{k}:{v}" for k, v in r.headers.items()).lower()
        return r.status, r.headers.get("content-type", ""), hdr, body


def check_llms(dom):
    for scheme in ("https://www.", "https://"):
        try:
            status, ct, hdr, body = get(scheme + dom + "/llms.txt", 6000)
        except Exception:  # noqa: BLE001
            continue
        low = body.lower()
        if "<html" in low[:300] or "<!doctype" in low[:100] or "text/html" in ct:
            return dom, "html"  # una página cualquiera con 200: falso positivo
        first = [l.strip() for l in body.splitlines() if l.strip()][:2]
        if "shopify" in hdr or first and first[0].lower().startswith("# agent instructions"):
            return dom, "plataforma:shopify"
        return dom, "propio"
    return dom, "sin respuesta"


def detect_platform(dom):
    for scheme in ("https://www.", "https://"):
        try:
            _, _, hdr, body = get(scheme + dom + "/", 400000)
        except Exception:  # noqa: BLE001
            continue
        low = body.lower()
        for name, fn in SIGNATURES:
            try:
                if fn(hdr, low):
                    return dom, name
            except Exception:  # noqa: BLE001
                pass
        return dom, "otra/propia"
    return dom, "sin respuesta"


def main():
    d = pathlib.Path(sys.argv[1])
    agg_path = d / "agregado.json"
    out = json.loads(agg_path.read_text())
    rows = out["tiendas"]
    positives = [r["dominio"] for r in rows if r.get("llms_txt")]
    with cf.ThreadPoolExecutor(16) as ex:
        llms = dict(ex.map(check_llms, positives))
    with cf.ThreadPoolExecutor(20) as ex:
        plat = dict(ex.map(detect_platform, [r["dominio"] for r in rows]))

    for r in rows:
        k = llms.get(r["dominio"])
        r["llms_txt_verificado"] = k in ("propio", "plataforma:shopify")
        r["llms_txt_origen"] = k  # propio | plataforma:shopify | html | sin respuesta | None
        r["plataforma"] = plat.get(r["dominio"])

    def enrich(block, sub):
        n = len(sub)
        ver = [r for r in sub if r["llms_txt_verificado"]]
        block["llms_txt_verificado_pct"] = round(100 * len(ver) / n, 1) if n else None
        block["llms_txt_plataforma_pct"] = round(100 * sum(1 for r in ver if r["llms_txt_origen"].startswith("plataforma")) / n, 1) if n else None
        block["llms_txt_propio_pct"] = round(100 * sum(1 for r in ver if r["llms_txt_origen"] == "propio") / n, 1) if n else None
        block["plataformas"] = Counter(r["plataforma"] for r in sub).most_common()

    enrich(out["total"], rows)
    for k, block in out["por_tramo"].items():
        enrich(block, [r for r in rows if r["tramo"] == k])
    for k, block in out["por_sector"].items():
        enrich(block, [r for r in rows if r["sector"] == k])
    out["verificacion"] = {
        "fecha": dt.date.today().isoformat(),
        "llms_txt": Counter(llms.values()).most_common(),
        "nota": "Cada llms.txt detectado se volvió a leer: 'propio' = texto escrito por la tienda; 'plataforma:shopify' = la plantilla automática «Agent Instructions» que Shopify sirve en todas sus tiendas; 'html' = un 200 con una página normal (falso positivo, se descuenta); 'sin respuesta' = no respondió al releerlo (se descuenta). La plataforma se detecta por firmas en la home con un fetch identificado; 'sin respuesta' es la tienda que no lo sirve a un bot, no una plataforma.",
    }
    agg_path.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    (d / "verificacion.json").write_text(json.dumps({"llms_txt": llms, "plataformas": plat}, ensure_ascii=False, indent=1))
    t = out["total"]
    print(f"llms.txt: detectados {len(positives)} → verificados {t['llms_txt_verificado_pct']} % "
          f"(plataforma {t['llms_txt_plataforma_pct']} %, propio {t['llms_txt_propio_pct']} %) · "
          f"origen: {Counter(llms.values()).most_common()} · plataformas: {t['plataformas'][:6]}")


if __name__ == "__main__":
    main()
