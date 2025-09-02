Sehr gut – lass uns das Ganze einmal **sauber dokumentieren**, so dass du (oder jeder andere im Team) auch in ein paar Wochen noch genau verstehst, wie das 2-Bot-System für TempVoice funktioniert.

---

# 📘 Deadlock Bots – TempVoice mit 2-Bot Setup (Anti-429)

## Hintergrund

Discord hat strikte **Rate Limits** für API-Aufrufe (z. B. `channel.edit`).
Da **TempVoice** viele automatische Channel-Bearbeitungen macht (Name ändern, User-Limits, Overwrites etc.), kam es bei hoher Aktivität zu **HTTP 429 (Rate Limited)**.

Die Lösung:
Wir teilen die Arbeit auf **2 Bots** auf.

* **Bot 1 (Main)**

  * kümmert sich um **UI/UX** (Buttons, Views, Embeds, Nachrichten).
  * sendet **keine** übermäßigen `channel.edit()`-Calls mehr.
  * schickt stattdessen Befehle über eine **Socket-Verbindung** an Bot 2.

* **Bot 2 (Worker)**

  * läuft unsichtbar im Hintergrund.
  * ist in derselben Guild wie Bot 1.
  * übernimmt alle **API-intensiven Aufgaben** (Channel-Rename, User-Limit setzen, Overwrites ändern).
  * entlastet damit Bot 1 und reduziert die Rate-Limit-Probleme.

---

## Architektur

```
+----------------+         TCP Socket (JSON)        +----------------+
|                |   <-------------------------->   |                |
|   Main Bot     |                                 |   Worker Bot   |
| (neu_TempVoice)|   sendet: {op, channel_id, ...} | (tempvoice_worker)|
|  - UI + Logic  |-------------------------------->|  - API Calls   |
|  - Events      |   empfängt: {ok, error}         |  - Lane Edits  |
+----------------+                                  +----------------+
```

---

## Aufgabenverteilung

✅ **Bei Bot 1 bleiben**:

* UI / Interaktionen (`discord.ui.View`, Buttons, Dropdowns).
* Messages/Embeds im Interface Channel.
* "Spieler gesucht"-Posts im LFG-Channel.
* Owner/Kick/Ban Logik.
* AFK-Handling + Match-Timer.

➡️ **An Bot 2 ausgelagert**:

* `channel.edit` (Name, User-Limit, Bitrate).
* `channel.set_permissions` (Overwrites für Ränge/Bans).

---

## Code-Organisation

### Main Bot

Pfad: `cogs/neu_TempVoice.py`

* Enthält die gesamte **TempVoice-Logik**.
* Nutzt **WorkerProxy** (`shared/worker_client.py`) als Schnittstelle.
* Versucht **zuerst**, eine Operation über den Worker laufen zu lassen.
* Falls Worker nicht erreichbar: **lokaler Fallback** (Bot 1 macht es selbst).

### Worker Bot

Pfad: `service/tempvoice_worker_bot.py`

* Startet einen **SocketServer** (`shared/socket_bus.py`).
* Lauscht auf einem TCP Port (z. B. `SOCKET_PORT=45679`).
* Empfängt Jobs von Bot 1 und führt sie mit **Bot 2 Discord-Token** aus.
* Antwortet mit `{ok: true}` oder `{ok: false, error: "..."}`
* Macht **keine Nachrichten** in Discord, nur API-Aktionen.

### Shared Utils

Pfad: `shared/`

* `worker_client.py` → Proxy für Bot 1, sendet Jobs.
* `socket_bus.py` → einfacher TCP JSON Server/Client.
* `__init__.py` → leer oder mit kurzem Docstring.

---

## ENV-Setup

### `.env` (Main Bot)

```env
DISCORD_TOKEN="token-main-bot"
SOCKET_PORT=45679
GERMAN_GUILD_ID=1289721245281292288
# ... weitere Variablen wie bisher
```

### `.env.worker` (Worker Bot)

```env
DISCORD_TOKEN="token-worker-bot"
SOCKET_PORT=45679
GERMAN_GUILD_ID=1289721245281292288
```

⚠️ Beide Bots müssen im **gleichen Server (Guild)** sein.

---

## Starten

1. **Main Bot starten** (wie gewohnt):

   ```bash
   python main.py
   ```

2. **Worker Bot starten**:

   ```bash
   python -m dotenv -f .env.worker run -- python service/tempvoice_worker_bot.py
   ```

---

## Beispiel Flow

1. Spieler klickt in Bot 1 auf „✅ Voll“ →
   Bot 1 berechnet neuen Namen/Limits → sendet Task an Worker.

   ```json
   { "op": "edit_channel", "channel_id": 123, "name": "Lane 1 • voll", "user_limit": 6 }
   ```

2. Bot 2 empfängt, führt `channel.edit()` aus, bestätigt:

   ```json
   { "ok": true }
   ```

3. Bot 1 speichert den State in RAM und macht weiter.

---

## Vorteile

* Weniger API-Timeouts (`429`) → bessere Stabilität.
* Main Bot bleibt **reaktiv** für User-Interaktionen.
* Skalierbar → bei Bedarf könnte ein dritter Worker nur für bestimmte Tasks dazukommen.

---

## TODO/Nächste Schritte

* AFK-Logik erweitern: cool-down Timer (5 min, 30 min, 1h).
* Testen, ob Worker bei hoher Last wirklich entlastet.
* Optional: Worker-Fallback weiter optimieren (z. B. Retry-Backoff).

---

👉 Damit hast du jetzt ein **komplettes Architekturdokument** zur aktuellen Umsetzung.

Willst du, dass ich dir auch gleich ein **README.md** in Markdown-Format schreibe, das du direkt in deinem GitHub-Branch (`2-Bots-429-API-Timeout-umgehen-last-verteilung`) einchecken kannst?
