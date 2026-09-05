# Protocolo de la prueba agéntica (Nivel 2, n=40)

> Escrito ANTES de medir (14-jul-2026), a propósito: los criterios de éxito y
> fallo se fijan ahora para no poder ajustarlos después a lo que salga. Si algún
> criterio resulta mal definido en las 3 tiendas de calibración, se corrige UNA
> vez, se documenta el cambio, y no se toca más.

## Pregunta que responde

**¿Puede un agente de IA con navegador llegar hasta el carrito con un producto
concreto en cada tienda?** Es el dato que nadie ha publicado para España y la
antesala del comercio agéntico (un agente que no llega al carrito no podrá
comprar por ti, da igual lo que diga el marketing).

## Qué NO es esta prueba

- **No se compra nada.** La prueba termina en el carrito, ANTES de cualquier
  paso de checkout, pago o creación de cuenta. Cero transacciones, cero datos
  personales, cero direcciones o tarjetas.
- **No se evalúa la calidad de la tienda**, solo su operabilidad por un agente.
- **No se hacen reintentos infinitos**: presupuesto cerrado por tienda.

## Montaje

- **Agente**: Claude con navegador (el harness de agentic browsing de Kiwop, el
  mismo del claim de kiwop.com). Modelo y versión se congelan al inicio y se
  publican; si cambia el modelo a mitad, se repite entero el nivel.
- **Navegador**: Chromium headless con UA de Chrome normalizado (misma política
  que el nivel 1: `navigator.webdriver` honesto, sin evasión de fingerprinting).
- **Ventana**: las 40 tiendas en ≤10 días naturales, fechas publicadas.
- **Muestra**: 40 tiendas, submuestra estratificada del nivel 1 (sector × tamaño),
  seleccionadas por regla mecánica publicada (p. ej. las 2 primeras de cada celda
  por orden alfabético de dominio), NUNCA a dedo.

## Tarea (idéntica en las 40)

> "Busca en esta tienda [PRODUCTO GENÉRICO DE SU SECTOR] por menos de [PRESUPUESTO],
> elige uno disponible y añádelo al carrito. Para cuando el producto esté en el
> carrito. No inicies sesión, no crees cuenta, no pases del carrito."

- El producto genérico por sector se define en la tabla del anexo (p. ej. moda:
  "una camiseta blanca de algodón"; electrónica: "un cable USB-C"; belleza: "una
  crema hidratante facial"). Genérico a propósito: existe en cualquier tienda
  del sector.
- **Presupuesto de interacción**: máximo 25 pasos de navegación o 8 minutos por
  tienda, lo que llegue antes. Un intento por tienda (más el de calibración si
  aplica).
- Los banners de cookies se gestionan con la opción más privada disponible
  (rechazar no esenciales), y ese clic cuenta como paso.

## Resultado por tienda (categorías cerradas)

| Código | Significado |
|---|---|
| `EXITO` | Producto correcto en el carrito, verificado visualmente (screenshot del carrito con el ítem) |
| `EXITO_PARCIAL` | Llegó a ficha de producto correcta pero el añadir-al-carrito falló (botón inoperable, error JS, stock fantasma) |
| `FALLO_BUSQUEDA` | No encontró ningún producto pertinente (buscador/navegación inutilizables para el agente) |
| `FALLO_INTERACCION` | Encontró pero no pudo operar (modales que atrapan, selectores de talla/variante imposibles, elementos no accesibles) |
| `BLOQUEO_ANTIBOT` | CAPTCHA, challenge o 403 antes o durante la tarea. **No se evade: se registra y se para.** |
| `NO_MEDIBLE` | La tienda quedó fuera por causas ajenas (caída, mantenimiento) → se sustituye por la siguiente de su celda y se anota |

Regla dura: ante CAPTCHA o challenge anti-bot, el agente PARA. No se resuelve,
no se reintenta con otra identidad, no se maquilla. `BLOQUEO_ANTIBOT` es un
resultado tan válido y tan publicable como `EXITO`.

## Evidencia por tienda (obligatoria, o la medición no cuenta)

1. Transcript completo de acciones del agente (pasos, selectores, decisiones).
2. Screenshot inicial (home), screenshot final (carrito o punto de fallo).
3. Timestamp, duración, nº de pasos.
4. Código de resultado + nota de una línea.

La evidencia se archiva; en el informe se publican los agregados y ejemplos
anonimizados de patrones de fallo (sin humillar a tiendas concretas, norma del
estudio).

## Calibración

Antes de las 40: **3 tiendas de calibración fuera de la muestra** (no computan)
para verificar que el presupuesto de pasos, la redacción de la tarea y las
categorías funcionan. Solo tras la calibración se congela el protocolo.

## Métricas que saldrán (y ninguna más sin re-preregistrar)

- % de tiendas donde el agente llega al carrito (global, por sector, por tamaño).
- Distribución de categorías de fallo.
- Mediana de pasos hasta el carrito en los éxitos.
- Cruce con el nivel 1: ¿las tiendas con schema Product son más operables? (correlación, no causalidad, y se dirá así).
