#!/usr/bin/env python3
"""
Kiwop Labs · Observatorio de precios publicados de servicios digitales en España.

Recaptura trimestral de los precios que cada proveedor PUBLICA en su propia web
(scripts/labs/precios/fuentes.json, verificado a mano la primera vez). Solo
biblioteca estándar. El script no inventa ni corrige precios: comprueba que el
precio anotado sigue publicado y agrega por servicio. Un precio que ya no se
encuentra se conserva con su último valor conocido y su estado, y NO entra en las
medianas. Ver docs/LABS.md («Observatorio de precios publicados»).

Uso:
  precios.py run --period AAAA-MM [--out public/labs/data/precios] [--fuentes ...]
  precios.py check [--fuentes ...]      # solo imprime qué fuentes ya no se verifican
"""
import argparse
import datetime as dt
import gzip
import html
import json
import os
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

UA = 'KiwopResearchBot/1.0 (+https://www.kiwop.com/labs; observatorio precios; info@kiwop.com)'
TIMEOUT = 20
COURTESY_SECONDS = 1.0
SERIES = 'precios'
PAGE_URL = 'https://www.kiwop.com/labs/precios-servicios-digitales-espana'
METODO = (
    'Precios publicados por el propio proveedor en su web, anotados a mano (texto literal y URL) '
    'y recapturados por script: se descarga cada URL identificándose como KiwopResearchBot, se '
    'extrae el texto visible descartando los precios tachados y se comprueba que el texto literal '
    '(o el importe junto al símbolo €) sigue publicado. Solo los precios verificados en la captura '
    'entran en las medianas; el resto se conserva con su último valor conocido y su estado. '
    'Sin IVA salvo que la página lo incluya; «desde» es el mínimo anunciado.'
)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
DEFAULT_FUENTES = os.path.join(HERE, 'fuentes.json')
DEFAULT_OUT = os.path.join(REPO, 'public', 'labs', 'data', 'precios')

# Familias de unidad: cómo se agregan las unidades anotadas en fuentes.json.
UNIDAD_FAMILIA = {
    '€': 'cerrado',
    '€ desde': 'cerrado',
    '€/mes': 'mensual',
    '€/mes desde': 'mensual',
    '€/h': 'hora',
}
FAMILIAS = {
    'cerrado': 'Precio cerrado o mínimo anunciado (€)',
    'mensual': 'Cuota mensual (€/mes)',
    'hora': 'Hora de trabajo (€/h)',
}

# ---------------------------------------------------------------- descarga

_last_hit = {}


