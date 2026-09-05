#!/usr/bin/env node
/**
 * Medición HEADLESS — "Estado de la IA en el ecommerce español 2026"
 *
 * Variante con navegador real (Chromium vía Playwright) para el HTML, decidida
 * tras el piloto: 8/20 tiendas devolvían 403 a un cliente HTTP identificado.
 * Se DECLARA en la metodología del estudio.
 *
 * Reparto honesto de métodos:
 *   - robots.txt y llms.txt → HTTP simple con UA de investigación (son ficheros
 *     para bots; medirlos como bot es lo correcto).
 *   - Home y ficha de producto → navegador headless real (lo que vería una
 *     persona, y también un agente que navega con navegador).
 *
 * Diferencia metodológica relevante que este script captura: el JSON-LD puede
 * inyectarse por JavaScript. El fetch estático no lo ve; el headless sí. Ambos
 * datos son verdad, pero responden a preguntas distintas (¿qué ve un crawler
 * sin JS? vs ¿qué hay en el DOM final?). Registramos el DOM final.
 *
 * Uso:
 *   node medir-headless.mjs                # tiendas-piloto.json → resultados-headless/
 *   node medir-headless.mjs --concurrency=2 --out=resultados-headless
 *
 * Requiere: npm i playwright (los navegadores ya están en la caché de ms-playwright).
 */

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import {
  AI_BOTS, CHAT_VENDORS, SEARCH_VENDORS, PRODUCT_URL_HINTS,
  fetchText, parseRobots, policyFor, extractJsonLdTypes
} from './medir.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Robustez para la muestra grande (sep-2026): un 'error' no manejado del socket
// HTTP/2 de undici (UND_ERR_SOCKET "other side closed") tumbaba el proceso entero
// a mitad de la pasada. Lo registramos y seguimos; la tienda afectada queda con
// su error en `errores`, que es el dato honesto.
process.on('uncaughtException', (e) => { console.error('⚠ uncaughtException (seguimos):', e?.code || e?.message || e); });
process.on('unhandledRejection', (e) => { console.error('⚠ unhandledRejection (seguimos):', e?.code || e?.message || e); });

const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const [k, v = true] = a.replace(/^--/, '').split('=');
    return [k, v];
  })
);

const OUT = args.out || 'resultados-headless';
const CONCURRENCY = Number(args.concurrency || 2);
const NAV_TIMEOUT = Number(args.timeout || 45000);
const SETTLE_MS = 2500; // margen para que el JS inyecte schema/widgets

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const detect = (html, table) =>
  table.filter(([, re]) => re.test(html)).map(([name]) => name);

/* -------------------------------------------------- navegación con Playwright */

/**
 * Carga una URL en una pestaña nueva y devuelve status + HTML del DOM final.
 * Bloquea imágenes/vídeo/fuentes: no las necesitamos y aceleran 5-10×.
 */
async function renderPage(context, url) {
  const page = await context.newPage();
  try {
    await page.route('**/*', (route) => {
      const t = route.request().resourceType();
      if (t === 'image' || t === 'media' || t === 'font') return route.abort();
      return route.continue();
    });
    const resp = await page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout: NAV_TIMEOUT
    });
    await page.waitForTimeout(SETTLE_MS);
    const html = await page.content();
    return {
      ok: Boolean(resp) && resp.status() < 400,
      status: resp ? resp.status() : 0,
      url: page.url(),
      html
    };
  } catch (err) {
    return { ok: false, status: 0, url, html: '', error: String(err.message || err).split('\n')[0] };
  } finally {
    await page.close().catch(() => {});
  }
}

/* ------------------------------------------- descubrir ficha de producto (x2) */

/** Vía 1: sitemap por HTTP (como el piloto estático). */
async function productoViaSitemap(sitemaps) {
  for (const sm of (sitemaps || []).slice(0, 3)) {
    const res = await fetchText(sm);
    if (!res.ok || !res.body) continue;
    const locs = [...res.body.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/gi)].map((m) => m[1]);
    if (!locs.length) continue;

    if (/<sitemapindex/i.test(res.body)) {
      const candidate = locs.find((u) => /product|producto|item/i.test(u)) || locs[0];
      const sub = await fetchText(candidate);
      if (!sub.ok) continue;
      const subLocs = [...sub.body.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/gi)].map((m) => m[1]);
      const prod = subLocs.find((u) => PRODUCT_URL_HINTS.some((re) => re.test(u)));
      if (prod) return prod;
      if (subLocs.length) return subLocs[Math.floor(subLocs.length / 2)];
      continue;
    }
    const prod = locs.find((u) => PRODUCT_URL_HINTS.some((re) => re.test(u)));
    if (prod) return prod;
  }
  return null;
}

