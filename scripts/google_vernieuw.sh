#!/bin/bash
# Vernieuwt het Google-refresh-token van Birdy (nodig zolang de OAuth-app in testmodus staat: token verloopt na 7 dagen).
# Draai dit op je eigen Mac. Er opent een browser: log in als het Birdy-account en geef toestemming.
# Client-id en secret worden van de server gehaald, het nieuwe token gaat direct de server-.env in; niets wordt geprint.
set -euo pipefail
SERVER=${BIRDY_SERVER:-root@167.233.70.137}
DIR=/root/family-agent
HIER=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
$PY -c "import google_auth_oauthlib" 2>/dev/null || { echo "Installeer eerst: $PY -m pip install google-auth-oauthlib"; exit 1; }

echo "→ client-gegevens ophalen van de server"
CID=$(ssh "$SERVER" "grep '^GOOGLE_CLIENT_ID=' $DIR/.env | cut -d= -f2-")
CSEC=$(ssh "$SERVER" "grep '^GOOGLE_CLIENT_SECRET=' $DIR/.env | cut -d= -f2-")
[ -n "$CID" ] && [ -n "$CSEC" ] || { echo "GOOGLE_CLIENT_ID/SECRET niet gevonden in $DIR/.env"; exit 1; }

echo "→ browser opent: log in als het Birdy-Google-account en klik op Toestaan"
TOKEN=$(GOOGLE_CLIENT_ID="$CID" GOOGLE_CLIENT_SECRET="$CSEC" $PY "$HIER/scripts/google_consent.py" | grep '^GOOGLE_REFRESH_TOKEN=' | cut -d= -f2-)
[ -n "$TOKEN" ] || { echo "Geen token ontvangen."; exit 1; }

echo "→ token op de server zetten en container opnieuw starten"
ssh "$SERVER" "cd $DIR && sed -i 's|^GOOGLE_REFRESH_TOKEN=.*|GOOGLE_REFRESH_TOKEN=$TOKEN|' .env && docker compose up -d --force-recreate >/dev/null 2>&1 && echo herstart"
unset TOKEN CID CSEC

echo "→ controleren"
for i in $(seq 1 20); do
  if ssh "$SERVER" "curl -sf http://127.0.0.1:8811/api/overview -H 'X-Dashboard-Key: '\$(grep '^DASHBOARD_TOKEN=' $DIR/.env | cut -d= -f2-)" >/dev/null 2>&1; then break; fi
  sleep 3
done
ssh "$SERVER" "cd $DIR && K=\$(grep '^DASHBOARD_TOKEN=' .env | cut -d= -f2-); P=\$(grep '^DASHBOARD_GELD_PIN=' .env | cut -d= -f2-); curl -s -H \"X-Dashboard-Key: \$K\" -H \"X-Geld-Pin: \$P\" 'http://127.0.0.1:8811/api/geld?ververs=1'" | python3 -c "import json,sys; d=json.load(sys.stdin); print('Geld-register:', 'OK' if d.get('beschikbaar') else 'NIET beschikbaar: ' + str(d.get('fout')))"
echo "Klaar. Let op: in testmodus verloopt dit token over 7 dagen weer; definitieve fix = OAuth-app publiceren (homepage + privacy-pagina)."
