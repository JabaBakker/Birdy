#!/bin/bash
# Uitrollen op de VPS: nieuwste main van GitHub ophalen en de container herbouwen.
# Wordt gestart door GitHub Actions (via een SSH-sleutel die alléén dit script mag
# draaien, zie authorized_keys) of met de hand: bash /root/family-agent/scripts/deploy.sh
# .env en workspace/ staan niet in git en blijven onaangeraakt.
set -euo pipefail
cd /root/family-agent

voor=$(git rev-parse --short HEAD 2>/dev/null || echo "-")
git fetch --quiet origin main
git reset --hard --quiet origin/main
na=$(git rev-parse --short HEAD)
echo "code: $voor → $na ($(git log -1 --format=%s | cut -c1-70))"

docker compose up -d --build 2>&1 | tail -1
sleep 6
docker compose ps --format '{{.Name}} {{.Status}}'
curl -s -o /dev/null -w 'dashboard http %{http_code}\n' http://127.0.0.1:8811/ || echo "dashboard niet bereikbaar"
