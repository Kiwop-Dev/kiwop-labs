/**
 * Kiwop Labs · comprobador «¿Está tu tienda preparada para la IA?»
 *
 * Almacén compartido entre la API del sitio (src/pages/api/labs/comprobador.ts) y el
 * worker (worker.mjs): cola de dominios por medir, resultados por dominio, índice de
 * recientes y límite de peticiones. Todo son ficheros planos bajo LABS_COMPROBADOR_DIR
 * (por defecto <cwd>/labs-runs/comprobador, es decir /home/kiwop-astro/labs-runs/...
 * en producción, FUERA de git y fuera de public/). Sin base de datos a propósito: el
 * volumen es de decenas de informes al día y así el worker y los 3 workers de PM2 del
 * sitio comparten estado sin nada más que el disco.
 *
 * Reglas:
 *  - Un dominio es la clave. Se normaliza (normalizeDomain) antes de tocar nada.
 *  - Un resultado se considera fresco RESULT_TTL_DAYS días; dentro de ese plazo no se
 *    vuelve a medir (el botón «volver a medir» del informe salta el TTL con force).
 *  - El límite es por IP (hash) y global, por hora, para que nadie use el comprobador
 *    como escáner masivo ni nos dispare la factura de Chromium.
 */
import { mkdir, readFile, writeFile, readdir, stat, rename, unlink } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';

export const ROOT = process.env.LABS_COMPROBADOR_DIR || join(process.cwd(), 'labs-runs', 'comprobador');
export const DIRS = {
  queue: join(ROOT, 'queue'),
  running: join(ROOT, 'running'),
  results: join(ROOT, 'results'),
  meta: join(ROOT, 'meta'),
};
export const METHOD_VERSION = '2026-09a';
export const RESULT_TTL_DAYS = Number(process.env.LABS_COMPROBADOR_TTL_DAYS || 7);
export const LIMITS = {
  perIpPerHour: Number(process.env.LABS_COMPROBADOR_IP_HOUR || 6),
  globalPerHour: Number(process.env.LABS_COMPROBADOR_GLOBAL_HOUR || 60),
  maxQueue: Number(process.env.LABS_COMPROBADOR_MAX_QUEUE || 40),
};

let ensured = false;
export async function ensureDirs() {
  if (ensured) return;
  for (const d of Object.values(DIRS)) await mkdir(d, { recursive: true });
  ensured = true;
}

/* ------------------------------------------------------------- dominio */

const HOST_RE = /^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$/;
const FORBIDDEN_SUFFIX = /\.(local|localhost|internal|lan|home|corp|intranet|test|example|invalid|onion|arpa)$/;

/**
 * Normaliza lo que teclea una persona («HTTPS://www.Tienda.es/producto?x») a un
 * hostname en minúsculas y ASCII (los IDN salen ya en punycode por URL). Devuelve
 * null si no es un nombre de dominio público plausible.
 */
