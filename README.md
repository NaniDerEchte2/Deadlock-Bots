Community-Bot-Übersicht

Diese README fasst alle Features zusammen, die auf dem Server für Community-Mitglieder bereitstehen. Admin- oder Backoffice-Funktionen sind bewusst ausgelassen.

## Onboarding & Regelbestätigung
- Im Regel-Channel liegt ein permanenter Button **„Weiter ➜“**, der dir einen privaten Onboarding-Thread eröffnet und dort den kompletten Begrüßungsflow startet.【F:cogs/rules_channel.py†L3-L198】
- Der Welcome-DM/Thread-Flow führt dich Schritt für Schritt durch Status-Abfrage, optionale Streamer-Infos, Steam-Verknüpfung und Regelbestätigung – inklusive abschließender Tipps je nach Spielstatus.【F:cogs/welcome_dm/dm_main.py†L116-L340】

## Steam-Verknüpfung & Verified-Rolle
- Slash-Commands `/link` und `/link_steam` starten wahlweise den Discord-OAuth-Flow (mit automatischem Steam-Fallback) oder direkt Steam OpenID. Nach erfolgreicher Verknüpfung erhältst du sofort eine DM samt Hinweis auf die anstehende Freundschaftsanfrage des Steam-Bots.【F:cogs/steam/steam_link_oauth.py†L748-L825】【F:cogs/steam/steam_link_oauth.py†L307-L325】
- Zusätzliche Befehle `/links`, `/whoami`, `/addsteam`, `/setprimary` und `/unlink` helfen beim Auflisten, Prüfen oder Anpassen deiner gespeicherten Steam-Accounts – inklusive Primär-Markierung und manuellem Eintrag, falls die automatische Verknüpfung einmal nicht greift.【F:cogs/steam/steam_link_oauth.py†L802-L921】
- Solltest du länger als 30 Minuten im Voice sein ohne Steam-Link (und keine Opt-out-Rolle besitzen), bekommst du eine DM mit Direkt-Buttons für OAuth, Steam-Login, Schnell-Einladung oder manuelle SteamID-Eingabe. Die Ansicht bleibt persistent, bis du sie schließt oder einen Link hinterlegt hast.【F:cogs/steam_link_voice_nudge.py†L28-L520】
- Ein Hintergrunddienst durchsucht regelmäßig alle verifizierten Steam-Verknüpfungen und vergibt automatisch die Server-Rolle **„Verified“**, damit du Zugriff auf geschützte Bereiche behältst.【F:cogs/steam_verified_role.py†L23-L199】

## TempVoice-Lanes
- Sobald du einen Staging-Voice-Channel betrittst, erzeugt das TempVoice-System automatisch eine persönliche Lane, verschiebt dich dorthin und speichert alle Einstellungen in der zentralen DB.【F:cogs/tempvoice/DOK.MD†L5-L161】
- Über das persistente Panel im zugehörigen Textkanal steuerst du deine Lane: DE/EU-Filter, User-Limit, Mindest-Rang (in der Rank-Kategorie), Kick/Ban/Unban sowie **Owner Claim** sind nur einen Button entfernt.【F:cogs/tempvoice/DOK.MD†L102-L188】

## Voice-Aktivität & Leaderboard
- `!vstats [@User]` zeigt Gesamtspielzeit, Punkte sowie Live-Zuwachs der laufenden Session an – inklusive Hinweis, ob du die 3-Minuten-Grace-Rolle trägst.【F:cogs/voice_activity_tracker.py†L476-L527】
- `!vleaderboard` (Aliases `!vlb`, `!voicetop`) listet die Top-Spieler nach Voice-Punkten, während `!vtest` einen Health-Check für das Voice-System liefert (u. a. aktive Sessions, Grace-Settings und persönlichen Voice-Status).【F:cogs/voice_activity_tracker.py†L533-L623】

## Deadlock Team Balancer
- Das Command-Set `!balance` hilft beim Aufsetzen fairer Matches: `auto`/`voice` erstellt eine Vorschau, `start` legt automatisch zwei Team-Voice-Channels an und moved alle Teilnehmer, `manual` erlaubt eine Auswahl per Mentions.【F:cogs/deadlock_team_balancer.py†L305-L396】
- Weitere Unterbefehle zeigen Ränge (`status`), laufende Matches (`matches`), bereinigen alte Matches (`cleanup`) oder schließen ein Spiel inklusive optionalem Debrief-Channel (`end`).【F:cogs/deadlock_team_balancer.py†L397-L520】

## Clip-Einreichung
- Im Clip-Channel findest du einen persistierenden Button **„Clip einsenden“**. Nach der Rechtebestätigung öffnet sich ein Modal für Link, Credit und Zusatzinfos (inklusive Cooldown gegen Spam).【F:cogs/clip_submission.py†L201-L338】
- Das Panel informiert dich über das aktuelle Wochenfenster und hält Einsendungen automatisch fest. Nach Ablauf generiert der Bot einen TXT-Dump aller Clips und versendet ihn an das verantwortliche Team bzw. postet ihn als Fallback im Channel.【F:cogs/clip_submission.py†L368-L533】

## Feedback Hub
- Ein permanenter Button **„Anonymes Feedback senden“** öffnet ein Formular mit fünf Freitext-Fragen zu Spielerlebnis, Server, Verbesserungen und Wünschen. Nach dem Absenden geht ein anonymes Embed an das Community-Team, optional mit Link zum ursprünglichen Interface.【F:cogs/feedback_hub.py†L28-L205】

## Match-Coaching
- Der Button **„Match-Coaching starten“** erzeugt einen privaten Thread und führt dich durch Rang-, Subrang- und Heldenauswahl sowie einen Kommentar. Abschließend erhältst du eine Zusammenfassung, der Thread wird archiviert und das Coaching-Team informiert.【F:cogs/dl_coaching.py†L217-L520】

## Twitch-Statistiken
- `!twl` funktioniert als Proxy-Befehl für das Twitch-Cog: Im vorgesehenen Statistik-Channel erhältst du ein interaktives Leaderboard mit Filtern wie `samples=`, `avg=`, `partner=`, `limit=`, `sort=` und `order=`. Eine `help`-Abfrage erklärt alle Optionen.【F:cogs/twitch/__init__.py†L16-L61】【F:cogs/twitch/leaderboard.py†L323-L417】

Viel Spaß beim Nutzen der Bots und Features – und danke fürs Mitgestalten der Deutschen Deadlock Community!
