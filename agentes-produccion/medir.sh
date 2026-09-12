#!/bin/bash
# Kiwop Labs · «Agentes de IA en producción»: medición trimestral (cron en arxiu:
# día 1 de ene/abr/jul/oct a las 06:00). Ejecuta agregar.sql en la BD de Nexo por SSH
# (root@mail.kiwop.com, psql solo lectura con las credenciales del .env del propio
# servidor: nada sale de allí salvo el JSON agregado) → escribe
# public/labs/data/agentes-produccion/<periodo>.json + latest.json + index.json →
# commit → push → deploy → IndexNow de las 7 URL → aviso. Solo agregados: ni nombres,
# ni textos, ni ids. Log: /var/log/kiwop-labs-agentes.log
#
# Uso: medir.sh [AAAA-MM] [--no-publish]   (periodo = mes de la medición; ventana =
# los 90 días anteriores al día 1 de ese mes, o hasta hoy si es el mes en curso)
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
cd /home/kiwop-astro || exit 1
PERIODO="${1:-$(date +%Y-%m)}"
PUBLISH=1; [[ "${2:-}" == "--no-publish" ]] && PUBLISH=0
OUT=public/labs/data/agentes-produccion
LOG=/var/log/kiwop-labs-agentes.log
NEXO_HOST=root@mail.kiwop.com
DIAS=90
if [[ -t 1 ]]; then :; else exec >> "$LOG" 2>&1; fi
echo "=== $(date -Is) agentes en producción $PERIODO ==="
fail() {
  echo "FALLO: $1"
  printf 'Subject: [Labs] Agentes en producción %s FALLÓ\nTo: josep@kiwop.com\n\n%s\nLog: %s\n' "$PERIODO" "$1" "$LOG" | /usr/sbin/sendmail -t
  exit 1
}
# Fin de la ventana: hoy si el periodo es el mes en curso; si no, el día 1 del periodo.
if [[ "$PERIODO" == "$(date +%Y-%m)" ]]; then HASTA=$(date +%Y-%m-%d); else HASTA="$PERIODO-01"; fi
mkdir -p "$OUT"
TMP=$(mktemp)
# El SQL viaja por stdin; psql lee el .env de Nexo en el propio servidor.
ssh -o BatchMode=yes -o ConnectTimeout=15 "$NEXO_HOST" 'cd /home/equip/shared && eval "$(grep -E "^DB_(HOST|PORT|DATABASE|USERNAME|PASSWORD)=" .env | sed "s/^/export /")" && PGPASSWORD="$DB_PASSWORD" psql -h "${DB_HOST:-127.0.0.1}" -p "${DB_PORT:-5432}" -U "$DB_USERNAME" -d "$DB_DATABASE" -At -v dias='"$DIAS"' -v hasta='"$HASTA"' -f -' \
  < scripts/labs/agentes-produccion/agregar.sql > "$TMP" || fail "psql por ssh devolvió error: $(head -c 300 "$TMP")"
python3 - "$TMP" "$OUT" "$PERIODO" <<'EOF' || fail "no se pudo escribir el dataset"
import json, sys, os, glob
tmp, out, periodo = sys.argv[1:4]
raw = open(tmp).read().strip()
try:
    data = json.loads(raw)
except Exception as e:
    print("salida no es JSON:", raw[:300]); sys.exit(1)
doc = {
    "series": "agentes-produccion",
    "periodo": periodo,
    "generado_en": data.pop("generado_en", None),
    "ventana": data.pop("ventana"),
    "page": "https://www.kiwop.com/labs/agentes-ia-produccion",
    "license": "CC BY 4.0",
    "metodo": "Agregados de la telemetría de Nexo (plataforma de Kiwop en producción): ejecuciones de agentes, comentarios firmados, PR del worker, triage y guardián de correo, consultas al cerebro, secuencias de leads, metering de la API y del CLI, y crons. Ventana de 90 días. Sin nombres, textos ni ids. Consulta pública en scripts/labs/agentes-produccion/agregar.sql.",
    "notas": [
        "coste_api.coste_facturado_eur es lo que pasa por API de pago; coste_equivalente_suscripcion_eur es lo que va por suscripción (console_auth) valorado a precio de API, no facturado.",
        "coste_cli.coste_lista_usd es el consumo real de las suscripciones del CLI valorado a precio de lista en USD.",
        "Los comentarios de agente nacen pendientes: firmados = una persona los validó en la web.",
        "agentes_proactivos.duracion en segundos desde started_at hasta completed_at.",
    ],
    **data,
}
os.makedirs(out, exist_ok=True)
json.dump(doc, open(f"{out}/{periodo}.json", "w"), ensure_ascii=False, indent=1)
json.dump(doc, open(f"{out}/latest.json", "w"), ensure_ascii=False, indent=1)
months = sorted(os.path.basename(p)[:-5] for p in glob.glob(f"{out}/????-??.json"))
json.dump({"series": "agentes-produccion", "months": months, "latest": months[-1]}, open(f"{out}/index.json", "w"), ensure_ascii=False, indent=1)
print(f"dataset escrito: {out}/{periodo}.json ({len(months)} periodos)")
EOF
rm -f "$TMP"
if [[ $PUBLISH -eq 1 ]]; then
  git add "$OUT"
  if git diff --cached --quiet; then
    echo "sin cambios en el dataset, nada que publicar"
  else
    git commit -q -m "Labs: agentes en producción $PERIODO (medición automática)" || fail "git commit"
    git push -q origin main || fail "git push"
    bash scripts/deploy.sh --prod || fail "deploy"
    for l in "" /en /ca /de /fr /nl /pt; do echo "https://www.kiwop.com$l/labs/agentes-ia-produccion"; done | python3 scripts/indexnow-ping.py || true
  fi
  TEXT="Labs · agentes en producción $PERIODO publicado en https://www.kiwop.com/labs/agentes-ia-produccion (ventana de $DIAS días hasta $HASTA)."
  if [ -f /home/kiwop-astro/.credentials/nexo-token ]; then
    curl -sS -X POST "https://nexo.kiwop.com/api/v1/workspaces/kiwop/slack/dm" \
      -H "Authorization: Bearer $(cat /home/kiwop-astro/.credentials/nexo-token)" \
      -H "Content-Type: application/json" -H "Accept: application/json" \
      -d "$(python3 -c 'import json,sys; print(json.dumps({"to":"josep@kiwop.com","text":sys.argv[1],"human_validated":False}))' "$TEXT")" >/dev/null && echo "aviso enviado" || echo "Slack DM falló"
  fi
fi
echo "=== fin $(date -Is) ==="
