# Birdy — gezinsassistent

Een zelf-gehoste gezins-agent die de mentale last van het gezinsleven overneemt.
Jullie appen of fotograferen alles naar de chat; Birdy maakt er taken van met een
**eigenaar en een deadline**, archiveert documenten in de gezamenlijke Drive-map,
zet data in de agenda, beheert de boodschappenlijst, geeft elke ochtend een
dagbriefing, plant zondagavond de week vooruit, en pakt dagelijks één taak zelf op
(uitzoekwerk, concepten) — zonder ooit iets extern te versturen.

**Kanalen:** Slack (2.0, aanbevolen) en/of Telegram (1.0, vervalt na de overgang).
Beide via uitgaande verbindingen — geen open poorten op de server.

## Principe: code hier, data en secrets op de server

- Deze repo bevat alleen **code en prompts**. Bron van waarheid = git.
- `workspace/` (gezinsdata, eigen git-historie) en `.env` (secrets) leven alleen op
  de server en komen nooit in deze repo. `workspace-template/` is het startsjabloon
  voor een verse installatie: `cp -R workspace-template workspace`.
- Documenten, agenda en lijstjes leven in Google Drive, Google Calendar en Todoist —
  Birdy is de manager, niet de database.

## Installeren (op de server)

```bash
cd /root/family-agent
cp .env.example .env
nano .env        # tokens invullen, zie hieronder
docker compose up -d --build
docker compose logs -f     # meekijken; Ctrl+C stopt alleen het meekijken
```

Let op: na élke `.env`-wijziging is het `docker compose up -d --force-recreate`
(een kale `restart` laadt `.env` niet opnieuw).

## Slack koppelen (2.0)

1. Maak een gratis Slack-workspace voor het gezin, met kanalen **#birdy**
   (conversatie) en **#briefing** (leeskanaal voor de briefings).
2. Op api.slack.com/apps: *Create New App → From scratch* in die workspace.
   - **Socket Mode** aan → App-Level Token met scope `connections:write` → `SLACK_APP_TOKEN`.
   - **OAuth & Permissions** → Bot Token Scopes: `app_mentions:read`,
     `channels:history`, `channels:read`, `chat:write`, `files:read`, `files:write`,
     `im:history`, `im:read`, `im:write`, `reactions:read`, `reactions:write`, `users:read`.
   - **Event Subscriptions** → bot events: `message.channels`, `message.im`,
     `app_mention`, `reaction_added`, `file_shared`.
   - *Install to Workspace* → `SLACK_BOT_TOKEN`. Daarna `/invite @Birdy` in beide kanalen.
3. Kanaal-ids (View channel details → onderaan) in `SLACK_CHANNEL_BIRDY` en
   `SLACK_CHANNEL_BRIEFING`.
4. Start met lege `SLACK_ALLOWED_MEMBER_IDS`, stuur de bot een DM — hij antwoordt met
   je member-id. Invullen (komma-gescheiden), `--force-recreate`, klaar.

Bevestigen van voorstellen kan met een 👍-reactie op Birdy's bericht of gewoon "ja"
in de thread.

## Google koppelen (Calendar + Drive)

Birdy werkt met **OAuth als Jaap** (de Drive-map staat in een persoonlijke My Drive).
Eenmalig, op je eigen laptop:

1. In het bestaande Google Cloud-project: **Drive API** aanzetten (Calendar staat al aan).
2. OAuth consent screen op **In production** (anders verloopt het token elke 7 dagen);
   Credentials → **OAuth Client ID, type Desktop app**.
3. `GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python scripts/google_consent.py`
   → inloggen → het script print het `GOOGLE_REFRESH_TOKEN`.
4. De drie waarden in `.env` op de server; `GOOGLE_CALENDAR_ID` staat er al;
   `DRIVE_ROOT_FOLDER_ID` = het id van de map "Birdy 2.0" (achter `/folders/` in de URL).
5. Werkt alles, dan mag het oude service-account weg
   (`workspace/secrets/service-account.json` + uitschakelen in Google Cloud). Zolang
   de OAuth-variabelen leeg zijn, valt de agenda automatisch terug op het service-account.

Vraag daarna in de chat: "richt je archief in" — Birdy maakt zelf de mappenstructuur
(00 Inbox, 10 Gezin, 20 Huishouden, 30 Financiën, 90 Archief). Bestanden die iemand in
**00 Inbox** zet worden elke 5 minuten opgepikt en als voorstel in de chat gezet.

## Todoist koppelen (boodschappen & acties)

Maak in Todoist twee gedeelde projecten: **Boodschappen** en **Acties**. Zet je
API-token (Settings → Integrations → Developer) in `TODOIST_API_TOKEN`.
Test: "voeg kwark toe aan de boodschappen".

## Telegram (1.0 — tijdens de overgang)

Werkt zoals voorheen: bot via @BotFather (`/setprivacy` → Disable), token in
`TELEGRAM_BOT_TOKEN`, `/start` in de groep voor het chat-id →
`TELEGRAM_ALLOWED_CHAT_IDS`. Laat de variabelen leeg om Telegram uit te zetten.

## Dagelijks gebruik

- Alles wat in je hoofd zit → #birdy in. Tekst, foto of pdf (lijstje, schoolbrief).
- Documenten: Birdy stelt voor (wat, waarheen, welke agenda-items) → 👍 → gearchiveerd.
- "voeg X toe aan de boodschappen" → staat op ieders telefoon in Todoist.
- Zondag 19:30 — weekplanning in #briefing. Elke ochtend 07:15 werkt Birdy stil haar
  aandachtspunten voor het dashboard bij (AGENT_AANDACHT_TIME); de chat-ochtendbriefing
  staat uit (aanzetten = tijd in AGENT_DIGEST_TIME). Uitzoekwerk doet
  Birdy op verzoek ("zoek eens uit …"); het dagelijkse eigen-initiatief-moment staat uit
  (AGENT_PROACTIVE_TIME leeg; vul een tijd in om het aan te zetten).
- "die is klaar" / "verzet dat naar vrijdag" — gewoon zeggen, Birdy werkt het bij.
- Uitrollen gaat via GitHub: een push naar `main` draait de tests (GitHub Actions) en
  start daarna `scripts/deploy.sh` op de VPS (git pull + `docker compose up --build`).
  Nodig: deploy-sleutel van de VPS als *Deploy key* op de repo, en de Actions-sleutel als
  secret `VPS_SSH_KEY` (die mag op de VPS alleen het deploy-script starten).
  Handmatig kan ook: `bash /root/family-agent/scripts/deploy.sh` op de server.
- Sportprogramma's (iCal-link van Nevobo, KNVB, …) in `AGENDA_SYNC_ICS` zet Birdy elke
  paar uur ín de Google-gezinsagenda en houdt ze actueel (verschoven of afgelast = mee).
  `AGENDA_ICS_FEEDS` toont een feed alleen-lezen náást de agenda (zoals FamilyWall).

## Veiligheid & principes

- Alleen gewhiteliste Slack-member-ids / Telegram-chat-ids worden bediend.
- Birdy verstuurt nooit iets naar de buitenwereld — voorwerk ja, versturen nee.
- Alles staat als bestanden in `workspace/` onder git; elk moment terug te kijken.
- Budget-plafonds: $1 per actie, $3 per dag (aanpasbaar in `.env`).
- Standaard-eigenaar van nieuwe taken is Jaap — bewuste ontwerpkeuze.

## Kosten

Sonnet-model, korte cycli: reken op **$1–3 per dag** bij normaal gebruik
(plus €7/maand voor de server die er al is).
