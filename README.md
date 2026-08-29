# Fien — gezinsassistent

Een zelf-gehoste gezins-agent die de mentale last van het gezinsleven overneemt.
Jullie appen of fotograferen alles naar één Telegram-chat; Fien maakt er taken van met
een **eigenaar en een deadline**, geeft elke ochtend een dagbriefing, plant zondagavond
de week vooruit, en pakt dagelijks één taak zelf op (uitzoekwerk, concepten) — zonder
ooit iets extern te versturen.

## Wat je nodig hebt

1. **Een Telegram-bot** (2 minuten): open Telegram, zoek `@BotFather`, stuur `/newbot`,
   kies een naam (bijv. "Fien") en een gebruikersnaam (bijv. `jullie_fien_bot`).
   BotFather geeft een token (`123456:ABC-...`) — dat gaat in `.env`.
   Stuur BotFather ook `/setprivacy` → kies je bot → **Disable**, anders ziet de bot
   groepsberichten niet.
2. **Een gezinsgroep**: maak een Telegram-groep met jou, je vrouw en de bot.
3. De server + Anthropic API-key die je al hebt.

## Installeren (op de server)

```bash
cd /root && unzip family-agent.zip && cd family-agent
cp .env.example .env
nano .env        # API-key + bot-token invullen
docker compose up -d --build
docker compose logs -f     # meekijken; Ctrl+C stopt alleen het meekijken
```

Stuur nu in de gezinsgroep `/start`. De bot antwoordt met een **chat-id** (negatief getal
voor groepen). Zet dat in `.env` bij `TELEGRAM_ALLOWED_CHAT_IDS=`, en herstart:

```bash
docker compose restart
```

Klaar. Test met: "Fien, we moeten nog een cadeau regelen voor het feestje van zaterdag."

## Agenda koppelen (Google Calendar, optioneel maar aan te raden)

Fien leest jullie gedeelde gezinsagenda en kan er afspraken inzetten. Eenmalige setup
(~10 minuten), via een "service-account" — een soort robot-account van Google dat jullie
toegang geven tot alleen déze agenda:

1. Ga naar **console.cloud.google.com** (inloggen met je Google-account) →
   projectkiezer linksboven → **New project** → naam "fien" → Create.
2. Zoekbalk bovenin: zoek **"Google Calendar API"** → Enable.
3. Menu → **IAM & Admin → Service Accounts** → **Create service account** →
   naam "fien" → Create → (rollen overslaan) → Done.
4. Klik het account aan → tab **Keys** → **Add key → Create new key → JSON** →
   er downloadt een `.json`-bestand. Bewaar het goed; dit is de sleutel.
5. Kopieer het e-mailadres van het service-account
   (`fien@…iam.gserviceaccount.com`).
6. Open **Google Calendar** (web) → tandwiel → instellingen van jullie
   gezinsagenda → **Delen met specifieke personen** → voeg dat e-mailadres toe met
   rechten **"Wijzigingen aanbrengen in afspraken"**.
7. Zelfde instellingenpagina, kopje **"Agenda integreren"**: kopieer de **Agenda-ID**
   (ziet eruit als `xxxx@group.calendar.google.com`).
8. Zet de sleutel op de server en vul `.env` aan (vanaf je Mac):

```bash
ssh root@JOUW_SERVER_IP "mkdir -p /root/family-agent/workspace/secrets"
scp ~/Downloads/fien-*.json root@JOUW_SERVER_IP:/root/family-agent/workspace/secrets/service-account.json
```

Vul in `.env` de `GOOGLE_CALENDAR_ID=` in, en `docker compose restart`. Test in de chat:
"Fien, wat staat er deze week in de agenda?"

Het sleutelbestand blijft buiten git (`workspace/.gitignore`) en geeft alleen toegang
tot agenda's die jullie er expliciet mee delen.

## Dagelijks gebruik

- Alles wat in je hoofd zit → de groep in. Tekst of foto (lijstje, schoolbrief).
- `/overzicht` — alles wat loopt, gesorteerd op urgentie.
- 07:15 — ochtendbriefing: wat vandaag moet en wie het doet.
- Zondag 19:30 — weekplanning.
- 13:00 — Fien pakt zelf één uitzoektaak op en meldt het resultaat.
- "die is klaar" / "verzet dat naar vrijdag" — gewoon zeggen, Fien werkt het bij.

## Veiligheid & principes

- Alleen jullie eigen chat-ids worden bediend; de rest wordt genegeerd.
- Fien verstuurt nooit iets naar de buitenwereld — voorwerk ja, versturen nee.
- Alles staat als bestanden in `workspace/` onder git; elk moment terug te kijken.
- Budget-plafonds: $1 per actie, $3 per dag (aanpasbaar in `.env`).
- Standaard-eigenaar van nieuwe taken is Jaap — bewuste ontwerpkeuze.

## Kosten

Sonnet-model, korte cycli: reken op **$1–3 per dag** bij normaal gebruik
(plus €7/maand voor de server die er al is).