/**
 * Vía 2 (nueva, para las tiendas cuyo sitemap está tras el WAF): enlaces de la
 * propia home renderizada que huelan a ficha de producto, mismo host.
 */
function productoViaHome(html, baseUrl) {
  const host = new URL(baseUrl).host.replace(/^www\./, '');
  const hrefs = [...html.matchAll(/href=["']([^"'#?]+)[^"']*["']/gi)].map((m) => m[1]);
  for (const h of hrefs) {
    let abs;
    try { abs = new URL(h, baseUrl).toString(); } catch { continue; }
    const sameHost = new URL(abs).host.replace(/^www\./, '') === host;
    if (sameHost && PRODUCT_URL_HINTS.some((re) => re.test(new URL(abs).pathname))) {
      return abs;
    }
  }
  return null;
}

/* ---------------------------------------------------------------- una tienda */

async function medirTienda(context, tienda) {
  const base = `https://${tienda.dominio}`;
  const r = {
    dominio: tienda.dominio,
    sector: tienda.sector,
    url: tienda.url,
    metodo: 'headless',
    medido_en: new Date().toISOString(),
    errores: []
  };

  // 1-2. robots.txt y llms.txt — HTTP simple, UA de investigación (a propósito).
  const robots = await fetchText(`${base}/robots.txt`);
  r.robots_status = robots.status;
  if (robots.ok && robots.body) {
    const { groups, sitemaps } = parseRobots(robots.body);
    r.robots_existe = true;
    r.sitemaps = sitemaps;
    r.ia_crawlers = {};
    for (const bot of AI_BOTS) r.ia_crawlers[bot] = policyFor(groups, bot);
    r.ia_bloqueados = Object.entries(r.ia_crawlers)
      .filter(([, v]) => v.policy === 'blocked').map(([k]) => k);
    r.ia_con_regla_propia = Object.entries(r.ia_crawlers)
      .filter(([, v]) => v.explicit).map(([k]) => k);
  } else {
    r.robots_existe = false;
    r.sitemaps = [];
    r.errores.push(`robots.txt: ${robots.error || robots.status}`);
  }

  const llms = await fetchText(`${base}/llms.txt`);
  r.llms_txt = Boolean(llms.ok && llms.body && !/<html/i.test(llms.body));
  r.llms_status = llms.status;

  // 3. Home — navegador real.
  const home = await renderPage(context, tienda.url || base);
  r.home_status = home.status;
  if (home.ok && home.html) {
    const ld = extractJsonLdTypes(home.html);
    r.home_jsonld_types = ld.types;
    r.home_jsonld_blocks = ld.blocks;
    r.home_jsonld_rotos = ld.broken;
    r.chatbot = detect(home.html, CHAT_VENDORS);
    r.buscador_personalizacion = detect(home.html, SEARCH_VENDORS);
  } else {
    r.errores.push(`home: ${home.error || home.status}`);
    r.home_jsonld_types = [];
    r.chatbot = [];
    r.buscador_personalizacion = [];
  }

  // 4. Ficha de producto — sitemap primero; si no, enlaces de la home.
  try {
    let prodUrl = await productoViaSitemap(r.sitemaps);
    r.producto_descubierto_via = prodUrl ? 'sitemap' : null;
    if (!prodUrl && home.ok && home.html) {
      prodUrl = productoViaHome(home.html, home.url);
      if (prodUrl) r.producto_descubierto_via = 'home-links';
    }
    r.producto_url = prodUrl;
    if (prodUrl) {
      const prod = await renderPage(context, prodUrl);
      r.producto_status = prod.status;
      if (prod.ok && prod.html) {
        const ld = extractJsonLdTypes(prod.html);
        r.producto_jsonld_types = ld.types;
        r.producto_tiene_Product = ld.types.includes('Product');
        r.producto_tiene_Offer = ld.types.some((t) => /Offer/i.test(t));
      } else {
        r.errores.push(`producto: ${prod.error || prod.status}`);
      }
    }
  } catch (e) {
    r.errores.push(`producto: ${String(e.message || e)}`);
  }

  return r;
}

/* ------------------------------------------------------------------ ejecución */

async function pool(items, size, fn) {
  const out = [];
  let i = 0;
  const workers = Array.from({ length: size }, async () => {
    while (i < items.length) {
      const idx = i++;
      out[idx] = await fn(items[idx], idx);
      await sleep(900); // cortesía
    }
  });
  await Promise.all(workers);
  return out;
}

function toCsv(rows) {
  const cols = [
    'dominio', 'sector', 'metodo', 'robots_existe', 'ia_bloqueados_n',
    'ia_bloqueados', 'ia_con_regla_propia_n', 'llms_txt', 'home_status',
    'home_jsonld_types_n', 'home_jsonld_rotos', 'chatbot',
    'buscador_personalizacion', 'producto_descubierto_via',
    'producto_tiene_Product', 'producto_tiene_Offer', 'errores'
  ];
  const esc = (v) => {
    const s = Array.isArray(v) ? v.join('|') : v === undefined || v === null ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [cols.join(',')];
  for (const r of rows) {
    lines.push([
      r.dominio, r.sector, r.metodo, r.robots_existe,
      (r.ia_bloqueados || []).length, r.ia_bloqueados,
      (r.ia_con_regla_propia || []).length, r.llms_txt, r.home_status,
      (r.home_jsonld_types || []).length, r.home_jsonld_rotos,
      r.chatbot, r.buscador_personalizacion, r.producto_descubierto_via,
      r.producto_tiene_Product, r.producto_tiene_Offer, r.errores
    ].map(esc).join(','));
  }
  return lines.join('\n');
}

async function main() {
  const cfg = JSON.parse(await readFile(join(__dirname, args.in || 'tiendas-piloto.json'), 'utf8'));
  const tiendas = cfg.tiendas;

  // Guardado incremental + reanudación (--resume): la pasada de 300 dura y no
  // puede perderse entera por una caída. El parcial se reescribe cada 5 tiendas.
  const dir = join(__dirname, OUT);
  await mkdir(dir, { recursive: true });
  const partialPath = join(dir, 'parcial-headless.json');
  let previos = [];
  if (args.resume) {
    try { previos = JSON.parse(await readFile(partialPath, 'utf8')); } catch { previos = []; }
  }
  const hechos = new Set(previos.map((r) => r.dominio));
  const pendientes = tiendas.filter((t) => !hechos.has(t.dominio));
  const parciales = [...previos];
  let escribiendo = Promise.resolve();
  const flush = () => { escribiendo = escribiendo.then(() => writeFile(partialPath, JSON.stringify(parciales, null, 1))).catch(() => {}); return escribiendo; };
  console.log(`Midiendo ${pendientes.length}/${tiendas.length} tiendas (${previos.length} ya hechas) · HEADLESS (Chromium) · concurrencia ${CONCURRENCY}\n`);

  const browser = await chromium.launch({ headless: true });

  // El UA por defecto de Playwright anuncia "HeadlessChrome" y muchos WAF lo
  // cortan en la puerta (primera pasada: 6/20 accesibles, PEOR que el fetch
  // estático). Lo normalizamos al token estándar de Chrome DE LA MISMA VERSIÓN:
  // práctica común de medición, no suplanta a nadie. Línea que NO cruzamos:
  // navigator.webdriver se queda en true (la señal honesta de automatización);
  // enmascararlo sería evasión activa de fingerprinting. Si un WAF bloquea por
  // eso, se registra como dato y se declara.
  const version = browser.version().split('.')[0]; // p. ej. "149"
  const context = await browser.newContext({
    userAgent:
      `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ` +
      `(KHTML, like Gecko) Chrome/${version}.0.0.0 Safari/537.36`,
    locale: 'es-ES',
    timezoneId: 'Europe/Madrid',
    viewport: { width: 1366, height: 900 }
  });

  let rows;
  try {
    await pool(pendientes, CONCURRENCY, async (t) => {
      let r;
      try { r = await medirTienda(context, t); }
      catch (e) { r = { dominio: t.dominio, sector: t.sector, errores: [`fatal: ${String(e?.message || e)}`], ia_bloqueados: [] }; }
      parciales.push(r);
      if (parciales.length % 5 === 0) await flush();
      const flag = r.errores.length ? '⚠' : '·';
      console.log(
        `${flag} ${r.dominio.padEnd(24)} home:${String(r.home_status).padStart(3)} ` +
        `IA-bloq:${(r.ia_bloqueados || []).length}/${AI_BOTS.length} ` +
        `schema-home:${(r.home_jsonld_types || []).length} ` +
        `prod:${r.producto_tiene_Product === true ? 'Product✓' : r.producto_url ? 'sin-schema' : '-'} ` +
        `chat:${(r.chatbot || []).join('/') || '-'}`
      );
      return r;
    });
  } finally {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }

  await flush();
  rows = parciales;
  await writeFile(join(dir, 'piloto-headless.json'), JSON.stringify(rows, null, 2));
  await writeFile(join(dir, 'piloto-headless.csv'), toCsv(rows));

  const okHome = rows.filter((r) => r.home_status === 200).length;
  const conProducto = rows.filter((r) => r.producto_jsonld_types).length;

  console.log('\n--- Validez (comparar contra el piloto estático) ---');
  console.log(`Home accesible (200):      ${okHome}/${rows.length}  (estático: 12/20)`);
  console.log(`Ficha de producto medida:  ${conProducto}/${rows.length}  (estático: 11/20)`);
  console.log(`\nResultados en ${dir}/`);
}

const invocadoDirectamente =
  process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];

if (invocadoDirectamente) {
  main().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
