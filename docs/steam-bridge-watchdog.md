# Steam Bridge Watchdog

Der Steam-Bridge-Watchdog ist ein externer Helferprozess. Er liest nur den veröffentlichten Zustand aus `standalone_bot_state` plus einige Diagnosedaten aus `steam_tasks` und `steam_friend_requests`. Er hängt nicht am Discord-Master-Prozess.

## Zweck

Der Watchdog soll genau die Fälle abfangen, in denen die Steam Bridge zwar noch als Prozess lebt, aber fachlich kaputt ist, zum Beispiel:

- Bridge läuft, ist aber nicht bei Steam eingeloggt
- Bridge meldet Login, aber ohne `steam_id64`
- Friend-Requests stauen sich an und `AUTH_SEND_FRIEND_REQUEST` läuft in Timeouts
- Heartbeat der Bridge ist veraltet

Der Neustart erfolgt sauber von außen über den konfigurierten Restart-Command, standardmäßig:

```bash
systemctl --user restart deadlock-bot.service
```

Dadurch werden Master-Prozess, Standalone-Manager und Steam Bridge vollständig neu aufgebaut.

## Starten

Einmalig testen:

```bash
python standalone/steam_bridge_watchdog.py --once --verbose
```

Dauerbetrieb:

```bash
python standalone/steam_bridge_watchdog.py --interval 30
```

## Wichtige Parameter

- `--db-path`
- `--state-path`
- `--restart-command`
- `--interval`
- `--grace-period`
- `--heartbeat-max-age`
- `--restart-cooldown`
- `--dry-run`

## Empfohlene systemd --user Unit

`~/.config/systemd/user/deadlock-steam-watchdog.service`

```ini
[Unit]
Description=Deadlock Steam Bridge Watchdog
After=default.target

[Service]
Type=simple
WorkingDirectory=%h/Documents/Deadlock-Bots
ExecStart=%h/Documents/Deadlock-Bots/.venv/bin/python standalone/steam_bridge_watchdog.py --interval 30
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
```

Danach:

```bash
systemctl --user daemon-reload
systemctl --user enable --now deadlock-steam-watchdog.service
systemctl --user status deadlock-steam-watchdog.service
```

## Interner Self-Heal

Der interne Restart im Master-Bot ist standardmäßig deaktiviert. Falls nötig, kann er bewusst wieder aktiviert werden über:

```bash
STEAM_BRIDGE_INTERNAL_SELF_HEAL=1
```

Für den Regelbetrieb ist der externe Watchdog die saubere Variante.
