#!/usr/bin/env node
/**
 * Piloto de medición — "Estado de la IA en el ecommerce español 2026"
 *
 * Mide, para cada tienda, señales OBJETIVAS y observables desde fuera:
 *   1. robots.txt  → ¿deja entrar a los crawlers de IA?
 *   2. /llms.txt   → ¿existe?
 *   3. Home        → JSON-LD (@type), chatbot, buscador/personalización
 *   4. Producto    → JSON-LD Product/Offer (descubriendo una URL vía sitemap)
 *
 * Objetivo del PILOTO: validar que las señales se detectan bien y que el
 * estudio se sostiene, antes de invertir en la muestra grande.
 *
 * NO interpreta ni puntúa: solo registra hechos y la evidencia. La lectura
 * se hace después, a mano y con cuidado.
 *
 * Uso:
 *   node medir.mjs                       # usa tiendas-piloto.json
 *   node medir.mjs --ua=browser          # UA de navegador (ver nota ética)
 *   node medir.mjs --out=resultados
 *
 * Nota ética/metodológica sobre el User-Agent: por defecto nos identificamos
 * como bot de investigación (honesto y verificable). Algunas tiendas con
 * protección anti-bot nos devolverán 403: eso NO es un fallo del script, es un
 * dato y una limitación que hay que declarar en el informe. La opción
 * --ua=browser existe para comprobar cuánto cambia la medición, pero si se usa
 * en el estudio final hay que decirlo en la metodología.
 */

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Robustez para la muestra grande (sep-2026): un 'error' no manejado del socket
// HTTP/2 de undici (UND_ERR_SOCKET "other side closed") tumbaba el proceso entero
// a mitad de la pasada. Lo registramos y seguimos; la tienda afectada queda con
// su error en `errores`, que es el dato honesto.
process.on('uncaughtException', (e) => { console.error('⚠ uncaughtException (seguimos):', e?.code || e?.message || e); });
process.on('unhandledRejection', (e) => { console.error('⚠ unhandledRejection (seguimos):', e?.code || e?.message || e); });

const UA_RESEARCH =
  'KiwopResearchBot/1.0 (+https://www.kiwop.com; estudio IA ecommerce; info@kiwop.com)';
const UA_BROWSER =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36';

const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const [k, v = true] = a.replace(/^--/, '').split('=');
    return [k, v];
  })
);

const UA = args.ua === 'browser' ? UA_BROWSER : UA_RESEARCH;
const OUT = args.out || 'resultados';
const CONCURRENCY = Number(args.concurrency || 3);
const TIMEOUT_MS = Number(args.timeout || 20000);

/** Crawlers de IA que nos interesan. */
export const AI_BOTS = [
  'GPTBot',            // OpenAI, entrenamiento
  'OAI-SearchBot',     // OpenAI, búsqueda
  'ChatGPT-User',      // OpenAI, navegación en vivo
  'ClaudeBot',         // Anthropic
  'anthropic-ai',      // Anthropic (legacy)
  'PerplexityBot',     // Perplexity
  'Google-Extended',   // Gemini (entrenamiento)
  'Applebot-Extended', // Apple Intelligence
  'CCBot',             // Common Crawl (alimenta a muchos LLM)
  'Bytespider',        // ByteDance
  'meta-externalagent' // Meta AI
];

/** Firmas de chatbot / asistente conversacional. */
export const CHAT_VENDORS = [
  ['Intercom', /widget\.intercom\.io|intercomSettings/i],
  ['Zendesk', /static\.zdassets\.com|zopim/i],
  ['Tidio', /code\.tidio\.co/i],
  ['Crisp', /client\.crisp\.chat/i],
  ['Drift', /js\.driftt\.com/i],
  ['HubSpot Chat', /js\.hs-scripts\.com|js\.usemessages\.com/i],
  ['Tawk.to', /embed\.tawk\.to/i],
  ['Freshchat', /wchat\.freshchat\.com/i],
  ['Gorgias', /config\.gorgias\.chat|gorgias\.chat/i],
  ['LivePerson', /lpcdn\.lpsnmedia\.net|liveperson/i],
  ['Salesforce Chat', /embeddedservice|salesforceliveagent/i],
  ['Oct8ne', /oct8ne/i],
  ['Ada', /static\.ada\.support/i],
  ['Zowie', /zowie\.ai/i],
  ['Genesys', /genesys(cloud)?/i]
];

/**
 * Buscadores / personalización. OJO: no todos son "IA"; muchos son búsqueda
 * avanzada. Se registra el proveedor y la interpretación se hace después,
 * sin inflar el titular.
 */