export function normalizeDomain(input) {
  if (typeof input !== 'string') return null;
  let s = input.trim().toLowerCase();
  if (!s || s.length > 300) return null;
  if (!/^[a-z][a-z0-9+.-]*:\/\//.test(s)) s = `http://${s}`;
  let host;
  try {
    const u = new URL(s);
    if (u.username || u.password) return null;
    host = u.hostname;
  } catch {
    return null;
  }
  host = host.replace(/\.$/, '').replace(/^www\./, ''); // www. y no-www son la misma tienda
  if (!host || isIP(host) || host.startsWith('[')) return null;
  if (!HOST_RE.test(host)) return null;
  if (host === 'localhost' || FORBIDDEN_SUFFIX.test(host)) return null;
  return host;
}

function ipv4ToInt(ip) {
  return ip.split('.').reduce((acc, o) => (acc << 8) + Number(o), 0) >>> 0;
}

/** true si la IP es pública (no loopback, privada, link-local, CGNAT, multicast, reservada). */
export function isPublicIp(ip) {
  const v = isIP(ip);
  if (v === 4) {
    const n = ipv4ToInt(ip);
    const inRange = (cidr, bits) => (n >>> (32 - bits)) === (ipv4ToInt(cidr) >>> (32 - bits));
    return !(
      inRange('0.0.0.0', 8) || inRange('10.0.0.0', 8) || inRange('100.64.0.0', 10) ||
      inRange('127.0.0.0', 8) || inRange('169.254.0.0', 16) || inRange('172.16.0.0', 12) ||
      inRange('192.0.0.0', 24) || inRange('192.0.2.0', 24) || inRange('192.168.0.0', 16) ||
      inRange('198.18.0.0', 15) || inRange('198.51.100.0', 24) || inRange('203.0.113.0', 24) ||
      inRange('224.0.0.0', 3)
    );
  }
  if (v === 6) {
    const low = ip.toLowerCase();
    if (low === '::' || low === '::1') return false;
    if (low.startsWith('::ffff:')) return isPublicIp(low.slice(7));
    if (/^(fc|fd)/.test(low)) return false; // ULA
    if (/^fe[89ab]/.test(low)) return false; // link-local
    if (/^ff/.test(low)) return false; // multicast
    if (low.startsWith('2001:db8')) return false;
    return true;
  }
  return false;
}

/**
 * Resuelve el host y exige que TODAS sus direcciones sean públicas. Es la guardia
 * anti-SSRF: el worker va a hacer peticiones a lo que le pidan, así que nunca a
 * nuestra propia red. Devuelve la lista de IPs o lanza.
 */
export async function assertPublicHost(host) {
  let addrs;
  try {
    addrs = await lookup(host, { all: true, verbatim: true });
  } catch (e) {
    const err = new Error('dns');
    err.code = 'DNS';
    throw err;
  }
  if (!addrs.length) {
    const err = new Error('dns');
    err.code = 'DNS';
    throw err;
  }
  for (const a of addrs) {
    if (!isPublicIp(a.address)) {
      const err = new Error('private');
      err.code = 'PRIVATE';
      throw err;
    }
  }
  return addrs.map((a) => a.address);
}

/* ------------------------------------------------------------ resultados */

const safeName = (domain) => domain.replace(/[^a-z0-9.-]/g, '_');
export const resultPath = (domain) => join(DIRS.results, `${safeName(domain)}.json`);
const queuePath = (domain) => join(DIRS.queue, `${safeName(domain)}.json`);
const runningPath = (domain) => join(DIRS.running, `${safeName(domain)}.json`);
const INDEX = () => join(DIRS.meta, 'recent.json');

export async function readResult(domain) {
  try {
    return JSON.parse(await readFile(resultPath(domain), 'utf8'));
  } catch {
    return null;
  }
}

export function isFresh(result, ttlDays = RESULT_TTL_DAYS) {
  if (!result?.medido_en) return false;
  const age = Date.now() - new Date(result.medido_en).getTime();
  return age >= 0 && age < ttlDays * 86400_000 && result.metodo_version === METHOD_VERSION;
}

export async function writeResult(domain, result) {
  await ensureDirs();
  const tmp = `${resultPath(domain)}.tmp`;
  await writeFile(tmp, JSON.stringify(result, null, 1));
  await rename(tmp, resultPath(domain));
  await updateRecent({
    dominio: domain,
    puntuacion: result.puntuacion?.total ?? null,
    veredicto: result.puntuacion?.veredicto ?? null,
    medido_en: result.medido_en,
    plataforma: result.plataforma ?? null,
    no_medible: Boolean(result.no_medible),
  });
}

async function updateRecent(entry) {
  let list = [];
  try {
    list = JSON.parse(await readFile(INDEX(), 'utf8'));
  } catch {
    list = [];
  }
  list = list.filter((e) => e.dominio !== entry.dominio);
  list.unshift(entry);
  list = list.slice(0, 500);
  const tmp = `${INDEX()}.tmp`;
  await writeFile(tmp, JSON.stringify(list));
  await rename(tmp, INDEX());
}

/** Últimos informes (para la portada del comprobador y el sitemap). Solo los públicos. */
export async function listRecent(n = 20) {
  await ensureDirs();
  try {
    const list = JSON.parse(await readFile(INDEX(), 'utf8'));
    const hidden = await hiddenSet();
    return list.filter((e) => !hidden.has(e.dominio) && !e.no_medible && e.puntuacion != null).slice(0, n);
  } catch {
    return [];
  }
}

/** Dominios cuyo titular pidió no aparecer: meta/hidden.txt, uno por línea. */
export async function hiddenSet() {
  try {
    const txt = await readFile(join(DIRS.meta, 'hidden.txt'), 'utf8');
    return new Set(txt.split(/\r?\n/).map((l) => l.trim().toLowerCase()).filter(Boolean));
  } catch {
    return new Set();
  }
}

/* ------------------------------------------------------------------ cola */

async function exists(p) {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

export async function queueState(domain) {
  await ensureDirs();
  if (await exists(runningPath(domain))) return 'running';
  if (await exists(queuePath(domain))) return 'queued';
  return null;
}

export async function listQueue() {
  await ensureDirs();
  const names = await readdir(DIRS.queue);
  const items = [];
  for (const n of names) {
    if (!n.endsWith('.json')) continue;
    try {
      const s = await stat(join(DIRS.queue, n));
      items.push({ file: n, mtime: s.mtimeMs });
    } catch {
      /* borrado entre medias */
    }
  }
  return items.sort((a, b) => a.mtime - b.mtime);
}

/**
 * Encola un dominio. Devuelve { status: 'queued'|'running'|'done', position }.
 * Idempotente: si ya está en cola o midiéndose, no duplica.
 */
export async function enqueue(domain, meta = {}, { force = false } = {}) {
  await ensureDirs();
  const state = await queueState(domain);
  if (state) {
    const q = await listQueue();
    const pos = q.findIndex((i) => i.file === `${safeName(domain)}.json`);
    return { status: state, position: pos >= 0 ? pos + 1 : 0 };
  }
  if (!force) {
    const existing = await readResult(domain);
    if (existing && isFresh(existing)) return { status: 'done', position: 0 };
  }
  const q = await listQueue();
  if (q.length >= LIMITS.maxQueue) {
    const err = new Error('queue_full');
    err.code = 'QUEUE_FULL';
    throw err;
  }
  await writeFile(queuePath(domain), JSON.stringify({ dominio: domain, pedido_en: new Date().toISOString(), ...meta }));
  return { status: 'queued', position: q.length + 1 };
}

/** El worker coge el siguiente de la cola (mueve queue → running). null si no hay. */
export async function claimNext() {
  await ensureDirs();
  const q = await listQueue();
  for (const item of q) {
    const from = join(DIRS.queue, item.file);
    const to = join(DIRS.running, item.file);
    try {
      const job = JSON.parse(await readFile(from, 'utf8'));
      await rename(from, to);
      return job;
    } catch {
      /* otro proceso se lo llevó */
    }
  }
  return null;
}

export async function finish(domain) {
  await unlink(runningPath(domain)).catch(() => {});
}

/** Trabajos en running desde hace más de maxMinutes: los devolvemos a la cola una vez. */
export async function requeueStale(maxMinutes = 10) {
  await ensureDirs();
  const names = await readdir(DIRS.running);
  let n = 0;
  for (const name of names) {
    const p = join(DIRS.running, name);
    try {
      const s = await stat(p);
      if (Date.now() - s.mtimeMs > maxMinutes * 60_000) {
        const job = JSON.parse(await readFile(p, 'utf8'));
        if (job.reintentado) {
          await unlink(p);
          continue;
        }
        job.reintentado = true;
        await writeFile(join(DIRS.queue, name), JSON.stringify(job));
        await unlink(p);
        n++;
      }
    } catch {
      /* ignorar */
    }
  }
  return n;
}

/* ---------------------------------------------------------- rate limit */

export function hashIp(ip) {
  const salt = process.env.LABS_COMPROBADOR_SALT || 'kiwop-labs';
  return createHash('sha256').update(`${salt}|${ip || 'unknown'}`).digest('hex').slice(0, 16);
}

/**
 * Cuenta la petición para esa IP y globalmente. Devuelve { ok, reason }.
 * meta/ratelimit.json: { ips: { hash: [ts...] }, global: [ts...] }
 */
export async function rateLimit(ipHash) {
  await ensureDirs();
  const p = join(DIRS.meta, 'ratelimit.json');
  let data = { ips: {}, global: [] };
  try {
    data = JSON.parse(await readFile(p, 'utf8'));
  } catch {
    /* primera vez */
  }
  const now = Date.now();
  const hour = now - 3600_000;
  data.global = (data.global || []).filter((t) => t > hour);
  for (const k of Object.keys(data.ips || {})) {
    data.ips[k] = data.ips[k].filter((t) => t > hour);
    if (!data.ips[k].length) delete data.ips[k];
  }
  const mine = data.ips[ipHash] || [];
  if (mine.length >= LIMITS.perIpPerHour) return { ok: false, reason: 'ip' };
  if (data.global.length >= LIMITS.globalPerHour) return { ok: false, reason: 'global' };
  mine.push(now);
  data.ips[ipHash] = mine;
  data.global.push(now);
  await writeFile(p, JSON.stringify(data)).catch(() => {});
  return { ok: true };
}
