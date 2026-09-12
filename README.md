# Kiwop Labs

Scripts y datasets abiertos de [Kiwop Labs](https://www.kiwop.com/labs): lo que medimos sobre IA, buscadores y ecommerce en España, con método y datos publicados para que cualquiera lo repita.

*Open scripts and datasets from Kiwop Labs: what we measure about AI, search and ecommerce in Spain, with method and data published so anyone can reproduce it. Reports are in Spanish, Catalan, English, German, French, Dutch and Portuguese on kiwop.com.*

| Serie | Qué mide | Página | Datos |
|---|---|---|---|
| **Baseline GEO** (mensual) | Qué agencias recomiendan ChatGPT, Claude, Gemini y Perplexity para 40 preguntas de compra en España, qué dominios citan y dónde aparece Kiwop. Respuestas íntegras publicadas. | [/labs/geo-baseline](https://www.kiwop.com/labs/geo-baseline) | [`data/geo-baseline/`](data/geo-baseline/) |
| **IA en el ecommerce español** (trimestral) | 300 tiendas online: crawlers de IA en robots.txt, llms.txt (y su origen), schema Product en ficha, chatbots y buscadores. | [/labs/ia-ecommerce-espana](https://www.kiwop.com/labs/ia-ecommerce-espana) | [`data/ecommerce-ia/`](data/ecommerce-ia/) |
| **Qué se pregunta a la IA en España** (mensual) | Consultas en asistentes de IA frente a Google para 60 términos sobre agencias, precios e IA para empresas; consultas en IA por cada 1.000 en Google. | [/labs/preguntas-ia-espana](https://www.kiwop.com/labs/preguntas-ia-espana) | [`data/preguntas-ia/`](data/preguntas-ia/) |
| **Comprobador «¿Está tu tienda preparada para la IA?»** (bajo demanda) | Un dominio → informe público 0-100 con el mismo medidor del estudio: crawlers de IA en robots.txt, acceso al bot, llms.txt y su origen, JSON-LD de la home, ficha con Product+Offer, sitemap, HTTPS. Doble pasada HTTP → Chromium. | [/labs/comprobador-ia-ecommerce](https://www.kiwop.com/labs/comprobador-ia-ecommerce) | JSON por informe (`?format=json`) |
| **Agentes de IA en producción** (trimestral) | Agregados de la telemetría de Nexo, la plataforma de Kiwop: ejecuciones y fallos de agentes, comentarios firmados por una persona, PR del worker autónomo, triage y guardián de correo, coste por API y por suscripción, crons. Sin nombres ni textos. | [/labs/agentes-ia-produccion](https://www.kiwop.com/labs/agentes-ia-produccion) | [`data/agentes-produccion/`](data/agentes-produccion/) |
| **WebMCP repro** | Repro mínimo del crash del renderer de Chrome con WebMCP + navegación same-document (crbug 534655509). | [/webmcp-repro](https://www.kiwop.com/webmcp-repro) | [`webmcp-repro/`](webmcp-repro/) |

## geo-baseline

`geo_baseline.py run --month AAAA-MM` lanza los prompts de `prompts.json` (tres sets: los 15 del ranking de marketingdirecto reproducidos tal cual, 15 propios de compra de IA y 10 con lo que se pregunta de verdad a los asistentes) contra:

- Claude por la API de Anthropic con `web_search` (`ANTHROPIC_API_KEY`),
- ChatGPT por la Responses API de OpenAI con `web_search` (`OPENAI_API_KEY`),
- Gemini y Perplexity por su API directa si hay clave (`GEMINI_API_KEY`, `PERPLEXITY_API_KEY`) o, si no, por la LLM Responses API de DataForSEO (`DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD`),
- el ChatGPT de consumo (chatgpt.com con búsqueda, localizado en España, sin sesión) por el LLM Scraper de DataForSEO.

Las empresas recomendadas se extraen de la respuesta íntegra con salida estructurada (Claude, `json_schema`), y Kiwop se busca además por texto. El JSON resultante lleva método, modelos exactos, fecha, cada respuesta completa, empresas, dominios citados y agregados por set y proveedor. Cada prompt se lanza una sola vez por proveedor, en sesión nueva, sin instrucciones de sistema, en castellano.

Dependencias: Python 3.10+, `anthropic`, `openai`, `google-genai` (solo con clave propia de Gemini). Las claves van en variables de entorno o en un `.env` junto al script; ninguna se guarda en el repo.

## estudio-ecommerce

Nivel 1 (automático, n=300):

1. `tiendas-300.json`: la muestra, por regla pública sector × tamaño sobre el marco de Semrush Trending Websites España (jun-2026).
2. `medir.mjs`: pasada HTTP identificada como `KiwopResearchBot` (robots.txt y sus reglas para 14 crawlers de IA, llms.txt, JSON-LD de la home, localización de una ficha de producto y su schema, firmas de chatbot y buscador).
3. `medir-headless.mjs`: la misma medición con Chromium headless y el user-agent de Chrome normalizado, `navigator.webdriver` honesto y sin evasión de fingerprinting. Reanudable (`--resume`), guarda parciales.
4. `aggregate.py <dir>`: une las dos pasadas por señal y agrega por tramo y sector.
5. `verificar-llms.py <dir>`: relee cada llms.txt detectado y clasifica su origen (propio, plantilla de plataforma, falso positivo, sin respuesta) y detecta la plataforma por la home. Sin este paso el dato de llms.txt sale inflado: en septiembre de 2026 la mitad eran la plantilla automática de Shopify.

`npm install && npm test` corre los tests unitarios de los parsers. No se compra nada, no se crean cuentas y no se resuelve ningún CAPTCHA: un bloqueo anti-bot es un resultado.

Nivel 2 (prueba agéntica, n=40): el protocolo está preregistrado en [`protocolo-agentico.md`](estudio-ecommerce/protocolo-agentico.md) antes de medir. Se publica en el cuarto trimestre de 2026.

## preguntas-ia y nota-mensual

`preguntas_ia.py run --month AAAA-MM` mide los 60 términos de `keywords.json` (volumen en asistentes de IA por DataForSEO AI Keyword Data y en Google Ads, España/es) y escribe el JSON de la serie. `nota_mensual.py` extrae los hechos del mes de los datasets, pide a Claude una nota en 7 idiomas solo con esos hechos y abre una tarea de revisión: el cron redacta, una persona publica. `monthly.sh` es la pasada del día 1 que encadena baseline, preguntas, nota, commit, deploy y avisos.

## comprobador

`node worker.mjs --once tienda.es` mide un dominio y lo imprime (sin cola). En kiwop.com el mismo código corre como worker de una cola de ficheros (`store.mjs`) alimentada por la API del sitio. `medir-dominio.mjs` reutiliza los helpers de `estudio-ecommerce/medir.mjs` (parser de robots, política por bot, tipos JSON-LD) para que tienda y estudio midan igual; `safeFetch` resuelve las redirecciones a mano y comprueba que cada salto vaya a una IP pública (el dominio lo teclea cualquiera). La puntuación (`puntuar`) está versionada: crawlers 25, acceso 10, llms.txt 15, schema de la home 10, ficha Product+Offer 25, sitemap 10, HTTPS 5. Si la tienda devuelve un desafío anti-bot a nuestro servidor, el informe lo dice y no puntúa.

Dependencias: Node 22+, `playwright` (Chromium) para la segunda pasada; sin navegador mide solo por HTTP.

## agentes-produccion

`agregar.sql` es una consulta de solo lectura sobre la base de datos de Nexo (Postgres) que devuelve una fila JSON con agregados de 90 días: nada identificable, solo contadores, distribuciones y medianas. `medir.sh` la ejecuta cada trimestre por SSH y publica el dataset. Las definiciones (qué es una ejecución, una firma, un PR entregado) están comentadas en el propio SQL.

## webmcp-repro

Rutas de Astro (`src/pages/webmcp-repro/`) que reproducen el crash del renderer de Chrome 150+ cuando conviven `ClientRouter`, tools imperativas y una tool declarativa de WebMCP. Se copian tal cual a un proyecto Astro con el origin trial activo (`PUBLIC_WEBMCP_OT_TOKEN`). La matriz de variantes y el resultado de cada una están en `index.astro`; la versión viva, en kiwop.com/webmcp-repro.

## Licencias

Código: MIT ([LICENSE](LICENSE)). Datos: CC BY 4.0 ([data/LICENSE](data/LICENSE)), con atribución a Kiwop Labs y enlace a la página de la serie.

## Cómo citar

> Kiwop Labs (2026). *Baseline GEO: qué agencias citan los LLM en España*. https://www.kiwop.com/labs/geo-baseline
>
> Kiwop Labs (2026). *IA en el ecommerce español 2026: lo que las tiendas le enseñan a la IA*. https://www.kiwop.com/labs/ia-ecommerce-espana