def fetch(url):
    """Descarga una URL con cortesía por host. Devuelve (status, texto_html | None, error | None)."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    waited = time.time() - _last_hit.get(host, 0)
    if waited < COURTESY_SECONDS:
        time.sleep(COURTESY_SECONDS - waited)
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'es-ES,es;q=0.9,ca;q=0.8,en;q=0.5',
        'Accept-Encoding': 'gzip, identity',
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            status = resp.status
            enc = (resp.headers.get('Content-Encoding') or '').lower()
            ctype = resp.headers.get('Content-Type') or ''
    except urllib.error.HTTPError as e:
        return e.code, None, f'HTTP {e.code}'
    except Exception as e:  # timeout, DNS, TLS, conexión rechazada
        return None, None, f'{type(e).__name__}: {e}'
    finally:
        _last_hit[host] = time.time()
    if 'gzip' in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    charset = None
    m = re.search(r'charset=([\w-]+)', ctype, re.I)
    if m:
        charset = m.group(1)
    else:
        m = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', raw[:4096], re.I)
        if m:
            charset = m.group(1).decode('ascii', 'ignore')
    for cs in (charset, 'utf-8', 'latin-1'):
        if not cs:
            continue
        try:
            return status, raw.decode(cs), None
        except (UnicodeDecodeError, LookupError):
            continue
    return status, raw.decode('utf-8', 'ignore'), None


# ------------------------------------------------------------ texto visible

_STRIKE_CLASS = re.compile(r'tachado|old-price|line-through', re.I)
_TAG_OPEN = re.compile(r'<([a-zA-Z][\w:-]*)\b([^>]*)>', re.S)


def _drop_element(html_text, start):
    """Devuelve el índice del final del elemento cuya etiqueta de apertura empieza en `start`."""
    m = _TAG_OPEN.match(html_text, start)
    if not m:
        return start + 1
    tag = m.group(1).lower()
    if m.group(2).rstrip().endswith('/') or tag in ('br', 'img', 'input', 'hr', 'meta', 'link'):
        return m.end()
    depth = 1
    pos = m.end()
    pat = re.compile(r'<(/?)' + re.escape(tag) + r'\b[^>]*>', re.I | re.S)
    while depth:
        n = pat.search(html_text, pos)
        if not n:
            return len(html_text)
        depth += -1 if n.group(1) else 1
        pos = n.end()
    return pos


def visible_text(html_text):
    """Texto visible: sin script/style/comentarios, sin precios tachados, entidades decodificadas."""
    t = re.sub(r'<!--.*?-->', ' ', html_text, flags=re.S)
    t = re.sub(r'<(script|style|noscript|template|svg)\b[^>]*>.*?</\1\s*>', ' ', t, flags=re.S | re.I)
    # Elementos tachados por etiqueta (<del>, <s>, <strike>).
    t = re.sub(r'<(del|s|strike)\b[^>]*>.*?</\1\s*>', ' ', t, flags=re.S | re.I)
    # Elementos tachados por clase o estilo (precio antiguo).
    out = []
    pos = 0
    for m in _TAG_OPEN.finditer(t):
        if m.start() < pos:
            continue
        attrs = m.group(2)
        cls = re.search(r'class\s*=\s*(["\'])(.*?)\1', attrs, re.S | re.I)
        sty = re.search(r'style\s*=\s*(["\'])(.*?)\1', attrs, re.S | re.I)
        if (cls and _STRIKE_CLASS.search(cls.group(2))) or (sty and 'line-through' in sty.group(2).lower()):
            out.append(t[pos:m.start()])
            pos = _drop_element(t, m.start())
    out.append(t[pos:])
    t = ' '.join(out)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = html.unescape(t)
    t = unicodedata.normalize('NFKC', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


# ----------------------------------------------------------- comparación

def norm(s):
    """Comparación tolerante: minúsculas, sin espacios, sin separadores de miles, sin ,00."""
    s = unicodedata.normalize('NFKC', s).lower()
    s = s.replace('euros', '€').replace('eur ', '€').replace('&euro;', '€')
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'(?<=\d)\.(?=\d{3}(?!\d))', '', s)   # 1.500 -> 1500
    s = re.sub(r'(?<=\d)[.,]00(?!\d)', '', s)         # 152,00 -> 152
    return s


def number_variants(precio):
    """Variantes escritas del importe: 1500, 1.500, 1,500, 53,60, 53.60, 53,6..."""
    if float(precio).is_integer():
        n = int(precio)
        s = str(n)
        variants = {s}
        if n >= 1000:
            variants.add(f'{n:,}'.replace(',', '.'))
            variants.add(f'{n:,}')
        for v in list(variants):
            variants.add(v + ',00')
            variants.add(v + '.00')
        return variants
    s = f'{precio:.2f}'
    ip, dp = s.split('.')
    ips = {ip}
    if int(ip) >= 1000:
        ips.add(f'{int(ip):,}'.replace(',', '.'))
        ips.add(f'{int(ip):,}')
    variants = set()
    for i in ips:
        variants.add(f'{i},{dp}')
        variants.add(f'{i}.{dp}')
        if dp.endswith('0'):
            variants.add(f'{i},{dp[0]}')
            variants.add(f'{i}.{dp[0]}')
    return variants


def find_price(text_norm, precio):
    """Busca el importe pegado al símbolo € (antes o después) en el texto normalizado."""
    entero = float(precio).is_integer()
    for v in number_variants(precio):
        vv = norm(v)
        # Decimales en <sup> («1295 00 €») quedan pegados al entero al quitar espacios: se admite «00» suelto.
        sup = r'(?:00)?' if entero and ',' not in vv and '.' not in vv else ''
        pat = r'(?<![\d,])' + re.escape(vv) + sup + r'€|€' + re.escape(vv) + r'(?![\d])'
        if re.search(pat, text_norm):
            return True
    return False


def verify_record(rec, text_norm):
    """Devuelve (verificado, metodo) para un registro contra el texto normalizado de su página."""
    lit = norm(rec['texto_literal'])
    # Con frontera de cifra: «500€» no puede darse por visto dentro de «2500€» ni de «53,500€».
    # El punto sí se permite delante (es el final de la frase anterior; los miles ya van sin punto).
    if lit and re.search(r'(?<![\d,])' + re.escape(lit) + r'(?!\d)', text_norm):
        return True, 'literal'
    if find_price(text_norm, rec['precio']):
        return True, 'numero'
    return False, None


# --------------------------------------------------------------- captura

def capture(fuentes, log=print):
    """Descarga cada URL una vez y verifica todos sus registros. Devuelve la lista de registros."""
    by_url = {}
    for i, rec in enumerate(fuentes['fuentes']):
        by_url.setdefault(rec['url'], []).append(i)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    results = [None] * len(fuentes['fuentes'])
    urls = sorted(by_url, key=lambda u: (urllib.parse.urlsplit(u).netloc, u))
    for k, url in enumerate(urls, 1):
        status, body, err = fetch(url)
        if body is None:
            estado = 'bloqueado' if status else 'sin-respuesta'
            log(f'[{k}/{len(urls)}] {estado:13} {status or "-":>4} {url} ({err})')
            for i in by_url[url]:
                rec = dict(fuentes['fuentes'][i])
                rec.update(verificado=False, estado=estado, http_status=status, capturado_en=now, metodo_verificacion=None)
                results[i] = rec
            continue
        text_norm = norm(visible_text(body))
        for i in by_url[url]:
            rec = dict(fuentes['fuentes'][i])
            ok, metodo = verify_record(rec, text_norm)
            rec.update(verificado=ok, estado='ok' if ok else 'no-encontrado', http_status=status,
                       capturado_en=now, metodo_verificacion=metodo)
            results[i] = rec
        oks = sum(1 for i in by_url[url] if results[i]['verificado'])
        log(f'[{k}/{len(urls)}] {"ok" if oks == len(by_url[url]) else "parcial" if oks else "no-encontrado":13} {status:>4} {url} ({oks}/{len(by_url[url])})')
    return results


# ------------------------------------------------------------- agregación

def quantiles(values):
    vals = sorted(values)
    if not vals:
        return None
    if len(vals) == 1:
        v = vals[0]
        return {'min': v, 'p25': v, 'mediana': v, 'p75': v, 'max': v}
    q = statistics.quantiles(vals, n=4, method='inclusive')
    return {
        'min': vals[0],
        'p25': round(q[0], 2),
        'mediana': round(statistics.median(vals), 2),
        'p75': round(q[2], 2),
        'max': vals[-1],
    }


def aggregate(fuentes, records):
    servicios = fuentes['servicios']
    agg = {}
    for sid in sorted(servicios, key=int):
        sid_i = int(sid)
        recs = [r for r in records if int(r['servicio']) == sid_i]
        por_unidad = {}
        for fam in FAMILIAS:
            fam_recs = [r for r in recs if UNIDAD_FAMILIA.get(r['unidad']) == fam]
            if not fam_recs:
                continue
            ver = [r for r in fam_recs if r['verificado']]
            entry = {
                'unidad': FAMILIAS[fam],
                'n': len(fam_recs),
                'n_verificados': len(ver),
                'n_desde': sum(1 for r in fam_recs if 'desde' in r['unidad']),
            }
            stats = quantiles([float(r['precio']) for r in ver])
            entry.update(stats or {'min': None, 'p25': None, 'mediana': None, 'p75': None, 'max': None})
            por_unidad[fam] = entry
        por_tipo = {}
        for r in recs:
            por_tipo[r['tipo_proveedor']] = por_tipo.get(r['tipo_proveedor'], 0) + 1
        agg[sid] = {
            'servicio': servicios[sid],
            'n': len(recs),
            'n_verificados': sum(1 for r in recs if r['verificado']),
            'proveedores': len({r['proveedor'] for r in recs}),
            'por_unidad': por_unidad,
            'n_por_tipo_proveedor': dict(sorted(por_tipo.items())),
        }
    return agg


def record_key(r):
    return (r['proveedor'], r['url'], int(r['servicio']), r['unidad'])


def apply_previous(prev, agg, records):
    """Añade deltas de mediana y precio_anterior comparando con el periodo anterior."""
    if not prev:
        return
    prev_agg = prev.get('agregado_por_servicio', {})
    for sid, a in agg.items():
        for fam, e in a['por_unidad'].items():
            pe = prev_agg.get(sid, {}).get('por_unidad', {}).get(fam)
            if pe and pe.get('mediana') is not None and e.get('mediana') is not None:
                e['delta_mediana_vs_anterior'] = round(e['mediana'] - pe['mediana'], 2)
                e['delta_mediana_pct_vs_anterior'] = round((e['mediana'] - pe['mediana']) / pe['mediana'] * 100, 1) if pe['mediana'] else None
            else:
                e['delta_mediana_vs_anterior'] = None
    prev_recs = {record_key(r): r for r in prev.get('registros', [])}
    for r in records:
        p = prev_recs.get(record_key(r))
        if not p:
            continue
        if float(p['precio']) != float(r['precio']):
            r['precio_anterior'] = p['precio']
        if not r['verificado'] and p.get('ultima_verificacion'):
            r['ultima_verificacion'] = p['ultima_verificacion']


def load_previous(out_dir, period):
    idx_path = os.path.join(out_dir, 'index.json')
    if not os.path.exists(idx_path):
        return None
    with open(idx_path, encoding='utf-8') as f:
        idx = json.load(f)
    earlier = sorted(m for m in idx.get('months', []) if m < period)
    if not earlier:
        return None
    with open(os.path.join(out_dir, f'{earlier[-1]}.json'), encoding='utf-8') as f:
        return json.load(f)


def write_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write('\n')


# ---------------------------------------------------------------- comandos

def load_fuentes(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def cmd_run(args):
    fuentes = load_fuentes(args.fuentes)
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    prev = load_previous(out_dir, args.period)
    records = capture(fuentes)
    manual = fuentes.get('verificacion_manual')
    for r in records:
        if r['verificado']:
            r['ultima_verificacion'] = r['capturado_en'][:10]
        elif manual:
            r['ultima_verificacion'] = manual
    agg = aggregate(fuentes, records)
    apply_previous(prev, agg, records)
    n_ver = sum(1 for r in records if r['verificado'])
    data = {
        'series': SERIES,
        'periodo': args.period,
        'generado_en': dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
        'metodo': METODO,
        'version_fuentes': fuentes.get('version'),
        'nota_fuentes': fuentes.get('nota'),
        'page': PAGE_URL,
        'license': 'CC BY 4.0',
        'servicios': fuentes['servicios'],
        'familias_unidad': FAMILIAS,
        'agregado_por_servicio': agg,
        'registros': records,
        'resumen': {
            'fuentes': len(records),
            'urls': len({r['url'] for r in records}),
            'proveedores': len({r['proveedor'] for r in records}),
            'verificados': n_ver,
            'verificados_pct': round(n_ver / len(records) * 100, 1) if records else 0,
            'por_estado': {k: sum(1 for r in records if r['estado'] == k) for k in ('ok', 'no-encontrado', 'sin-respuesta', 'bloqueado')},
            'periodo_anterior': prev['periodo'] if prev else None,
        },
    }
    write_json(os.path.join(out_dir, f'{args.period}.json'), data)
    write_json(os.path.join(out_dir, 'latest.json'), data)
    idx_path = os.path.join(out_dir, 'index.json')
    months = []
    if os.path.exists(idx_path):
        with open(idx_path, encoding='utf-8') as f:
            months = json.load(f).get('months', [])
    months = sorted(set(months) | {args.period})
    write_json(idx_path, {'series': SERIES, 'months': months, 'latest': months[-1]})
    print(f'\n{args.period}: {n_ver}/{len(records)} precios verificados ({data["resumen"]["verificados_pct"]} %), '
          f'{data["resumen"]["por_estado"]}')
    print(f'escrito {out_dir}/{args.period}.json, latest.json, index.json')
    bad = [r for r in records if not r['verificado']]
    if bad:
        print('\nSin verificar (revisar a mano):')
        for r in bad:
            print(f'  - {r["estado"]:13} {r["proveedor"]} · servicio {r["servicio"]} · {r["texto_literal"]} · {r["url"]}')


def cmd_check(args):
    fuentes = load_fuentes(args.fuentes)
    records = capture(fuentes, log=lambda s: None)
    bad = [r for r in records if not r['verificado']]
    print(f'{len(records) - len(bad)}/{len(records)} precios verificados')
    if not bad:
        print('Todas las fuentes se verifican.')
        return
    print('Fuentes que ya no se verifican (revisar a mano):')
    for r in bad:
        print(f'  - {r["estado"]:13} {r["http_status"] or "-":>4} {r["proveedor"]} · servicio {r["servicio"]} · '
              f'{r["texto_literal"]} · {r["url"]}')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run', help='recaptura y escribe el dataset del periodo')
    r.add_argument('--period', required=True, help='AAAA-MM')
    r.add_argument('--out', default=DEFAULT_OUT)
    r.add_argument('--fuentes', default=DEFAULT_FUENTES)
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser('check', help='solo imprime qué fuentes ya no se verifican')
    c.add_argument('--fuentes', default=DEFAULT_FUENTES)
    c.set_defaults(fn=cmd_check)
    args = ap.parse_args(argv)
    if getattr(args, 'period', None) and not re.fullmatch(r'\d{4}-\d{2}', args.period):
        ap.error('--period debe ser AAAA-MM')
    args.fn(args)


if __name__ == '__main__':
    main()
