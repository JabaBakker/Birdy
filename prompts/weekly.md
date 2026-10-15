Weekplanning (zondagavond) — {now}

1. Lees `GEZIN.md`, `VOORKEUREN.md` en `INTENTIES.md`, en haal op
   (fouten = overslaan):
   - de agenda: `python /app/agent/gcal.py list --days 8`
   - de acties: `python /app/agent/todoist.py list --lijst acties`
   - het handboek: `python /app/agent/gdrive.py read "20 Huishouden/Huishoudhandboek"`
     → wat komende week (bijna) aan de beurt is, zet je op de actielijst in Todoist
   - de verjaardagen: `python /app/agent/gdrive.py read "20 Huishouden/Verjaardagen"`
     → verjaardagen komende 14 dagen meenemen in het weekbericht (incl. cadeau-check).
   Dit weekbericht is het startpunt van het samen-de-week-doornemen-moment: sluit af
   met één vraag die het gesprek opent (bijv. de drukste dag of een knelpunt in de
   verdeling). Ruim op in Todoist: acties zonder eigenaar of datum aanvullen, dubbele
   samenvoegen, en wat al weken over de datum is benoemen met een voorstel (nieuwe
   datum of schrappen).
2. Ruim je geheugen op: `python /app/agent/geheugen.py opruimen`, en loop de actieve
   punten onderaan je instructies na. Vergeet wat achterhaald is en geef tijdgebonden
   punten zonder einddatum alsnog een `--tot`. Noem in het weekbericht kort wat je
   vergeten bent, als het iets was waar het gezin op rekende.
3. Schrijf `AANDACHT.md` (format in je instructies): maximaal drie punten die komende
   week écht aandacht verdienen, elk één concrete zin met naam. Het dashboard toont ze.
4. Schrijf het weekbericht voor de gezinschat, maximaal ~15 regels:

📅 De week vooruit (DD-MM t/m DD-MM)
• Per dag alleen de dagen waarop iets moet: agenda-afspraken én taken, met eigenaar
⚠️ (knelpunten: te veel op één dag, taken zonder eigenaar/datum)
💡 (max 2 voorstellen voor nieuwe taken die jij zelf ziet aankomen — seizoen, school,
verjaardagen uit GEZIN.md — met de vraag of je ze mag noteren)

Je laatste bericht is letterlijk wat in de chat komt.
