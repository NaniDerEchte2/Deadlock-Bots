# Aktivitäts-Tracking: Text-Scoring + Leaderboards + Public-API (2026-04-17)

## Ziel
Server-Grinding fair machen: Text wird konversations-qualität-gescored (nicht Spam-Count), getrennte Leaderboards Voice/Text auf Discord, Public-API + Discord-OAuth damit die Website (dl-activity) Leaderboard + Personal-Dashboard zeigen kann.

## Plan
`/home/naniadm/.claude/plans/wir-haben-ja-aktivit-ts-piped-crown.md`

## Arbeitsteilung
- **Claude (Orchestrator + Frontend):** Website `dl-activity` Vite-Subprojekt
- **GPT-Worker A (Backend-Tracking):** DB-Schema (`text_stats`, `text_conversation_log`), on_message Hybrid-Scoring (10-Min-Sessions, sqrt-Diminishing, Multi-User-Bonus ×1.5, Reply-Bonus), `!tleaderboard` + Footer-Eigenposition in `!vleaderboard`
- **GPT-Worker B (Backend-API):** 8 neue `/api/public/*` Endpoints in `service/public_stats.py` + Discord-OAuth (login/callback/logout, signed Session-Cookie)

## Erledigt
- GPT-Worker A: DB-Schema für Text-Scoring ergänzt (`text_stats`, `text_conversation_log` inkl. Indizes).
- GPT-Worker A: `UserActivityAnalyzer` um Hybrid-Text-Scoring mit 10-Min-Sessionfenstern, Reply-Bonus, Interaktionsbonus und periodischem Flush erweitert.
- GPT-Worker A: Discord-Commands ergänzt/erweitert: `!tleaderboard` neu, `!vleaderboard` Footer mit eigener Position + Embed-Empty-State.
- GPT-Worker B: `service/public_stats.py` um `/api/public/leaderboard/voice`, `/api/public/leaderboard/text`, `/api/public/me`, `/api/public/me/stats`, `/api/public/me/voice-history`, `/api/public/me/text-history`, `/api/public/me/heatmap`, `/api/public/me/co-players` erweitert.
- GPT-Worker B: Discord-OAuth in `service/public_stats.py` ergänzt (`/auth/discord/login`, `/auth/discord/callback`, `/auth/discord/logout`) inkl. signiertem `dl_session`-Cookie, signiertem OAuth-State-Cookie und CORS/Preflight für Dev-Origins.
- GPT-Worker B: Smoke-Verifikation lokal sauber: `python3 -m py_compile service/public_stats.py`, `python3 -c "from service import public_stats"`, `grep -n "def _handle_" service/public_stats.py`.

## Offen
- Infisical-Secrets für Discord-OAuth (`DISCORD_OAUTH_CLIENT_ID`, `DISCORD_OAUTH_CLIENT_SECRET`, Session-Secret) — erst prüfen, sonst nach User fragen
- E2E-Verifikation Bot↔API↔Frontend
- Commit + Push (noch nicht)

---

# Coaching Overhaul (2026-04-16)

## Ziel
Coaching-System Bot+Website stabilisieren: Parsing raus, echte Ränge, kritische Bugs weg, API abgesichert.

## Status
Durchgang 1 abgeschlossen. Änderungen liegen unstaged — noch nicht committed, User review offen.

## Erledigt

### Bot (`Deadlock-Bots/`)
- `cogs/coaching_panel.py`: `_split_rank_input` + `_split_games_hours` entfernt. Modal-Placeholder mit echten Deadlock-Rängen (Archon/Ascendant/Emissary als Beispiele). Rohtext wird jetzt direkt in `rank` / `games_played` gespeichert, `subrank`/`hours_played` bleiben leer.
- `cogs/coaching_request.py`: AI-Prompt und Embed auf Rohdaten umgestellt (kein künstliches `Subrank N/A` mehr). `_get_availability_label` entfernt. CoachClaim-Callback komplett umgebaut: `defer()` + `followup.send` überall (eliminiert Double-Response-Crash). Thread-Create-Fehler werden sauber gemeldet, DB bleibt konsistent. DM-Fail an den User ist kein fataler Fehler mehr — Session + Thread bleiben aktiv, Coach wird informiert. Zusätzlich outer try/except, damit der Button nie stumm crasht.
- `cogs/coaching_survey.py`: `on_voice_state_update` hat jetzt `@commands.Cog.listener()` — Voice-Events werden endlich empfangen, Survey-Trigger funktioniert wieder in Echtzeit.
- `Docs/deadlock-bots/coaching.md` komplett auf den echten Flow umgeschrieben (Panel → Modal → AI → Coach-Claim → Thread, inkl. Rang-Liste).

### Website-Backend (`Website/builds/backend/app/routers/coaching.py`) — via GPT-Worker `36de3803fac3`
- `require_bot_token()` Dependency (Header `X-Bot-Token`, hmac.compare_digest, 503 wenn ENV fehlt, 401 bei falschem Token) an `POST /requests`, `PATCH /requests/{id}/match`, `POST /surveys`.
- Anonymitäts-Leak in Reviews gefixt: stabiles `sha256(user_id+coach_id)[:6]`-Label statt Username-Präfix.
- Neue ENV: `COACHING_BOT_TOKEN`.

## Offen / bewusst verschoben
- `GET /api/coaching/requests` weiterhin public — nicht akut, aber sollte in einem Folge-Pass auch bot-gated werden.
- Sync-Layer Bot↔Website (aktuell getrennte DBs).
- Frontend-Routen `/coaching/apply`, `/coaching/dashboard`, Coaching-anfragen-Button-Logik.
- Discord-Rolle automatisch bei approvter Coach-Application vergeben.
- User-seitiges Cancel bewusst ausgelassen (User-Entscheidung).

## Verifikation
- `python3 -m py_compile` sauber für: `coaching_panel.py`, `coaching_request.py`, `coaching_survey.py`, `coaching_role_manager.py`, `builds/backend/app/routers/coaching.py`.
- Kein Commit / kein Push bisher.

## Nächster Schritt
User reviewt Änderungen. Bei OK: `COACHING_BOT_TOKEN` setzen (Infisical), dann commit+push in beiden Repos.