export const SEARCH_VENDORS = [
  ['Algolia', /algolia(net|\.com)/i],
  ['Doofinder', /doofinder/i],
  ['Klevu', /klevu/i],
  ['Searchspring', /searchspring/i],
  ['Constructor.io', /constructor\.io/i],
  ["Luigi's Box", /luigisbox/i],
  ['Nosto', /nosto\.com/i],
  ['Dynamic Yield', /dynamicyield/i],
  ['Clerk.io', /clerk\.io/i],
  ['Findify', /findify/i],
  ['Bloomreach', /bloomreach/i],
  ['Empathy.co', /empathy(broker)?/i]
];

/** Patrones de URL que suelen ser de ficha de producto. */
export const PRODUCT_URL_HINTS = [
  /\/p\//i, /\/producto/i, /\/product/i, /\/products\//i,
  /-p-\d+/i, /\/dp\//i, /\/item\//i
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function fetchText(url, { timeout = TIMEOUT_MS } = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeout);
  try {
    const res = await fetch(url, {
      signal: ctrl.signal,
      redirect: 'follow',
      headers: { 'user-agent': UA, accept: '*/*' }
    });
    const body = res.ok ? await res.text() : '';
    return { ok: res.ok, status: res.status, url: res.url, body };
  } catch (err) {
    return { ok: false, status: 0, url, body: '', error: String(err.name || err) };
  } finally {
    clearTimeout(t);
  }
}

/* ---------------------------------------------------------------- robots.txt */

/**
 * Parser de robots.txt con grupos reales (no un `includes`): agrupa
 * User-agent consecutivos y sus reglas, como manda el estándar.
 */
export function parseRobots(text) {
  const groups = [];
  const sitemaps = [];
  let current = null;
  let lastWasAgent = false;

  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/#.*$/, '').trim();
    if (!line) continue;
    const i = line.indexOf(':');
    if (i === -1) continue;
    const field = line.slice(0, i).trim().toLowerCase();
    const value = line.slice(i + 1).trim();

    if (field === 'sitemap') {
      sitemaps.push(value);
      continue;
    }
    if (field === 'user-agent') {
      if (!current || !lastWasAgent) {
        current = { agents: [], rules: [] };
        groups.push(current);
      }
      current.agents.push(value.toLowerCase());
      lastWasAgent = true;
      continue;
    }
    if (field === 'disallow' || field === 'allow') {
      if (!current) {
        current = { agents: ['*'], rules: [] };
        groups.push(current);
      }
      current.rules.push({ type: field, path: value });
      lastWasAgent = false;
    }
  }
  return { groups, sitemaps };
}

/**
 * Política para un bot concreto.
 * Devuelve: blocked | partial | allowed  +  si tenía grupo propio.
 *
 * Ojo (bug encontrado en el piloto con decathlon.es): un robots.txt puede
 * repetir varias veces el mismo User-agent en grupos distintos. El estándar
 * manda FUSIONAR todas las reglas de los grupos que casan con ese agente.
 * Quedarse con el primer grupo se come el resto de reglas.
 */
export function policyFor(groups, ua) {
  const lower = ua.toLowerCase();

  const explicitGroups = groups.filter((g) => g.agents.includes(lower));
  const wildcardGroups = groups.filter((g) => g.agents.includes('*'));
  const matched = explicitGroups.length ? explicitGroups : wildcardGroups;

  if (!matched.length) return { policy: 'allowed', explicit: false };

  // Fusionar las reglas de TODOS los grupos que casan.
  const rules = matched.flatMap((g) => g.rules);

  const disallows = rules.filter((r) => r.type === 'disallow' && r.path !== '');
  const allowsRoot = rules.some((r) => r.type === 'allow' && r.path === '/');
  const fullBlock = disallows.some((r) => r.path === '/');
  const explicit = explicitGroups.length > 0;

  if (fullBlock && !allowsRoot) return { policy: 'blocked', explicit };
  if (disallows.length > 0) return { policy: 'partial', explicit };
  return { policy: 'allowed', explicit };
}

/* ------------------------------------------------------------------- JSON-LD */

function collectTypes(node, out) {
  if (Array.isArray(node)) return node.forEach((n) => collectTypes(n, out));
  if (!node || typeof node !== 'object') return;
  if (node['@type']) {
    const t = node['@type'];
    (Array.isArray(t) ? t : [t]).forEach((x) => out.add(String(x)));
  }
  for (const [k, v] of Object.entries(node)) {
    if (k !== '@type' && v && typeof v === 'object') collectTypes(v, out);
  }
}

