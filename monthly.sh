#!/bin/bash
# Kiwop Labs · pasada mensual (cron: día 1 a las 07:00). Sustituye a geo-baseline/run.sh
# como entrada del cron; run.sh sigue valiendo para relanzar solo el baseline a mano.
#
#   1. baseline GEO (geo_baseline.py run)            → public/labs/data/geo-baseline
#   2. preguntas a la IA (preguntas_ia.py run)         → public/labs/data/preguntas-ia
#   3. nota mensual (nota_mensual.py all): hechos → borrador en 7 idiomas en blogs/
#      → tarea en Nexo para Josep + Slack. NO publica el post: eso lo hace una persona.
#   4. commit (datasets + borradores) → push → deploy → IndexNow → aviso del baseline.
#
# Cada paso que falla se anota y se avisa; los siguientes siguen si pueden (los
# datasets se publican aunque la nota falle, y al revés). Log: /var/log/kiwop-labs-monthly.log
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
cd /home/kiwop-astro || exit 1
PY=/root/borsa/venv/bin/python
MONTH="${1:-$(date +%Y-%m)}"
LOG=/var/log/kiwop-labs-monthly.log
exec >> "$LOG" 2>&1
echo "=== $(date -Is) Labs mensual $MONTH ==="
ERRORS=()

warn() { echo "FALLO: $1"; ERRORS+=("$1"); }

$PY scripts/labs/geo-baseline/geo_baseline.py run --month "$MONTH" || warn "baseline: geo_baseline.py run"
[ -s "public/labs/data/geo-baseline/$MONTH.json" ] || warn "baseline: no se generó $MONTH.json"

$PY scripts/labs/preguntas-ia/preguntas_ia.py run --month "$MONTH" || warn "preguntas-ia: preguntas_ia.py run"

$PY scripts/labs/nota-mensual/nota_mensual.py facts --month "$MONTH" || warn "nota: facts"
$PY scripts/labs/nota-mensual/nota_mensual.py draft --month "$MONTH" || warn "nota: draft (Claude)"

git add public/labs/data/geo-baseline public/labs/data/preguntas-ia
git add blogs/kiwop-labs-*"$MONTH"*.md 2>/dev/null || true
if git diff --cached --quiet; then
  echo "sin cambios que publicar"
else
  git commit -q -m "Labs: pasada mensual $MONTH (baseline GEO, preguntas a la IA, borrador de la nota)" || warn "git commit"
  git push -q origin main || warn "git push"
  bash scripts/deploy.sh --prod || warn "deploy"
  {
    for l in "" /en /ca /de /fr /nl /pt; do echo "https://www.kiwop.com$l/labs/geo-baseline"; done
    for u in /labs/ranking-agencias-ia-espana /en/labs/ai-agencies-spain-ranking /ca/labs/ranquing-agencies-ia-espanya /de/labs/ki-agenturen-spanien-ranking /fr/labs/classement-agences-ia-espagne /nl/labs/ai-bureaus-spanje-ranking /pt/labs/ranking-agencias-ia-espanha; do echo "https://www.kiwop.com$u"; done
    for u in /labs/preguntas-ia-espana /en/labs/ai-questions-spain /ca/labs/preguntes-ia-espanya /de/labs/ki-fragen-spanien /fr/labs/questions-ia-espagne /nl/labs/ai-vragen-spanje /pt/labs/perguntas-ia-espanha; do echo "https://www.kiwop.com$u"; done
  } | python3 scripts/indexnow-ping.py || true
fi

# La tarea de Nexo va después del deploy: así el borrador ya está en el repo cuando Josep lo abre.
if [ -s "labs-runs/notas/$MONTH/draft.json" ]; then
  $PY scripts/labs/nota-mensual/nota_mensual.py task --month "$MONTH" || warn "nota: tarea Nexo"
fi
$PY scripts/labs/geo-baseline/geo_baseline.py notify --month "$MONTH" || echo "aviso del baseline falló (no bloqueante)"

if [ ${#ERRORS[@]} -gt 0 ]; then
  printf 'Subject: [Labs] Pasada mensual %s con fallos\nTo: josep@kiwop.com\n\n%s\nLog: %s\n' "$MONTH" "$(printf '%s\n' "${ERRORS[@]}")" "$LOG" | /usr/sbin/sendmail -t
fi
echo "=== fin $(date -Is) (${#ERRORS[@]} fallo/s) ==="
