/**
 * Kiwop Labs · comprobador: medición de UN dominio.
 *
 * Mismas señales que el estudio «IA en el ecommerce español» (scripts/labs/
 * estudio-ecommerce): robots.txt y crawlers de IA, llms.txt (y si es propio o la
 * plantilla de la plataforma), JSON-LD de la home, ficha de producto con
 * Product/Offer, chatbot y buscador. Doble pasada por señal como en el estudio:
 * HTTP con UA identificado y, si el WAF nos echa, Chromium headless con UA de
 * navegador normalizado. robots.txt y llms.txt siempre por HTTP simple.
 *
 * Diferencias con el estudio, a propósito:
 *  - Todas las peticiones pasan por safeFetch: redirecciones a mano y cada salto
 *    comprobado contra assertPublicHost (anti-SSRF: el dominio lo teclea cualquiera).
 *  - Devuelve además una puntuación 0-100 (puntuar) con el método versionado
 *    (METHOD_VERSION en store.mjs). Cambiar un peso es cambiar la versión.
 *
 * Reutiliza los helpers exportados por medir.mjs para que las dos herramientas
 * midan exactamente igual (parser de robots, política por bot, tipos JSON-LD).
 */
import {
  AI_BOTS, CHAT_VENDORS, SEARCH_VENDORS, PRODUCT_URL_HINTS,
  parseRobots, policyFor, extractJsonLdTypes,
} from '../estudio-ecommerce/medir.mjs';
import { assertPublicHost, METHOD_VERSION } from './store.mjs';

export const UA_RESEARCH = 'KiwopResearchBot/1.0 (+https://www.kiwop.com/labs; comprobador IA ecommerce; info@kiwop.com)';
export const UA_BROWSER = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36';
const TIMEOUT_MS = 20_000;
const MAX_BODY = 2_500_000;
const NAV_TIMEOUT = 40_000;
const SETTLE_MS = 2500;

const detect = (html, table) => table.filter(([, re]) => re.test(html)).map(([name]) => name);

/**
 * Firmas de plataforma en la home (indicativas, como en verificar-llms.py). Firmas
 * TÉCNICAS (assets, variables globales, generator), no menciones en el texto: una web
 * que HABLA de PrestaShop no es PrestaShop.
 */
