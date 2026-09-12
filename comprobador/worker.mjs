#!/usr/bin/env node
/**
 * Kiwop Labs · comprobador: worker de la cola.
 *
 * Proceso aparte del sitio (PM2 «kiwop-labs-comprobador», ver ecosystem.config.cjs):
 * vigila labs-runs/comprobador/queue, mide cada dominio con medir-dominio.mjs (HTTP +
 * Chromium headless si hace falta) y deja el resultado en results/<dominio>.json, que
 * es lo que lee el informe SSR. Concurrencia baja a propósito: es una herramienta
 * pública y el navegador cuesta memoria.
 *
 * Variables: LABS_COMPROBADOR_DIR (almacén), LABS_COMPROBADOR_CONCURRENCY (2),
 * LABS_CHROMIUM_PATH (ejecutable de Chromium si el de Playwright no coincide con la
 * revisión instalada en /root/.cache/ms-playwright), LABS_COMPROBADOR_NO_BROWSER=1
 * para trabajar solo por HTTP.
 *
 * Uso directo: `node worker.mjs` (bucle) · `node worker.mjs --once tienda.es` (mide un
 * dominio y lo imprime, sin tocar la cola: para probar).
 */
import { claimNext, finish, writeResult, requeueStale, ensureDirs, normalizeDomain, ROOT } from './store.mjs';
import { medirDominio } from './medir-dominio.mjs';

process.on('uncaughtException', (e) => console.error('⚠ uncaughtException (seguimos):', e?.code || e?.message || e));
process.on('unhandledRejection', (e) => console.error('⚠ unhandledRejection (seguimos):', e?.code || e?.message || e));

const CONCURRENCY = Number(process.env.LABS_COMPROBADOR_CONCURRENCY || 2);
const POLL_MS = 1500;
const JOB_TIMEOUT_MS = 150_000;
const BROWSER_MAX_JOBS = 40; // reciclar Chromium cada N trabajos: memoria estable
const log = (...a) => console.log(new Date().toISOString(), ...a);

let browser = null;
let browserJobs = 0;

async function getBrowser() {
  if (process.env.LABS_COMPROBADOR_NO_BROWSER === '1') return null;
  if (browser && browserJobs < BROWSER_MAX_JOBS) return browser;
  await closeBrowser();
  try {
    const { chromium } = await import('playwright');
    browser = await chromium.launch({
      headless: true,
      executablePath: process.env.LABS_CHROMIUM_PATH || undefined,
      args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    });
    browserJobs = 0;
    log('chromium lanzado');
  } catch (e) {
    log('sin navegador (solo HTTP):', e.message?.split('\n')[0]);
    browser = null;
  }
  return browser;
}

async function closeBrowser() {
  if (browser) {
    await browser.close().catch(() => {});
    browser = null;
  }
}

function withTimeout(p, ms) {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('job_timeout')), ms);
    p.then((v) => { clearTimeout(t); resolve(v); }, (e) => { clearTimeout(t); reject(e); });
  });
}

async function runJob(job) {
  const domain = normalizeDomain(job.dominio);
  if (!domain) {
    await finish(job.dominio);
    return;
  }
  log(`▶ ${domain}`);
  const b = await getBrowser();
  let result;
  try {
    result = await withTimeout(medirDominio(domain, { browser: b, log: (m) => log(m) }), JOB_TIMEOUT_MS);
    browserJobs++;
  } catch (e) {
    log(`✖ ${domain}: ${e.message}`);
    result = {
      dominio: domain,
      metodo_version: job.metodo_version || undefined,
      medido_en: new Date().toISOString(),
      errores: [`medicion: ${e.message}`],
      fallo: true,
    };
    if (e.message === 'job_timeout') await closeBrowser();
  }
  result.idioma = job.lang || 'es';
  await writeResult(domain, result);
  await finish(domain);
  await purgeCloudflare(domain);
  log(`✔ ${domain} ${result.puntuacion ? `${result.puntuacion.total}/100 (${result.puntuacion.veredicto})` : 'fallo'} en ${result.duracion_ms ?? '?'} ms`);
}

/**
 * El informe se cachea (nginx + Cloudflare) unos segundos; al volver a medir, el
 * lector vería el viejo. Purgamos las 7 URL del dominio con el token del .env
 * (CF_API_TOKEN/CF_ZONE_ID, que PM2 inyecta). Si no hay token, no pasa nada.
 */
const REPORT_PATHS = ['/labs/comprobador-ia-ecommerce', '/en/labs/ai-readiness-check-ecommerce', '/ca/labs/comprovador-ia-ecommerce', '/de/labs/ki-check-onlineshop', '/fr/labs/verificateur-ia-ecommerce', '/nl/labs/ai-check-webshop', '/pt/labs/verificador-ia-ecommerce'];
async function purgeCloudflare(domain) {
  const token = process.env.CF_API_TOKEN;
  const zone = process.env.CF_ZONE_ID;
  if (!token || !zone) return;
  const files = REPORT_PATHS.flatMap((p) => [`https://www.kiwop.com${p}/${encodeURIComponent(domain)}`, `https://www.kiwop.com${p}`]);
  try {
    const res = await fetch(`https://api.cloudflare.com/client/v4/zones/${zone}/purge_cache`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ files }),
      signal: AbortSignal.timeout(10_000),
    });
    if (!res.ok) log(`purga CF ${res.status}`);
  } catch (e) {
    log('purga CF falló:', e.message);
  }
}

async function loop() {
  await ensureDirs();
  log(`worker en marcha · almacén ${ROOT} · concurrencia ${CONCURRENCY}`);
  let active = 0;
  let stopping = false;
  let lastStale = 0;
  const stop = async () => {
    stopping = true;
    log('parando…');
    const t0 = Date.now();
    while (active > 0 && Date.now() - t0 < 60_000) await new Promise((r) => setTimeout(r, 250));
    await closeBrowser();
    process.exit(0);
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
  for (;;) {
    if (stopping) break;
    if (Date.now() - lastStale > 60_000) {
      lastStale = Date.now();
      const n = await requeueStale(10).catch(() => 0);
      if (n) log(`${n} trabajo(s) colgado(s) devuelto(s) a la cola`);
    }
    if (active < CONCURRENCY) {
      const job = await claimNext().catch(() => null);
      if (job) {
        active++;
        runJob(job).catch((e) => log('error en job:', e.message)).finally(() => { active--; });
        continue;
      }
    }
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}

const args = process.argv.slice(2);
if (args[0] === '--once') {
  const domain = normalizeDomain(args[1] || '');
  if (!domain) {
    console.error('dominio no válido');
    process.exit(2);
  }
  const b = await getBrowser();
  const r = await medirDominio(domain, { browser: b, log: (m) => log(m) });
  console.log(JSON.stringify(r, null, 1));
  await closeBrowser();
  process.exit(0);
} else {
  loop();
}
