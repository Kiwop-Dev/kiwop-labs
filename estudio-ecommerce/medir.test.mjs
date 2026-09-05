/**
 * Tests del parser de robots.txt. La cifra estrella del estudio ("el X% bloquea
 * a los crawlers de IA") sale de aquí: si esto falla, el estudio miente.
 *
 * Ejecutar: node --test medir.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseRobots, policyFor, extractJsonLdTypes } from './medir.mjs';

const policy = (txt, ua) => policyFor(parseRobots(txt).groups, ua);

test('bloqueo total explícito de GPTBot', () => {
  const r = policy('User-agent: GPTBot\nDisallow: /', 'GPTBot');
  assert.equal(r.policy, 'blocked');
  assert.equal(r.explicit, true);
});

test('allow explícito (caso MediaMarkt) NO es bloqueo', () => {
  const txt = 'User-Agent: *\n\nUser-agent: GPTBot\nAllow: /\n\nUser-agent: ClaudeBot\nAllow: /';
  assert.equal(policy(txt, 'GPTBot').policy, 'allowed');
  assert.equal(policy(txt, 'GPTBot').explicit, true);
  assert.equal(policy(txt, 'ClaudeBot').explicit, true);
});

test('grupos * repetidos se FUSIONAN (bug hallado con decathlon.es)', () => {
  const txt = [
    'User-Agent: *',
    'Disallow: /unsubscribe/',
    '',
    'User-Agent: *',
    'Disallow: */bricks/*',
    '',
    'User-Agent: *',
    'Disallow: /*?mc=*'
  ].join('\n');
  // Antes solo veía /unsubscribe/. Ahora debe ver las tres reglas → partial.
  const r = policy(txt, 'GPTBot');
  assert.equal(r.policy, 'partial');
  assert.equal(r.explicit, false); // hereda del comodín
});

test('bloqueo total repartido en un segundo grupo del mismo agente', () => {
  const txt = 'User-agent: CCBot\nCrawl-delay: 5\n\nUser-agent: CCBot\nDisallow: /';
  // Si no fusionáramos, el primer grupo (sin Disallow) diría "allowed": falso negativo.
  assert.equal(policy(txt, 'CCBot').policy, 'blocked');
});

test('Disallow vacío significa permitir todo', () => {
  assert.equal(policy('User-agent: *\nDisallow:', 'GPTBot').policy, 'allowed');
});

test('bloqueo por comodín afecta a un bot sin regla propia', () => {
  const r = policy('User-agent: *\nDisallow: /', 'PerplexityBot');
  assert.equal(r.policy, 'blocked');
  assert.equal(r.explicit, false);
});

test('regla propia gana al comodín (Allow gana a Disallow global)', () => {
  const txt = 'User-agent: *\nDisallow: /\n\nUser-agent: GPTBot\nAllow: /';
  assert.equal(policy(txt, 'GPTBot').policy, 'allowed');
  assert.equal(policy(txt, 'CCBot').policy, 'blocked');
});

test('mayúsculas y comentarios no rompen el parseo', () => {
  const txt = '# comentario\nUSER-AGENT: GPTBot   # inline\nDISALLOW: /   ';
  assert.equal(policy(txt, 'gptbot').policy, 'blocked');
});

test('sitemaps se extraen', () => {
  const { sitemaps } = parseRobots('Sitemap: https://x.es/s.xml\nUser-agent: *\nDisallow:');
  assert.deepEqual(sitemaps, ['https://x.es/s.xml']);
});

test('JSON-LD: extrae @type anidados y cuenta los rotos', () => {
  const html = `
    <script type="application/ld+json">{"@context":"x","@graph":[{"@type":"Organization"},{"@type":["Product","Thing"]}]}</script>
    <script type="application/ld+json">{ roto </script>
  `;
  const r = extractJsonLdTypes(html);
  assert.ok(r.types.includes('Organization'));
  assert.ok(r.types.includes('Product'));
  assert.equal(r.broken, 1);
  assert.equal(r.blocks, 2);
});