export const PLATFORM_SIGNATURES = [
  ['Shopify', /cdn\.shopify\.com|Shopify\.theme|window\.Shopify/i],
  ['PrestaShop', /\/modules\/ps_|var prestashop\s*=|content=["']PrestaShop|\/themes\/[^"']+\/assets\/js\/theme\.js/i],
  ['WooCommerce', /wp-content\/plugins\/woocommerce|woocommerce\.min\.js|class=["'][^"']*\bwoocommerce\b/i],
  ['Magento', /Magento_[A-Za-z]+\/|\/static\/version\d+\/frontend\/|mage\/requirejs/i],
  ['Salesforce Commerce Cloud', /demandware\.static|\/on\/demandware\.store|dwstatic/i],
  ['BigCommerce', /cdn\d*\.bigcommerce\.com|stencil-utils/i],
  ['VTEX', /vteximg\.com\.br|vtexassets\.com|vtex\.render-runtime/i],
  ['Wix', /static\.wixstatic\.com|static\.parastorage\.com/i],
  ['Squarespace', /static1\.squarespace\.com|content=["']Squarespace/i],
  ['Shopware', /\/bundles\/storefront\/|shopware-storefront/i],
  ['Webflow', /content=["']Webflow|assets\.website-files\.com/i],
];

/* ---------------------------------------------------------------- fetch */

/**
 * fetch con redirecciones manuales: cada salto se valida (host público, http/https,
 * mismo límite de tamaño). Devuelve { ok, status, url, body, error, hops }.
 */
export async function safeFetch(url, { timeout = TIMEOUT_MS, ua = UA_RESEARCH, maxRedirects = 5, accept = '*/*' } = {}) {
  let current = url;
  let hops = 0;
  for (;;) {
    let u;
    try {
      u = new URL(current);
    } catch {
      return { ok: false, status: 0, url: current, body: '', error: 'url' };
    }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return { ok: false, status: 0, url: current, body: '', error: 'protocol' };
    if (u.port && u.port !== '80' && u.port !== '443') return { ok: false, status: 0, url: current, body: '', error: 'port' };
    try {
      await assertPublicHost(u.hostname);
    } catch (e) {
      return { ok: false, status: 0, url: current, body: '', error: e.code === 'PRIVATE' ? 'private_host' : 'dns' };
    }
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(current, {
        signal: ctrl.signal,
        redirect: 'manual',
        headers: { 'user-agent': ua, accept, 'accept-language': 'es-ES,es;q=0.9,en;q=0.5' },
      });
      if (res.status >= 300 && res.status < 400 && res.headers.get('location')) {
        if (++hops > maxRedirects) return { ok: false, status: res.status, url: current, body: '', error: 'redirects' };
        current = new URL(res.headers.get('location'), current).toString();
        clearTimeout(t);
        continue;
      }
      let body = '';
      if (res.ok) {
        const reader = res.body?.getReader();
        if (reader) {
          const chunks = [];
          let size = 0;
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            size += value.byteLength;
            chunks.push(value);
            if (size > MAX_BODY) {
              await reader.cancel().catch(() => {});
              break;
            }
          }
          body = Buffer.concat(chunks).toString('utf8');
        }
      }
      return { ok: res.ok, status: res.status, url: current, body, hops, contentType: res.headers.get('content-type') || '', cfMitigated: res.headers.get('cf-mitigated') || null };
    } catch (err) {
      return { ok: false, status: 0, url: current, body: '', error: String(err.name === 'AbortError' ? 'timeout' : err.cause?.code || err.name || err) };
    } finally {
      clearTimeout(t);
    }
  }
}

/* ------------------------------------------------------------- headless */

/** Pestaña nueva con bloqueo de recursos pesados y de hosts no públicos. */
export async function renderPage(browser, url) {
  const context = await browser.newContext({
    userAgent: UA_BROWSER,
    locale: 'es-ES',
    viewport: { width: 1366, height: 900 },
    ignoreHTTPSErrors: false,
  });
  const page = await context.newPage();
  try {
    await page.route('**/*', (route) => {
      const req = route.request();
      const t = req.resourceType();
      if (t === 'image' || t === 'media' || t === 'font') return route.abort();
      let h;
      try {
        h = new URL(req.url()).hostname;
      } catch {
        return route.abort();
      }
      // Solo comprobación barata (literal): el DNS de cada subrecurso sería carísimo.
      if (h === 'localhost' || /^\d+\.\d+\.\d+\.\d+$/.test(h) || h.includes(':') || /\.(local|internal|lan)$/.test(h)) return route.abort();
      return route.continue();
    });
    const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT });
    await page.waitForTimeout(SETTLE_MS);
    const html = await page.content();
    return { ok: Boolean(resp) && resp.status() < 400, status: resp ? resp.status() : 0, url: page.url(), body: html };
  } catch (err) {
    return { ok: false, status: 0, url, body: '', error: String(err.message || err).split('\n')[0] };
  } finally {
    await context.close().catch(() => {});
  }
}

/* --------------------------------------------------- ficha de producto */

const locsOf = (xml) => [...xml.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/gi)].map((m) => m[1]);
const CATEGORY_RE = /\/(c|cat|categoria|categorias|category|categories|collections?|colecciones?|tienda|shop|blog|noticias|news)(\/|$)/i;
const looksProduct = (u) => {
  try {
    const path = new URL(u).pathname;
    return PRODUCT_URL_HINTS.some((re) => re.test(path)) && !CATEGORY_RE.test(path);
  } catch {
    return false;
  }
};
const pickProduct = (urls) => urls.find(looksProduct) || null;

async function productoViaSitemap(sitemaps, base) {
  const list = (sitemaps || []).length ? sitemaps : [`${base}/sitemap.xml`, `${base}/sitemap_index.xml`];
  for (const sm of list.slice(0, 3)) {
    const res = await safeFetch(sm, { accept: 'application/xml,text/xml,*/*' });
    if (!res.ok || !res.body) continue;
    const locs = locsOf(res.body);
    if (!locs.length) continue;
    if (/<sitemapindex/i.test(res.body)) {
      const candidates = [locs.find((u) => /product|producto|item/i.test(u)), locs[0]].filter(Boolean);
      for (const c of candidates.slice(0, 2)) {
        const sub = await safeFetch(c, { accept: 'application/xml,text/xml,*/*' });
        if (!sub.ok) continue;
        const subLocs = locsOf(sub.body);
        const prod = pickProduct(subLocs);
        if (prod) return { url: prod, via: 'sitemap' };
        // Sin URL que huela a ficha: solo si el propio sitemap se llama «product» nos
        // fiamos de que su contenido son fichas y cogemos una del medio.
        if (/product|producto/i.test(c) && subLocs.length > 20) return { url: subLocs[Math.floor(subLocs.length / 2)], via: 'sitemap' };
      }
      continue;
    }
    const prod = pickProduct(locs);
    if (prod) return { url: prod, via: 'sitemap' };
  }
  return null;
}

/**
 * Candidatas a ficha desde los enlaces de la home (mismo host): primero las que
 * huelen a producto por la URL, después las de último segmento «rico» (3+ guiones,
 * típico de Magento/Woo con la ficha en la raíz). Se devuelven varias porque el
 * medidor las prueba en orden hasta encontrar un JSON-LD Product.
 */
function candidatasViaHome(html, baseUrl, max = 4) {
  let host;
  try {
    host = new URL(baseUrl).host.replace(/^www\./, '');
  } catch {
    return [];
  }
  const hrefs = [...html.matchAll(/href=["']([^"'#?]+)[^"']*["']/gi)].map((m) => m[1]);
  const seen = new Set();
  const hint = [];
  const rich = [];
  for (const h of hrefs) {
    let abs;
    try {
      abs = new URL(h, baseUrl);
    } catch {
      continue;
    }
    if (abs.host.replace(/^www\./, '') !== host) continue;
    const path = abs.pathname;
    if (/\.(css|js|json|xml|png|jpe?g|webp|avif|svg|gif|pdf|ico|woff2?)$/i.test(path)) continue;
    const key = abs.origin + path;
    if (seen.has(key) || CATEGORY_RE.test(path)) continue;
    seen.add(key);
    if (looksProduct(abs.toString())) hint.push(key);
    else {
      const last = path.split('/').filter(Boolean).pop() || '';
      if ((last.match(/-/g) || []).length >= 3 && path.split('/').filter(Boolean).length <= 3) rich.push(key);
    }
  }
  return [...hint, ...rich].slice(0, max);
}

/* ------------------------------------------------------------- llms.txt */

function clasificarLlms(res) {
  if (!res.ok || !res.body) return { existe: false, origen: null };
  const body = res.body.trimStart();
  if (/^\s*<(!doctype|html)/i.test(body) || /text\/html/i.test(res.contentType || '')) return { existe: false, origen: 'html' };
  const first = body.split(/\r?\n/)[0] || '';
  if (/^#\s*agent instructions/i.test(first) || /shopify/i.test(first)) return { existe: true, origen: 'plataforma:shopify' };
  return { existe: true, origen: 'propio', lineas: body.split(/\r?\n/).length, bytes: Buffer.byteLength(body) };
}

/* ------------------------------------------------------------ puntuación */

const ENTITY_TYPES = ['Organization', 'OnlineStore', 'Store', 'LocalBusiness', 'WebSite', 'Corporation'];

/**
 * 0-100. Cada check: { id, puntos, max, estado: ok|parcial|ko|na, detalle }.
 * Los pesos son el método (METHOD_VERSION); están explicados en la página.
 */
export function puntuar(r) {
  if (r.no_medible) return null;
  const checks = [];
  const push = (id, puntos, max, estado, detalle = {}) => checks.push({ id, puntos: Math.round(puntos), max, estado, ...detalle });

  // 1. Crawlers de IA (25): ¿los deja entrar robots.txt? Solo cuenta el bloqueo TOTAL
  //    (Disallow: /), como en el estudio: un Disallow de /carrito para todos los bots
  //    no es cerrarle la puerta a la IA.
  if (r.robots_existe) {
    const total = AI_BOTS.length;
    const bloqueados = (r.ia_bloqueados || []).length;
    const pts = 25 * ((total - bloqueados) / total);
    push('crawlers', pts, 25, bloqueados === 0 ? 'ok' : bloqueados === total ? 'ko' : 'parcial', { bloqueados: r.ia_bloqueados });
  } else {
    push('crawlers', 15, 25, 'parcial', { sin_robots: true });
  }

  // 2. Acceso a un bot identificado (10)
  if (r.home_via === 'http') push('acceso', 10, 10, 'ok');
  else if (r.home_via === 'headless') push('acceso', 4, 10, 'parcial', { status_http: r.home_status_http });
  else push('acceso', 0, 10, 'ko', { status_http: r.home_status_http });

  // 3. llms.txt (15)
  if (r.llms_txt && r.llms_origen === 'propio') push('llms', 15, 15, 'ok');
  else if (r.llms_txt) push('llms', 8, 15, 'parcial', { origen: r.llms_origen });
  else push('llms', 0, 15, 'ko');

  // 4. Datos estructurados en la home (10)
  const types = r.home_jsonld_types || [];
  const rotos = r.home_jsonld_rotos || 0;
  if (types.some((t) => ENTITY_TYPES.includes(t))) push('schema_home', Math.max(0, 10 - rotos * 3), 10, rotos ? 'parcial' : 'ok', { tipos: types, rotos });
  else if (types.length) push('schema_home', Math.max(0, 5 - rotos * 3), 10, 'parcial', { tipos: types, rotos });
  else push('schema_home', 0, 10, r.home_via ? 'ko' : 'na', { rotos });

  // 5. Ficha de producto (25)
  if (!r.producto_url) push('producto', 0, 25, 'ko', { encontrada: false });
  else if (r.producto_tiene_Product && r.producto_tiene_Offer) push('producto', 25, 25, 'ok', { url: r.producto_url });
  else if (r.producto_tiene_Product) push('producto', 15, 25, 'parcial', { url: r.producto_url, sin_offer: true });
  else if (r.producto_status && r.producto_status >= 200 && r.producto_status < 400) push('producto', 0, 25, 'ko', { url: r.producto_url, tipos: r.producto_jsonld_types });
  else push('producto', 0, 25, 'na', { url: r.producto_url, status: r.producto_status });

  // 6. Sitemap declarado (10)
  if ((r.sitemaps || []).length) push('sitemap', 10, 10, 'ok', { n: r.sitemaps.length });
  else if (r.sitemap_descubierto) push('sitemap', 6, 10, 'parcial');
  else push('sitemap', 0, 10, 'ko');

  // 7. HTTPS en la URL final (5)
  push('https', r.url_final?.startsWith('https://') ? 5 : 0, 5, r.url_final?.startsWith('https://') ? 'ok' : 'ko');

  const total = Math.max(0, Math.min(100, checks.reduce((a, c) => a + c.puntos, 0)));
  const veredicto = total >= 80 ? 'preparada' : total >= 55 ? 'a-medias' : 'invisible';
  return { total, veredicto, checks, version: METHOD_VERSION };
}

/* ------------------------------------------------------------- medición */

/**
 * Mide un dominio. `browser` es opcional (Playwright ya lanzado); sin él no hay
 * segunda pasada y el informe lo dice (home_via === null cuando el HTTP falla).
 */
export async function medirDominio(domain, { browser = null, log = () => {} } = {}) {
  const started = Date.now();
  const r = {
    dominio: domain,
    metodo_version: METHOD_VERSION,
    medido_en: new Date().toISOString(),
    errores: [],
  };

  // 0. URL final (https → http, www o no): una petición ligera a la raíz.
  let base = `https://${domain}`;
  let home = await safeFetch(base, { accept: 'text/html,*/*' });
  if (!home.ok && home.status === 0 && home.error !== 'private_host' && home.error !== 'dns') {
    const alt = await safeFetch(`http://${domain}`, { accept: 'text/html,*/*' });
    if (alt.ok || alt.status) home = alt;
  }
  if (home.error === 'private_host' || home.error === 'dns') {
    r.errores.push(`home: ${home.error}`);
    r.url_final = base;
    r.puntuacion = puntuar(r);
    r.duracion_ms = Date.now() - started;
    return r;
  }
  r.home_status_http = home.status;
  r.url_final = home.url || base;
  try {
    base = new URL(r.url_final).origin;
  } catch {
    /* base queda */
  }
  r.origen = base;
  log(`  home http ${home.status} → ${r.url_final}`);

  // 1. robots.txt (siempre HTTP simple, sobre el origen final)
  const robots = await safeFetch(`${base}/robots.txt`, { accept: 'text/plain,*/*' });
  r.robots_status = robots.status;
  if (robots.cfMitigated) r.robots_cf_mitigated = robots.cfMitigated;
  if (robots.ok && robots.body && !/^\s*<(!doctype|html)/i.test(robots.body)) {
    const { groups, sitemaps } = parseRobots(robots.body);
    r.robots_existe = true;
    r.sitemaps = sitemaps;
    r.ia_crawlers = {};
    for (const bot of AI_BOTS) r.ia_crawlers[bot] = policyFor(groups, bot);
    r.ia_bloqueados = Object.entries(r.ia_crawlers).filter(([, v]) => v.policy === 'blocked').map(([k]) => k);
    r.ia_con_regla_propia = Object.entries(r.ia_crawlers).filter(([, v]) => v.explicit).map(([k]) => k);
  } else {
    r.robots_existe = false;
    r.sitemaps = [];
    r.ia_crawlers = {};
    r.ia_bloqueados = [];
    r.ia_con_regla_propia = [];
    if (!robots.ok) r.errores.push(`robots.txt: ${robots.error || robots.status}`);
  }

  // 2. llms.txt
  const llms = await safeFetch(`${base}/llms.txt`, { accept: 'text/plain,text/markdown,*/*' });
  r.llms_status = llms.status;
  const cl = clasificarLlms(llms);
  r.llms_txt = cl.existe;
  r.llms_origen = cl.origen;
  if (cl.lineas) {
    r.llms_lineas = cl.lineas;
    r.llms_bytes = cl.bytes;
  }

  // 3. Home: HTTP identificado y, si no, headless.
  let homeHtml = '';
  r.home_via = null;
  if (home.ok && home.body && /<html|<body|<div/i.test(home.body)) {
    homeHtml = home.body;
    r.home_via = 'http';
  } else if (browser) {
    log('  home headless…');
    const h = await renderPage(browser, r.url_final);
    r.home_status_headless = h.status;
    if (h.ok && h.body) {
      homeHtml = h.body;
      r.home_via = 'headless';
      r.url_final = h.url || r.url_final;
    } else if (h.error) r.errores.push(`home headless: ${h.error}`);
  }
  // Ni robots.txt ni la home (por bot ni por navegador): la tienda bloquea a NUESTRO
  // medidor (Cloudflare challenge a IP de datacenter, WAF) o no responde. No se puntúa:
  // un 20/100 aquí sería mentira sobre la tienda y verdad solo sobre nuestro servidor.
  if (!homeHtml && !robots.ok) {
    r.no_medible = true;
    r.bloqueo = robots.cfMitigated || home.cfMitigated ? 'challenge' : home.status === 0 && robots.status === 0 ? 'timeout' : 'forbidden';
    r.puntuacion = null;
    r.duracion_ms = Date.now() - started;
    return r;
  }
  if (homeHtml) {
    const ld = extractJsonLdTypes(homeHtml);
    r.home_jsonld_types = ld.types;
    r.home_jsonld_blocks = ld.blocks;
    r.home_jsonld_rotos = ld.broken;
    r.chatbot = detect(homeHtml, CHAT_VENDORS);
    r.buscador = detect(homeHtml, SEARCH_VENDORS);
    r.plataforma = PLATFORM_SIGNATURES.find(([, re]) => re.test(homeHtml))?.[0] || null;
    r.titulo = (homeHtml.match(/<title[^>]*>([^<]{1,200})/i)?.[1] || '').trim() || null;
  } else {
    r.home_jsonld_types = [];
    r.home_jsonld_blocks = 0;
    r.home_jsonld_rotos = 0;
    r.chatbot = [];
    r.buscador = [];
    r.plataforma = null;
    r.errores.push(`home: ${home.error || home.status}`);
  }

  // 4. Ficha de producto: sitemap (HTTP) o enlaces de la home; HTTP y, si no, headless.
  //    Se prueban hasta 4 candidatas y se queda con la primera que lleve JSON-LD Product;
  //    si ninguna lo lleva, el informe enseña la primera probada (sin Product).
  try {
    const candidatas = [];
    const viaSitemap = await productoViaSitemap(r.sitemaps, base);
    if (viaSitemap) {
      candidatas.push(viaSitemap);
      if (!(r.sitemaps || []).length) r.sitemap_descubierto = true;
    }
    if (homeHtml) for (const u of candidatasViaHome(homeHtml, r.url_final)) candidatas.push({ url: u, via: 'home-links' });
    r.producto_url = null;
    r.producto_via = null;
    r.producto_candidatas = candidatas.length;
    let primera = null;
    let headlessTries = 0;
    for (const cand of candidatas.slice(0, 4)) {
      const prod = await safeFetch(cand.url, { accept: 'text/html,*/*' });
      let prodHtml = prod.ok && prod.body ? prod.body : '';
      let statusHeadless;
      // Si el WAF echa al bot (la home ya fue por navegador), las candidatas van por
      // navegador también, hasta tres: si no, solo la primera.
      if (!prodHtml && browser && (cand === candidatas[0] || (r.home_via === 'headless' && headlessTries < 3))) {
        headlessTries++;
        log('  producto headless…');
        const h = await renderPage(browser, cand.url);
        statusHeadless = h.status;
        if (h.ok && h.body) prodHtml = h.body;
      }
      const ld = prodHtml ? extractJsonLdTypes(prodHtml) : { types: [], blocks: 0, broken: 0 };
      const info = { url: cand.url, via: cand.via, status: prod.status, status_headless: statusHeadless, tipos: ld.types, error: prodHtml ? undefined : prod.error };
      if (!primera) primera = info;
      if (ld.types.includes('Product')) {
        primera = info;
        break;
      }
    }
    if (primera) {
      r.producto_url = primera.url;
      r.producto_via = primera.via;
      r.producto_status = primera.status;
      if (primera.status_headless) r.producto_status_headless = primera.status_headless;
      r.producto_jsonld_types = primera.tipos;
      r.producto_tiene_Product = primera.tipos.includes('Product');
      r.producto_tiene_Offer = primera.tipos.some((t) => /Offer/i.test(t));
      if (primera.error) r.errores.push(`producto: ${primera.error}`);
    }
  } catch (e) {
    r.errores.push(`producto: ${String(e.message || e)}`);
  }

  r.puntuacion = puntuar(r);
  r.duracion_ms = Date.now() - started;
  return r;
}
