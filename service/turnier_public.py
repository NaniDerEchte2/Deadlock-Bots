"""Public-facing tournament website server (port 8767).

OAuth flow: turnier.earlysalty.com/auth/login
    -> link.earlysalty.com/discord/login?context=turnier
    -> Discord OAuth (identify scope)
    -> link.earlysalty.com/discord/callback
    -> steam_link_oauth.py issues one-time token in SQLite
    -> turnier.earlysalty.com/auth/complete?token=<uuid>
    -> session cookie created here
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from aiohttp import web

from service import db
from cogs.customgames import tournament_store as tstore

if TYPE_CHECKING:
    from discord.ext.commands import Bot

log = logging.getLogger(__name__)

TURNIER_PUBLIC_PORT = int(os.getenv("TURNIER_PUBLIC_PORT", "8767"))
TURNIER_PUBLIC_HOST = os.getenv("TURNIER_PUBLIC_HOST", "127.0.0.1")
TURNIER_PUBLIC_GUILD_ID = int(os.getenv("TURNIER_PUBLIC_GUILD_ID", "0"))

STEAM_LINK_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL") or "https://link.earlysalty.com"
).rstrip("/")

SESSION_COOKIE = "turnier_pub_session"
SESSION_TTL = 6 * 3600  # 6 hours

TURNIER_ROLE_ID = 1474210107255554331

_HTML_PATH = Path(__file__).resolve().parent / "static" / "turnier_public.html"


def _load_html() -> str:
    try:
        return _HTML_PATH.read_text(encoding="utf-8")
    except Exception as exc:
        log.error("turnier_public.html nicht lesbar: %s", exc)
        raise


# ── helpers ──────────────────────────────────────────────────────────────────


def _rank_display(rank: str, subrank: int) -> str:
    label = tstore.rank_label(rank) if rank else "Unbekannt"
    if subrank and subrank > 0:
        return f"{label} {subrank}"
    return label


def _generate_bracket(
    signups: List[Dict[str, Any]], teams: List[Dict[str, Any]]
) -> Dict[str, Any]:
    team_map = {int(t["id"]): t for t in teams}
    team_scores: Dict[int, List[int]] = {}
    solo_entries: List[Dict[str, Any]] = []

    for s in signups:
        rv = int(s.get("rank_value") or 1)
        rsub = int(s.get("rank_subvalue") or 3)
        score = rv * 6 + max(1, min(6, rsub))
        tid = s.get("team_id")
        if tid is not None:
            tid = int(tid)
            team_scores.setdefault(tid, []).append(score)
        else:
            dname = s.get("display_name") or f"User {s.get('user_id')}"
            solo_entries.append(
                {
                    "type": "solo",
                    "id": f"solo_{s.get('user_id')}",
                    "name": str(dname),
                    "score": score,
                }
            )

    entries: List[Dict[str, Any]] = []
    for tid, scores in team_scores.items():
        team = team_map.get(tid, {})
        avg = sum(scores) / len(scores) if scores else 0
        entries.append(
            {
                "type": "team",
                "id": f"team_{tid}",
                "name": str(team.get("name") or f"Team {tid}"),
                "score": round(avg, 2),
                "member_count": len(scores),
            }
        )
    entries.extend(solo_entries)

    if len(entries) < 2:
        return {
            "error": "Mindestens 2 Eintr\u00e4ge f\u00fcr einen Bracket ben\u00f6tigt.",
            "entries": entries,
            "rounds": [],
        }

    entries.sort(key=lambda e: e["score"], reverse=True)
    n = len(entries)
    slots = 1 << math.ceil(math.log2(n)) if n > 1 else 2
    num_rounds = int(math.log2(slots))

    seeded_matches = []
    for i in range(slots // 2):
        a_idx = i
        b_idx = slots - 1 - i
        a = entries[a_idx] if a_idx < n else None
        b = entries[b_idx] if b_idx < n else None
        seeded_matches.append((a, b))

    def round_label(r: int) -> str:
        rem = num_rounds - r
        if rem == 0:
            return "\U0001f3c6 Finale"
        if rem == 1:
            return "\U0001f94a Halbfinale"
        if rem == 2:
            return "\u2694\ufe0f Viertelfinale"
        return f"Runde {r}"

    rounds: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []
    for i, (a, b) in enumerate(seeded_matches):
        auto_winner = None
        if a is None and b is not None:
            auto_winner = b
        elif b is None and a is not None:
            auto_winner = a
        current.append(
            {
                "match_id": f"R1M{i + 1}",
                "entry_a": a,
                "entry_b": b,
                "winner": auto_winner,
                "is_bye": (a is None or b is None),
            }
        )
    rounds.append({"round": 1, "label": round_label(1), "matches": current})

    for r in range(2, num_rounds + 1):
        nxt = []
        for i in range(len(current) // 2):
            nxt.append(
                {
                    "match_id": f"R{r}M{i + 1}",
                    "entry_a": None,
                    "entry_b": None,
                    "winner": None,
                    "is_bye": False,
                }
            )
        rounds.append({"round": r, "label": round_label(r), "matches": nxt})
        current = nxt

    return {
        "entries": entries,
        "rounds": rounds,
        "num_entries": n,
        "num_rounds": num_rounds,
        "slots": slots,
    }


# ── Server class ──────────────────────────────────────────────────────────────


class TurnierPublicServer:
    def __init__(self, bot: "Bot", *, host: str = TURNIER_PUBLIC_HOST, port: int = TURNIER_PUBLIC_PORT) -> None:
        self.bot = bot
        self.host = host
        self.port = port
        self._sessions: Dict[str, Dict[str, Any]] = {}  # token -> {user_id, display_name, csrf_token, expires_at}
        self._runner: Optional[web.AppRunner] = None

        self.app = web.Application(middlewares=[self._security_headers_mw])
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/auth/login", self.handle_auth_login)
        self.app.router.add_get("/auth/complete", self.handle_auth_complete)
        self.app.router.add_get("/auth/logout", self.handle_auth_logout)
        self.app.router.add_get("/api/overview", self.handle_api_overview)
        self.app.router.add_get("/api/me", self.handle_api_me)
        self.app.router.add_post("/api/signup", self.handle_api_signup)
        self.app.router.add_post("/api/withdraw", self.handle_api_withdraw)
        self.app.router.add_post("/api/team/create", self.handle_api_team_create)
        self.app.router.add_post("/api/team/rename", self.handle_api_team_rename)
        self.app.router.add_post("/api/team/kick", self.handle_api_team_kick)
        self.app.router.add_get("/health", self.handle_health)

    # ── Middleware ────────────────────────────────────────────────────────────

    @web.middleware
    async def _security_headers_mw(self, request: web.Request, handler):
        try:
            resp = await handler(request)
        except web.HTTPException as ex:
            resp = ex
        except Exception:
            log.exception("Unhandled error in turnier public server")
            resp = web.Response(text='{"error":"internal"}', content_type="application/json", status=500)

        if not isinstance(resp, web.Response) and isinstance(resp, web.HTTPException):
            return resp

        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return resp

    # ── Session management ────────────────────────────────────────────────────

    def _create_session(self, user_id: int, display_name: str) -> Tuple[str, str]:
        """Returns (session_token, csrf_token)."""
        self._purge_expired_sessions()
        session_token = secrets.token_hex(32)
        csrf_token = secrets.token_hex(16)
        self._sessions[session_token] = {
            "user_id": user_id,
            "display_name": display_name,
            "csrf_token": csrf_token,
            "expires_at": time.time() + SESSION_TTL,
        }
        return session_token, csrf_token

    def _get_session(self, request: web.Request) -> Optional[Dict[str, Any]]:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        data = self._sessions.get(token)
        if not data:
            return None
        if data["expires_at"] < time.time():
            self._sessions.pop(token, None)
            return None
        return data

    def _delete_session(self, request: web.Request) -> None:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            self._sessions.pop(token, None)

    def _purge_expired_sessions(self) -> None:
        now = time.time()
        expired = [k for k, v in self._sessions.items() if v["expires_at"] < now]
        for k in expired:
            del self._sessions[k]

    def _check_csrf(self, request: web.Request, session: Dict[str, Any]) -> bool:
        header_token = request.headers.get("X-CSRF-Token", "")
        return header_token == session.get("csrf_token", "")

    # ── Guild resolution ──────────────────────────────────────────────────────

    def _resolve_guild_id(self) -> int:
        if TURNIER_PUBLIC_GUILD_ID:
            return TURNIER_PUBLIC_GUILD_ID
        guilds = self.bot.guilds
        if guilds:
            return guilds[0].id
        return 0

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "ts": int(time.time())})

    async def handle_index(self, request: web.Request) -> web.Response:
        try:
            html = _load_html()
        except Exception:
            return web.Response(text="Seite nicht verfügbar.", status=503)
        return web.Response(text=html, content_type="text/html")

    async def handle_auth_login(self, request: web.Request) -> web.Response:
        url = f"{STEAM_LINK_BASE_URL}/discord/login?context=turnier"
        raise web.HTTPFound(location=url)

    async def handle_auth_complete(self, request: web.Request) -> web.Response:
        token = request.query.get("token", "")
        if not token:
            raise web.HTTPFound(location="/")

        try:
            data = await tstore.consume_auth_token_async(token)
        except Exception:
            log.exception("consume_auth_token_async fehlgeschlagen")
            raise web.HTTPFound(location="/")

        if not data:
            raise web.HTTPFound(location="/")

        user_id = int(data["user_id"])
        display_name = str(data["display_name"])
        session_token, _csrf = self._create_session(user_id, display_name)

        resp = web.HTTPFound(location="/")
        resp.set_cookie(
            SESSION_COOKIE,
            session_token,
            httponly=True,
            samesite="Lax",
            max_age=SESSION_TTL,
            secure=True,
        )
        raise resp

    async def handle_auth_logout(self, request: web.Request) -> web.Response:
        self._delete_session(request)
        resp = web.HTTPFound(location="/")
        resp.del_cookie(SESSION_COOKIE)
        raise resp

    async def handle_api_overview(self, request: web.Request) -> web.Response:
        guild_id = self._resolve_guild_id()
        if not guild_id:
            return web.json_response({"error": "guild not configured"}, status=503)

        try:
            await tstore.ensure_schema_async()
            teams_raw = await tstore.list_teams_async(guild_id)
            signups_raw = await tstore.list_signups_async(guild_id)
            period = await tstore.get_active_period_async(guild_id)
        except Exception:
            log.exception("overview DB error")
            return web.json_response({"error": "db error"}, status=500)

        # Build signup map by user_id for quick lookup
        signup_by_user = {int(s["user_id"]): s for s in signups_raw}

        # Build member lists per team
        members_by_team: Dict[int, List[Dict[str, Any]]] = {}
        solo_signups = []
        for s in signups_raw:
            uid = int(s["user_id"])
            dname = s.get("display_name") or f"User {uid}"
            rank_key = str(s.get("rank") or "initiate")
            rank_sub = int(s.get("rank_subvalue") or 0)
            entry = {
                "user_id": uid,
                "display_name": dname,
                "rank": rank_key,
                "rank_label": _rank_display(rank_key, rank_sub),
                "rank_subvalue": rank_sub,
            }
            tid = s.get("team_id")
            if tid is not None:
                members_by_team.setdefault(int(tid), []).append(entry)
            else:
                solo_signups.append(entry)

        teams_out = []
        for t in teams_raw:
            tid = int(t["id"])
            teams_out.append(
                {
                    "id": tid,
                    "name": str(t.get("name") or f"Team {tid}"),
                    "created_by": t.get("created_by"),
                    "member_count": int(t.get("member_count") or 0),
                    "members": members_by_team.get(tid, []),
                }
            )

        bracket = _generate_bracket(signups_raw, teams_raw)

        return web.json_response(
            {
                "guild_id": guild_id,
                "active_period": period,
                "teams": teams_out,
                "solo_signups": solo_signups,
                "bracket": bracket,
            }
        )

    async def handle_api_me(self, request: web.Request) -> web.Response:
        session = self._get_session(request)
        if not session:
            return web.json_response({"logged_in": False, "user": None})

        guild_id = self._resolve_guild_id()
        user_id = int(session["user_id"])

        signup = None
        if guild_id:
            try:
                signup = await tstore.get_signup_async(guild_id, user_id)
            except Exception:
                pass

        return web.json_response(
            {
                "logged_in": True,
                "user": {
                    "id": user_id,
                    "display_name": session["display_name"],
                    "csrf_token": session["csrf_token"],
                },
                "signup": signup or None,
            }
        )

    async def handle_api_signup(self, request: web.Request) -> web.Response:
        session = self._get_session(request)
        if not session:
            return web.json_response({"error": "not logged in"}, status=401)
        if not self._check_csrf(request, session):
            return web.json_response({"error": "invalid csrf"}, status=403)

        guild_id = self._resolve_guild_id()
        if not guild_id:
            return web.json_response({"error": "guild not configured"}, status=503)

        user_id = int(session["user_id"])
        display_name = str(session["display_name"])

        # Check period
        period = await tstore.get_active_period_async(guild_id)
        if not period:
            return web.json_response({"error": "Kein aktiver Anmeldezeitraum."}, status=400)

        from datetime import datetime
        try:
            now = datetime.now()
            start = datetime.fromisoformat(str(period["registration_start"]))
            end = datetime.fromisoformat(str(period["registration_end"]))
            if not (start <= now <= end):
                return web.json_response({"error": "Anmeldezeitraum ist nicht offen."}, status=400)
        except Exception:
            return web.json_response({"error": "Zeitraum-Fehler."}, status=500)

        # Check role via bot guild
        guild = self.bot.get_guild(guild_id)
        if guild:
            member = guild.get_member(user_id)
            if member:
                has_role = any(r.id == TURNIER_ROLE_ID for r in member.roles)
                if not has_role:
                    return web.json_response(
                        {"error": f"Du benötigst die Turnier-Rolle ({TURNIER_ROLE_ID})."}, status=403
                    )

        # Check steam link + rank
        steam_row = await db.query_one_async(
            """
            SELECT deadlock_rank, deadlock_rank_name, deadlock_subrank
            FROM steam_links
            WHERE user_id = ? AND verified = 1
            ORDER BY primary_account DESC, deadlock_rank_updated_at DESC
            LIMIT 1
            """,
            (user_id,),
        )
        if not steam_row:
            return web.json_response(
                {"error": "Kein verifizierter Steam-Account verknüpft. Nutze /account_verknüpfen."}, status=400
            )

        rank_tier = int(steam_row.get("deadlock_rank") or 0)
        rank_sub = int(steam_row.get("deadlock_subrank") or 0)
        rank_name = str(steam_row.get("deadlock_rank_name") or "initiate")

        try:
            body = await request.json()
        except Exception:
            body = {}

        mode = str(body.get("mode", "solo"))
        if mode not in ("solo", "team"):
            mode = "solo"

        team_id: Optional[int] = None
        if mode == "team":
            team_id_raw = body.get("team_id")
            if team_id_raw is not None:
                try:
                    team_id = int(team_id_raw)
                except Exception:
                    return web.json_response({"error": "Ungültige team_id."}, status=400)
            else:
                # Auto-create team
                team_name = str(body.get("team_name", "")).strip()
                if not team_name:
                    return web.json_response({"error": "team_name fehlt für Team-Anmeldung."}, status=400)
                try:
                    team = await tstore.get_or_create_team_async(guild_id, team_name, created_by=user_id)
                    team_id = int(team["id"])
                except ValueError as exc:
                    return web.json_response({"error": str(exc)}, status=400)

        try:
            result = await tstore.upsert_signup_async(
                guild_id,
                user_id,
                registration_mode=mode,
                rank=rank_name.lower(),
                rank_subvalue=rank_sub,
                team_id=team_id,
                assigned_by_admin=False,
                display_name=display_name,
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({"ok": True, "signup": result})

    async def handle_api_withdraw(self, request: web.Request) -> web.Response:
        session = self._get_session(request)
        if not session:
            return web.json_response({"error": "not logged in"}, status=401)
        if not self._check_csrf(request, session):
            return web.json_response({"error": "invalid csrf"}, status=403)

        guild_id = self._resolve_guild_id()
        if not guild_id:
            return web.json_response({"error": "guild not configured"}, status=503)

        user_id = int(session["user_id"])
        removed = await tstore.remove_signup_async(guild_id, user_id)
        return web.json_response({"ok": removed})

    async def handle_api_team_create(self, request: web.Request) -> web.Response:
        session = self._get_session(request)
        if not session:
            return web.json_response({"error": "not logged in"}, status=401)
        if not self._check_csrf(request, session):
            return web.json_response({"error": "invalid csrf"}, status=403)

        guild_id = self._resolve_guild_id()
        if not guild_id:
            return web.json_response({"error": "guild not configured"}, status=503)

        user_id = int(session["user_id"])

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        team_name = str(body.get("name", "")).strip()
        if not team_name:
            return web.json_response({"error": "name fehlt."}, status=400)

        # Check the user is signed up
        signup = await tstore.get_signup_async(guild_id, user_id)
        if not signup:
            return web.json_response({"error": "Erst anmelden, dann Team erstellen."}, status=400)

        try:
            team = await tstore.get_or_create_team_async(guild_id, team_name, created_by=user_id)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        # Assign the user to the team
        try:
            await tstore.assign_signup_team_async(guild_id, user_id, team_id=int(team["id"]))
        except Exception as exc:
            log.exception("assign_signup_team_async fehlgeschlagen")
            return web.json_response({"error": str(exc)}, status=500)

        return web.json_response({"ok": True, "team": team})

    async def handle_api_team_rename(self, request: web.Request) -> web.Response:
        session = self._get_session(request)
        if not session:
            return web.json_response({"error": "not logged in"}, status=401)
        if not self._check_csrf(request, session):
            return web.json_response({"error": "invalid csrf"}, status=403)

        guild_id = self._resolve_guild_id()
        if not guild_id:
            return web.json_response({"error": "guild not configured"}, status=503)

        user_id = int(session["user_id"])

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        team_id_raw = body.get("team_id")
        new_name = str(body.get("name", "")).strip()
        if not team_id_raw or not new_name:
            return web.json_response({"error": "team_id und name erforderlich."}, status=400)

        try:
            team_id = int(team_id_raw)
        except Exception:
            return web.json_response({"error": "Ungültige team_id."}, status=400)

        # Only the team creator can rename
        team = await tstore.get_team_async(guild_id, team_id)
        if not team:
            return web.json_response({"error": "Team nicht gefunden."}, status=404)
        if team.get("created_by") != user_id:
            return web.json_response({"error": "Nur der Ersteller kann das Team umbenennen."}, status=403)

        try:
            renamed = await tstore.rename_team_async(guild_id, team_id, new_name)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        return web.json_response({"ok": renamed})

    async def handle_api_team_kick(self, request: web.Request) -> web.Response:
        session = self._get_session(request)
        if not session:
            return web.json_response({"error": "not logged in"}, status=401)
        if not self._check_csrf(request, session):
            return web.json_response({"error": "invalid csrf"}, status=403)

        guild_id = self._resolve_guild_id()
        if not guild_id:
            return web.json_response({"error": "guild not configured"}, status=503)

        user_id = int(session["user_id"])

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)

        team_id_raw = body.get("team_id")
        target_id_raw = body.get("user_id")
        if not team_id_raw or not target_id_raw:
            return web.json_response({"error": "team_id und user_id erforderlich."}, status=400)

        try:
            team_id = int(team_id_raw)
            target_id = int(target_id_raw)
        except Exception:
            return web.json_response({"error": "Ungültige IDs."}, status=400)

        # Only team creator can kick
        team = await tstore.get_team_async(guild_id, team_id)
        if not team:
            return web.json_response({"error": "Team nicht gefunden."}, status=404)
        if team.get("created_by") != user_id:
            return web.json_response({"error": "Nur der Ersteller kann Mitglieder entfernen."}, status=403)

        # Can't kick yourself via kick endpoint; use withdraw
        if target_id == user_id:
            return web.json_response({"error": "Nutze /api/withdraw um dich selbst abzumelden."}, status=400)

        # Unassign target from team (don't delete their signup entirely)
        removed = await tstore.assign_signup_team_async(guild_id, target_id, team_id=None)
        return web.json_response({"ok": bool(removed)})

    # ── Server lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        await tstore.ensure_schema_async()
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()

        max_retries = 5
        retry_delay = 0.5
        for attempt in range(max_retries):
            try:
                import errno
                site = web.TCPSite(self._runner, host=self.host, port=self.port)
                await site.start()
                log.info(
                    "TurnierPublicServer läuft auf %s:%s",
                    self.host,
                    self.port,
                )
                return
            except OSError as e:
                is_in_use = e.errno == 10048 or getattr(e, "errno", None) == errno.EADDRINUSE
                if is_in_use and attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                log.exception("TurnierPublicServer konnte nicht starten")
                break
            except Exception:
                log.exception("TurnierPublicServer konnte nicht starten")
                break

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            log.info("TurnierPublicServer gestoppt")
