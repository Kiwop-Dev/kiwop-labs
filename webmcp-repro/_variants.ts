/**
 * Matriz del repro de crbug.com/534655509. Cada variante enciende o apaga las tres
 * piezas del disparador; el orden es el de la tabla que va en el bug.
 */
export interface ReproVariant {
  slug: string;
  label: string;
  router: boolean;
  imperative: boolean;
  declarative: boolean;
  expected: 'crash' | 'ok';
}

export const VARIANTS: ReproVariant[] = [
  {
    slug: 'router-both',
    label: 'router + imperative + declarative',
    router: true,
    imperative: true,
    declarative: true,
    expected: 'crash',
  },
  {
    slug: 'router-imperative',
    label: 'router + imperative only',
    router: true,
    imperative: true,
    declarative: false,
    expected: 'ok',
  },
  {
    slug: 'router-declarative',
    label: 'router + declarative only',
    router: true,
    imperative: false,
    declarative: true,
    expected: 'ok',
  },
  {
    slug: 'router-none',
    label: 'router + token, zero tools',
    router: true,
    imperative: false,
    declarative: false,
    expected: 'ok',
  },
  {
    slug: 'mpa-both',
    label: 'no router, imperative + declarative',
    router: false,
    imperative: true,
    declarative: true,
    expected: 'ok',
  },
  {
    slug: 'mpa-none',
    label: 'no router, token, zero tools',
    router: false,
    imperative: false,
    declarative: false,
    expected: 'ok',
  },
];

export function findVariant(slug: string | undefined): ReproVariant | undefined {
  return VARIANTS.find((v) => v.slug === slug);
}
