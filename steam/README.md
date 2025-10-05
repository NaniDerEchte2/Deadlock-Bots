# Steam Service Helpers

This folder contains the Python utilities that supervise the `service/steam_presence`
Node.js worker.  The main entry point is `SteamPresenceServiceManager`, which is
used by the master Discord bot to start, stop, and inspect the Steam rich presence
bridge directly from Discord.

The manager supports:

- Automatic dependency installation (`npm install`) unless disabled with
  `STEAM_SERVICE_AUTO_INSTALL=0`.
- Boot-time auto start when `AUTO_START_STEAM_SERVICE` is truthy (default: on).
- Crash monitoring with automatic restart.
- Manual control through the `master steam ...` command group.

See `service/steam_presence/README.md` for environment variables required by the
Node.js worker.
