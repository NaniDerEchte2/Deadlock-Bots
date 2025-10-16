# Steam Presence Bridge

This directory hosts the standalone Node.js service that keeps a Steam Rich
Presence session alive and synchronises data into the shared SQLite database
used by the Discord bots.  The Python cog (:mod:`cogs.steam.steam_master`) now
acts solely as a coordination hub and relies on this service for any direct
Steam connectivity.

## Running locally

```bash
npm install
npm run start
```

The service reads credentials and other knobs from environment variables.  The
most important ones are:

| Variable | Description |
| --- | --- |
| `STEAM_BOT_USERNAME` / `STEAM_LOGIN` | Steam account name for the initial sign-in. |
| `STEAM_BOT_PASSWORD` / `STEAM_PASSWORD` | Password for the account above. |
| `STEAM_TOTP_SECRET` | Optional shared secret for generating 2FA codes. |
| `DEADLOCK_DB_PATH` | Location of the SQLite database (defaults to the same path as the Python services). |

A refresh token is stored inside `.steam-data/refresh.token`.  Once available,
the bridge logs in automatically without reusing the plaintext credentials.
Token files sit next to the service and are shared with the Python modules via
the commands exposed by ``steam_master``.

## Interaction with the Python bots

* Rich Presence updates and friend snapshots are written into the SQLite
  database so other cogs can react to them.
* Token files (`refresh.token` and `machine_auth_token.txt`) are inspected and
  can be cleaned via Discord commands provided by the hub cog.
* The watchlist, friend request queue, and quick invite tables are polled by the
  service.  Python modules can insert rows into those tables without needing a
  direct Steam connection.

For troubleshooting enable verbose logging by setting ``LOG_LEVEL=debug`` before
starting the service.
