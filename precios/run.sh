#!/bin/bash
# Kiwop Labs · observatorio de precios publicados (cron trimestral: día 1 de
# ene/abr/jul/oct a las 06:30). Recaptura las fuentes (precios.py run) → commitea
# el dataset en public/labs/data/precios → push → deploy a producción → IndexNow de
# las 7 URLs de la página → aviso por email. El cron SOLO recaptura y publica el
# dato; las fuentes nuevas y las correcciones las anota una persona en fuentes.json.
# Log: /var/log/kiwop-labs-precios.log
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
cd /home/kiwop-astro || exit 1
PY=/usr/bin/python3
PERIOD="${1:-$(date +%Y-%m)}"
LOG=/var/log/kiwop-labs-precios.log
DATA=public/labs/data/precios
exec >> "$LOG" 2>&1
echo "=== $(date -Is) precios publicados $PERIOD ==="

fail() {
  echo "FALLO: $1"
  printf 'Subject: [Labs] Observatorio de precios %s FALLÓ\nTo: josep@kiwop.com\n\n%s\nLog: %s\n' "$PERIOD" "$1" "$LOG" | /usr/sbin/sendmail -t
  exit 1
}

RUN_OUT=$($PY scripts/labs/precios/precios.py run --period "$PERIOD" 2>&1) || { echo "$RUN_OUT"; fail "precios.py run devolvió error"; }
echo "$RUN_OUT"
[ -s "$DATA/$PERIOD.json" ] || fail "no se generó $DATA/$PERIOD.json"

git add "$DATA"
if git diff --cached --quiet; then
  echo "sin cambios en el dataset, nada que publicar"
else
  git commit -q -m "Labs: precios publicados $PERIOD (recaptura automática)" || fail "git commit"
  git push -q origin main || fail "git push"
  bash scripts/deploy.sh --prod || fail "deploy"
  printf '%s\n' \
    https://www.kiwop.com/labs/precios-servicios-digitales-espana \
    https://www.kiwop.com/en/labs/digital-services-prices-spain \
    https://www.kiwop.com/ca/labs/preus-serveis-digitals-espanya \
    https://www.kiwop.com/de/labs/preise-digitale-dienstleistungen-spanien \
    https://www.kiwop.com/fr/labs/prix-services-numeriques-espagne \
    https://www.kiwop.com/nl/labs/prijzen-digitale-diensten-spanje \
    https://www.kiwop.com/pt/labs/precos-servicos-digitais-espanha \
    | $PY scripts/indexnow-ping.py || true
fi

# Aviso: resumen de la captura y las fuentes que ya no se verifican (para revisar a mano).
RESUMEN=$(sed -n '/precios verificados/,$p' <<< "$RUN_OUT")
printf 'Subject: [Labs] Observatorio de precios %s publicado\nTo: josep@kiwop.com\n\n%s\n\nPágina: https://www.kiwop.com/labs/precios-servicios-digitales-espana\nDataset: https://www.kiwop.com/labs/data/precios/%s.json\nLog: %s\n' \
  "$PERIOD" "$RESUMEN" "$PERIOD" "$LOG" | /usr/sbin/sendmail -t || echo "aviso falló (no bloqueante)"
echo "=== fin $(date -Is) ==="