export function extractJsonLdTypes(html) {
  const out = new Set();
  const re =
    /<script[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi;
  let m;
  let blocks = 0;
  let broken = 0;
  while ((m = re.exec(html))) {
    blocks++;
    try {
      collectTypes(JSON.parse(m[1].trim()), out);
    } catch {
      broken++; // JSON-LD roto: dato interesante en sí mismo
    }
  }
  return { types: [...out].sort(), blocks, broken };
}

function detect(html, table) {
  return table.filter(([, re]) => re.test(html)).map(([name]) => name);
}

/* ------------------------------------------------- descubrir ficha de producto */

async function findProductUrl(sitemaps) {
  for (const sm of sitemaps.slice(0, 3)) {
    const res = await fetchText(sm);
    if (!res.ok || !res.body) continue;

    const locs = [...res.body.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/gi)].map(
      (m) => m[1]
    );
    if (!locs.length) continue;

    // Índice de sitemaps → bajar un nivel buscando el que huela a producto
    const isIndex = /<sitemapindex/i.test(res.body);
    if (isIndex) {
      const candidate =
        locs.find((u) => /product|producto|item/i.test(u)) || locs[0];
      const sub = await fetchText(candidate);
      if (!sub.ok) continue;
      const subLocs = [...sub.body.matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/gi)].map(
        (m) => m[1]
      );
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

/* ---------------------------------------------------------------- una tienda */

async function medirTienda(tienda) {
  const base = `https://${tienda.dominio}`;
  const r = {
    dominio: tienda.dominio,
    sector: tienda.sector,
    url: tienda.url,
    medido_en: new Date().toISOString(),
    errores: []
  };

  // 1. robots.txt
  const robots = await fetchText(`${base}/robots.txt`);
  r.robots_status = robots.status;
  if (robots.ok && robots.body) {
    const { groups, sitemaps } = parseRobots(robots.body);
    r.robots_existe = true;
    r.sitemaps = sitemaps;
    r.ia_crawlers = {};
    for (const bot of AI_BOTS) {
      const { policy, explicit } = policyFor(groups, bot);
      r.ia_crawlers[bot] = { policy, explicit };
    }
    r.ia_bloqueados = Object.entries(r.ia_crawlers)
      .filter(([, v]) => v.policy === 'blocked')
      .map(([k]) => k);
    r.ia_con_regla_propia = Object.entries(r.ia_crawlers)
      .filter(([, v]) => v.explicit)
      .map(([k]) => k);
  } else {
    r.robots_existe = false;
    r.sitemaps = [];
    r.errores.push(`robots.txt: ${robots.error || robots.status}`);
  }

  // 2. llms.txt
  const llms = await fetchText(`${base}/llms.txt`);
  r.llms_txt = Boolean(llms.ok && llms.body && !/<html/i.test(llms.body));
  r.llms_status = llms.status;

  // 3. Home
  const home = await fetchText(tienda.url || base);
  r.home_status = home.status;
  if (home.ok && home.body) {
    const ld = extractJsonLdTypes(home.body);
    r.home_jsonld_types = ld.types;
    r.home_jsonld_blocks = ld.blocks;
    r.home_jsonld_rotos = ld.broken;
    r.chatbot = detect(home.body, CHAT_VENDORS);
    r.buscador_personalizacion = detect(home.body, SEARCH_VENDORS);
  } else {
    r.errores.push(`home: ${home.error || home.status}`);
    r.home_jsonld_types = [];
    r.chatbot = [];
    r.buscador_personalizacion = [];
  }

  // 4. Ficha de producto (JSON-LD Product/Offer)
  try {
    const prodUrl = await findProductUrl(r.sitemaps || []);
    r.producto_url = prodUrl;
    if (prodUrl) {
      const prod = await fetchText(prodUrl);
      r.producto_status = prod.status;
      if (prod.ok && prod.body) {
        const ld = extractJsonLdTypes(prod.body);
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
      await sleep(700); // cortesía: no martilleamos a nadie
    }
  });
  await Promise.all(workers);
  return out;
}

function toCsv(rows) {
  const cols = [
    'dominio', 'sector', 'robots_existe', 'ia_bloqueados_n', 'ia_bloqueados',
    'ia_con_regla_propia_n', 'llms_txt', 'home_jsonld_types_n', 'home_jsonld_rotos',
    'chatbot', 'buscador_personalizacion', 'producto_tiene_Product',
    'producto_tiene_Offer', 'home_status', 'errores'
  ];
  const esc = (v) => {
    const s = Array.isArray(v) ? v.join('|') : v === undefined ? '' : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const lines = [cols.join(',')];
  for (const r of rows) {
    lines.push(
      [
        r.dominio, r.sector, r.robots_existe,
        (r.ia_bloqueados || []).length, r.ia_bloqueados,
        (r.ia_con_regla_propia || []).length, r.llms_txt,
        (r.home_jsonld_types || []).length, r.home_jsonld_rotos,
        r.chatbot, r.buscador_personalizacion,
        r.producto_tiene_Product, r.producto_tiene_Offer,
        r.home_status, r.errores
      ].map(esc).join(',')
    );
  }
  return lines.join('\n');
}

async function main() {
  const cfg = JSON.parse(
    await readFile(join(__dirname, args.in || 'tiendas-piloto.json'), 'utf8')
  );
  const tiendas = cfg.tiendas;

  // Guardado incremental + reanudación (--resume): la pasada de 300 dura y no
  // puede perderse entera por una caída. El parcial se reescribe cada 5 tiendas.
  const dir = join(__dirname, OUT);
  await mkdir(dir, { recursive: true });
  const partialPath = join(dir, 'parcial.json');
  let previos = [];
  if (args.resume) {
    try { previos = JSON.parse(await readFile(partialPath, 'utf8')); } catch { previos = []; }
  }
  const hechos = new Set(previos.map((r) => r.dominio));
  const pendientes = tiendas.filter((t) => !hechos.has(t.dominio));
  const parciales = [...previos];
  let escribiendo = Promise.resolve();
  const flush = () => { escribiendo = escribiendo.then(() => writeFile(partialPath, JSON.stringify(parciales, null, 1))).catch(() => {}); return escribiendo; };
  console.log(`Midiendo ${pendientes.length}/${tiendas.length} tiendas (${previos.length} ya hechas) · UA=${args.ua === 'browser' ? 'browser' : 'research'}\n`);

  await pool(pendientes, CONCURRENCY, async (t) => {
    let r;
    try { r = await medirTienda(t); }
    catch (e) { r = { dominio: t.dominio, sector: t.sector, errores: [`fatal: ${String(e?.message || e)}`], ia_bloqueados: [] }; }
    parciales.push(r);
    if (parciales.length % 5 === 0) await flush();
    const bloq = (r.ia_bloqueados || []).length;
    const flag = r.errores.length ? '⚠' : '·';
    console.log(
      `${flag} ${r.dominio.padEnd(24)} robots:${r.robots_existe ? 'sí' : 'NO'} ` +
      `IA-bloq:${String(bloq).padStart(2)}/${AI_BOTS.length} ` +
      `llms:${r.llms_txt ? 'sí' : 'no'} ` +
      `chat:${(r.chatbot || []).join('/') || '-'} ` +
      `home:${r.home_status}`
    );
    return r;
  });

  await flush();
  const rows = parciales;
  await writeFile(join(dir, 'piloto.json'), JSON.stringify(rows, null, 2));
  await writeFile(join(dir, 'piloto.csv'), toCsv(rows));

  // Resumen honesto: qué se ha podido medir y qué no.
  const okHome = rows.filter((r) => r.home_status === 200).length;
  const okRobots = rows.filter((r) => r.robots_existe).length;
  const conProducto = rows.filter((r) => r.producto_jsonld_types).length;
  const bloqueanAlguno = rows.filter((r) => (r.ia_bloqueados || []).length > 0).length;

  console.log('\n--- Validez del piloto (esto es lo que hay que juzgar) ---');
  console.log(`robots.txt leído:        ${okRobots}/${rows.length}`);
  console.log(`Home accesible (200):    ${okHome}/${rows.length}`);
  console.log(`Ficha de producto hallada: ${conProducto}/${rows.length}`);
  console.log(`Tiendas que bloquean ≥1 crawler de IA: ${bloqueanAlguno}/${rows.length}`);
  console.log(`\nResultados en ${dir}/`);
  console.log('Si la home falla en muchas, la limitación es el anti-bot: hay que');
  console.log('decidir metodología (navegador real) y DECLARARLO en el informe.');
}

// Solo medir cuando se invoca directamente. Sin esto, importar el módulo (p. ej.
// desde los tests) dispara un crawl entero de las 20 tiendas.
const invocadoDirectamente =
  process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];

if (invocadoDirectamente) {
  main().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
