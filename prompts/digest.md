Ochtendbriefing — {now}

1. Lees `TAKEN.md`, `GEZIN.md` en `VOORKEUREN.md`, en haal op (fouten = overslaan):
   - de agenda: `python /app/agent/gcal.py list --days 2`
   - de onderwerpen: `python /app/agent/gdrive.py read "Wat loopt er"`
     → wijkt het Doc af van TAKEN.md (het gezin heeft iets aangepast), neem dat over
   - het handboek: `python /app/agent/gdrive.py read "20 Huishouden/Huishoudhandboek"`
     → staat er iets met "volgende" op of vóór vandaag, benoem het in de briefing en
     stel voor er een actie van te maken
   - de verjaardagen: `python /app/agent/gdrive.py read "20 Huishouden/Verjaardagen"`
     → verjaardag binnen 7 dagen? Benoem hem en vraag (max 1×) of er een cadeau-actie
     op de actielijst moet.
   Verschuif onderwerpen die vandaag/deze week horen naar de juiste sectie, signaleer
   deadlines die naderen of verstreken zijn, en werk `OVERZICHT.md` bij (en het Doc
   "Wat loopt er" als er iets veranderde).
2. Schrijf `AANDACHT.md` (format in je instructies): maximaal drie punten die vandaag
   écht aandacht verdienen, elk één concrete zin met naam. Dit is jouw blik over alles
   heen; het dashboard toont het de hele dag. Is er niets bijzonders, schrijf dan één
   geruststellende regel.
3. Schrijf de dagbriefing voor in de gezinschat. Format, maximaal ~12 regels:

☀️ Goeiemorgen! DD-MM
📅 Agenda vandaag: (afspraken met tijd, alleen als die er zijn)
🔴 Vandaag: (max 3, met eigenaar)
🟠 Komt eraan: (max 3, met datum)
💡 (optioneel: één meedenk-tip of iets dat aandacht verdient)

Vriendelijk en licht van toon; benoem expliciet wie wat oppakt. Is er echt niets voor
vandaag, maak het bericht dan kort en geruststellend. Je laatste bericht is letterlijk
wat in de chat komt.
