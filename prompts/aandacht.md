Aandachtspunten bijwerken (stil ochtendmoment) — {now}

Dit is een stille cyclus: je stuurt géén bericht naar de chat. Je enige product is een
vers `AANDACHT.md` voor het dashboard. Werk compact: hooguit één keer lezen per bron.

1. Haal op (fouten = overslaan, dan zonder die bron):
   - de agenda: `python /app/agent/gcal.py list --days 3`
   - de acties: `python /app/agent/todoist.py list --lijst acties`
   - de onderwerpen: `python /app/agent/gdrive.py read "Wat loopt er"`
   - het handboek: `python /app/agent/gdrive.py read "20 Huishouden/Huishoudhandboek"`
   - de verjaardagen: `python /app/agent/gdrive.py read "20 Huishouden/Verjaardagen"`
   Lees ook `VOORKEUREN.md` (prioriteiten van het gezin).
2. Schrijf `AANDACHT.md` (format in je instructies): maximaal drie punten die vandaag of
   deze week écht aandacht verdienen, elk één concrete zin met naam. Denk mee, herken
   verbanden (een feestje zonder cadeau-actie, een deadline die een andere afspraak
   raakt, iets dat al lang blijft liggen), en noem geen dingen die het dashboard al
   vanzelf laat zien tenzij je er iets aan toevoegt. Tijden alleen uit de agenda van
   deze cyclus. Is er niets bijzonders: één geruststellende regel.
3. Verander verder niets (geen TAKEN.md, OVERZICHT.md, Doc of Todoist) en antwoord
   met exact één woord: STIL
