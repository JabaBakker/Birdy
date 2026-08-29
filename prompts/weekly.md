Weekplanning (zondagavond) — {now}

1. Lees `TAKEN.md`, `GEZIN.md`, `VOORKEUREN.md` en `INTENTIES.md`, en haal op
   (fouten = overslaan):
   - de agenda: `python /app/agent/gcal.py list --days 8`
   - het handboek: `python /app/agent/gdrive.py read "20 Huishouden/Huishoudhandboek"`
     → wat komende week (bijna) aan de beurt is, zet je klaar in TAKEN.md
   - de verjaardagen: `python /app/agent/gdrive.py read "20 Huishouden/Verjaardagen"`
     → verjaardagen komende 14 dagen meenemen in het weekbericht (incl. cadeau-check).
   Dit weekbericht is het startpunt van het samen-de-week-doornemen-moment: sluit af
   met één vraag die het gesprek opent (bijv. de drukste dag of een knelpunt in de
   verdeling). Ruim op: verplaats taken naar de juiste secties voor de komende week,
   archiveer ✅-taken ouder dan twee weken, en werk `OVERZICHT.md` bij.
2. Schrijf het weekbericht voor de gezinschat, maximaal ~15 regels:

📅 De week vooruit (DD-MM t/m DD-MM)
• Per dag alleen de dagen waarop iets moet: agenda-afspraken én taken, met eigenaar
⚠️ (knelpunten: te veel op één dag, taken zonder eigenaar/datum)
💡 (max 2 voorstellen voor nieuwe taken die jij zelf ziet aankomen — seizoen, school,
verjaardagen uit GEZIN.md — met de vraag of je ze mag noteren)

Je laatste bericht is letterlijk wat in de chat komt.
