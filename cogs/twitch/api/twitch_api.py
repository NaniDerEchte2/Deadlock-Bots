import asyncio
import time
import logging
from typing import Dict, List, Optional, Tuple, Union

import aiohttp
from service.http_client import build_resilient_connector

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"


class TwitchAPI:
    """
    Async Wrapper für Twitch Helix mit App-Access-Token.

    - Eine wiederverwendete aiohttp.ClientSession (lazy erstellt)
    - Token wird automatisch geholt/refresh't
    - Hilfsfunktionen für Users, Streams & Kategorien
    """

    def __init__(self, client_id: str, client_secret: str, session: Optional[aiohttp.ClientSession] = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._session = session
        self._own_session = False
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._lock = asyncio.Lock()
        self._category_cache: Dict[str, str] = {}  # name_lower -> id
        self._log = logging.getLogger("TwitchStreams")

    # ---- Session lifecycle -------------------------------------------------
    def _ensure_session(self) -> None:
        if self._session is None:
            connector = build_resilient_connector()
            timeout = aiohttp.ClientTimeout(total=20)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                trust_env=True,
            )
            self._own_session = True

    def get_http_session(self) -> aiohttp.ClientSession:
        """Return the internal aiohttp session, ensuring it exists."""
        self._ensure_session()
        assert self._session is not None
        return self._session

    async def aclose(self) -> None:
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    # ---- OAuth -------------------------------------------------------------
    async def _ensure_token(self):
        self._ensure_session()
        async with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return
            assert self._session is not None
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            }
            async with self._session.post(TWITCH_TOKEN_URL, data=data) as r:
                if r.status != 200:
                    txt = await r.text()
                    self._log.error("twitch token exchange failed: HTTP %s: %s", r.status, txt[:300].replace("\n", " "))
                    r.raise_for_status()
                js = await r.json()
                self._token = js.get("access_token")
                expires = js.get("expires_in", 3600)
                self._token_expiry = time.time() + float(expires)

    def _headers(self) -> Dict[str, str]:
        return {"Client-ID": self.client_id, "Authorization": f"Bearer {self._token}"}

    async def _post(
        self,
        path: str,
        json: Optional[dict] = None,
        *,
        log_on_error: bool = True,
        oauth_token: Optional[str] = None,
        max_attempts: int = 3,
        request_timeout_total: Optional[float] = None,
    ) -> Dict:
        # Allow caller to override the auth token (e.g. EventSub with user tokens)
        token_override = (oauth_token or "").strip()
        if token_override.lower().startswith("oauth:"):
            token_override = token_override.split(":", 1)[1]

        if not token_override:
            await self._ensure_token()
            token_override = self._token or ""

        self._ensure_session()
        assert self._session is not None
        url = f"{TWITCH_API_BASE}{path}"
        last_exc: Optional[Exception] = None
        attempts = max(1, min(int(max_attempts or 1), 5))
        request_timeout = None
        if request_timeout_total is not None:
            request_timeout = aiohttp.ClientTimeout(total=max(0.1, float(request_timeout_total)))

        for attempt in range(attempts):
            try:
                headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {token_override}"}
                request_kwargs = {"headers": headers, "json": json}
                if request_timeout is not None:
                    request_kwargs["timeout"] = request_timeout
                async with self._session.post(url, **request_kwargs) as r:
                    if r.status not in {200, 202}:
                        txt = await r.text()
                        txt_one_line = txt[:300].replace("\n", " ")
                        if log_on_error:
                            self._log.error(
                                "POST %s failed: HTTP %s: %s", path, r.status, txt_one_line
                            )
                        else:
                            self._log.debug(
                                "POST %s failed (quiet): HTTP %s: %s", path, r.status, txt[:180].replace("\n", " ")
                            )
                        raise aiohttp.ClientResponseError(
                            request_info=r.request_info,
                            history=r.history,
                            status=r.status,
                            message=txt_one_line or (r.reason or ""),
                            headers=r.headers,
                        )
                    return await r.json()
            except aiohttp.ClientResponseError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < attempts - 1:
                    delay = 0.5 * (attempt + 1)
                    self._log.warning(
                        "POST %s retry %s/%s after %s (%s)",
                        path,
                        attempt + 1,
                        attempts,
                        delay,
                        exc.__class__.__name__,
                    )
                    await asyncio.sleep(delay)
                    continue
                self._log.error("POST %s failed after retries: %s", path, exc)
                raise last_exc
        # Defensive guard to avoid an implicit None on unexpected fallthrough
        raise last_exc or RuntimeError(f"POST {path} failed without raising")

    # ---- Core GET ----------------------------------------------------------
    async def _get(
        self,
        path: str,
        params: Optional[Union[Dict[str, str], List[Tuple[str, str]]]] = None,
        *,
        log_on_error: bool = True,
    ) -> Dict:
        await self._ensure_token()
        self._ensure_session()
        assert self._session is not None
        url = f"{TWITCH_API_BASE}{path}"
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                async with self._session.get(url, headers=self._headers(), params=params) as r:
                    if r.status != 200:
                        txt = await r.text()
                        if log_on_error:
                            self._log.error(
                                "GET %s failed: HTTP %s: %s", path, r.status, txt[:300].replace("\n", " ")
                            )
                        else:
                            self._log.debug(
                                "GET %s failed (quiet): HTTP %s: %s", path, r.status, txt[:180].replace("\n", " ")
                            )
                        if r.status in {500, 502, 503, 504} and attempt < 2:
                            delay = 0.5 * (attempt + 1)
                            self._log.warning(
                                "GET %s retry %s/3 after HTTP %s (%ss)",
                                path,
                                attempt + 1,
                                r.status,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                        r.raise_for_status()
                    return await r.json()
            except aiohttp.ClientResponseError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < 2:
                    delay = 0.5 * (attempt + 1)
                    self._log.warning("GET %s retry %s/3 after %s (%s)", path, attempt + 1, delay, exc.__class__.__name__)
                    await asyncio.sleep(delay)
                    continue
                self._log.error("GET %s failed after retries: %s", path, exc)
                raise last_exc
        # Sollte nie erreicht werden; defensive Absicherung gegen implizites None
        raise last_exc or RuntimeError(f"GET {path} failed without raising")

    # ---- Categories --------------------------------------------------------
    async def search_category_id(self, query: str) -> Optional[str]:
        if not query:
            return None
        ql = query.lower()
        if ql in self._category_cache:
            return self._category_cache[ql]
        js = await self._get("/search/categories", params={"query": query, "first": "25"})
        best: Optional[str] = None
        for item in js.get("data", []) or []:
            name = (item.get("name") or "").strip()
            if name.lower() == ql:
                best = item.get("id")
                break
            if not best and name.lower().startswith(ql):
                best = item.get("id")
        if best:
            self._category_cache[ql] = best
        return best

    async def get_category_id(self, name: str) -> Optional[str]:
        return await self.search_category_id(name)

    # ---- Users & Streams ---------------------------------------------------
    async def get_users(self, logins: List[str]) -> Dict[str, Dict]:
        out: Dict[str, Dict] = {}
        if not logins:
            return out
        for i in range(0, len(logins), 100):
            chunk = logins[i:i + 100]
            params: List[Tuple[str, str]] = [("login", x) for x in chunk]
            js = await self._get("/users", params=params)
            for u in js.get("data", []) or []:
                login = (u.get("login") or "").lower()
                out[login] = u
        return out

    async def get_user_info(self, login: str) -> Optional[Dict]:
        """Liefert detaillierte Informationen für einen einzelnen User (inkl. Bio/Description)."""
        users = await self.get_users([login])
        return users.get(login.lower())

    async def _fetch_stream_page(
        self,
        *,
        game_id: Optional[str] = None,
        language: Optional[str] = None,
        first: int = 100,
        after: Optional[str] = None,
        logins: Optional[List[str]] = None,
    ) -> Tuple[List[Dict], Optional[str]]:
        params: List[Tuple[str, str]] = []
        if game_id:
            params.append(("game_id", game_id))
        if language:
            params.append(("language", language))
        if logins:
            for lg in logins:
                params.append(("user_login", lg))
        params.append(("first", str(max(1, min(first, 100)))))
        if after:
            params.append(("after", after))

        js = await self._get("/streams", params=params)
        data = js.get("data", []) or []
        cursor = (js.get("pagination") or {}).get("cursor")
        return data, cursor

    async def get_streams_for_game(
        self,
        *,
        game_id: Optional[str],
        game_name: str,
        language: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict]:
        """Fetch up to ``limit`` live streams for the given game.

        Falls die Game-ID unbekannt ist, wird nach ``game_name`` gefiltert.
        """
        limit = max(1, min(limit, 1200))  # hard cap to protect API limits
        out: List[Dict] = []
        after: Optional[str] = None

        if game_id:
            while len(out) < limit:
                data, after = await self._fetch_stream_page(
                    game_id=game_id,
                    language=language,
                    first=100,
                    after=after,
                )
                out.extend(data)
                if not after or not data:
                    break
        else:
            # Fallback: ohne game_id viele Streams ziehen und anschließend filtern
            scanned = 0
            after = None
            while scanned < limit:
                data, after = await self._fetch_stream_page(
                    language=language,
                    first=100,
                    after=after,
                )
                if not data:
                    break
                out.extend(data)
                if not after:
                    break
            target = (game_name or "").lower()
            out = [s for s in out if (s.get("game_name") or "").lower() == target]

        if len(out) > limit:
            out = out[:limit]
        return out

    async def get_streams_by_logins(self, logins: List[str], language: Optional[str] = None) -> List[Dict]:
        """Return live streams for the given user logins.
        Wrapper around Helix /streams with user_login filters (batched).
        """
        if not logins:
            return []
        await self._ensure_token()
        out: List[Dict] = []
        for i in range(0, len(logins), 100):
            chunk = [x for x in logins[i:i+100] if x]
            if not chunk:
                continue
            params: List[Tuple[str, str]] = []
            for lg in chunk:
                params.append(("user_login", lg))
            if language:
                params.append(("language", language))
            js = await self._get("/streams", params=params)
            out.extend(js.get("data", []) or [])
        return out

    async def get_streams_by_category(self, category_id: str, language: Optional[str] = None, limit: int = 500) -> List[Dict]:
        """Return live streams for a given category/game id.
        Convenience wrapper that delegates to get_streams_for_game.
        """
        return await self.get_streams_for_game(game_id=category_id, game_name="", language=language, limit=limit)

    async def create_clip(
        self,
        broadcaster_id: str,
        *,
        user_token: str,
        has_delay: bool = False,
    ) -> Optional[Dict]:
        """Create a clip for a broadcaster using a user OAuth token.

        Note: Twitch determines the final segment from the stream buffer
        (typically the most recent ~90 seconds).
        """
        if not broadcaster_id or not user_token:
            return None

        token = (user_token or "").strip()
        if token.lower().startswith("oauth:"):
            token = token.split(":", 1)[1].strip()
        if not token:
            return None

        self._ensure_session()
        assert self._session is not None

        url = f"{TWITCH_API_BASE}/clips"
        params = {
            "broadcaster_id": str(broadcaster_id),
            "has_delay": "true" if has_delay else "false",
        }
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}",
        }

        try:
            async with self._session.post(url, headers=headers, params=params) as r:
                if r.status not in {200, 202}:
                    txt = await r.text()
                    self._log.debug(
                        "POST /clips failed: HTTP %s: %s",
                        r.status,
                        txt[:180].replace("\n", " "),
                    )
                    return None
                js = await r.json()
        except Exception:
            self._log.debug("create_clip failed for broadcaster=%s", broadcaster_id, exc_info=True)
            return None

        data = js.get("data", []) if isinstance(js, dict) else []
        if not data or not isinstance(data[0], dict):
            return None
        return data[0]

    async def get_latest_vod_thumbnail(self, *, user_id: Optional[str] = None, login: Optional[str] = None) -> Optional[str]:
        """Best-effort: Thumbnail des neuesten VOD (type=archive) als 1280x720-URL."""
        target_user_id = (user_id or "").strip()
        login_normalized = (login or "").strip().lower()

        # Falls f�lschlich ein Login als user_id gespeichert wurde (z.B. aus DB-Fallback),
        # behandle ihn wie einen Login und ermittle die echte numerische ID.
        if target_user_id and not target_user_id.isdigit():
            if not login_normalized:
                login_normalized = target_user_id.lower()
            target_user_id = ""

        if not target_user_id and login_normalized:
            try:
                users = await self.get_users([login_normalized])
                if login_normalized in users:
                    target_user_id = str(users[login_normalized].get("id") or "").strip()
            except Exception:
                self._log.exception("get_latest_vod_thumbnail: konnte user-id nicht ermitteln (%s)", login_normalized)
                return None

        if not target_user_id:
            return None

        try:
            js = await self._get(
                "/videos",
                params={"user_id": target_user_id, "type": "archive", "first": "1"},
            )
        except Exception:
            self._log.exception("get_latest_vod_thumbnail: API-Fehler fuer %s", login_normalized or target_user_id)
            return None

        first = (js.get("data", []) or [])
        if not first:
            return None
        thumb = (first[0].get("thumbnail_url") or "").strip()
        if not thumb:
            return None
        thumb = thumb.replace("{width}", "1280").replace("{height}", "720")
        return f"{thumb}?rand={int(time.time())}"

    async def get_followers_total(self, user_id: str, user_token: Optional[str] = None) -> Optional[int]:
        """Liefert die Follower-Gesamtzahl für einen Broadcaster (best-effort, via /channels/followers)."""
        if not user_id:
            return None
        try:
            if user_token:
                self._ensure_session()
                assert self._session is not None
                url = f"{TWITCH_API_BASE}/channels/followers"
                async with self._session.get(
                    url,
                    headers={"Client-ID": self.client_id, "Authorization": f"Bearer {user_token}"},
                    params={"broadcaster_id": user_id, "first": "1"},
                ) as r:
                    if r.status != 200:
                        txt = await r.text()
                        self._log.debug(
                            "GET /channels/followers (user) failed: HTTP %s: %s",
                            r.status,
                            txt[:180].replace("\n", " "),
                        )
                        r.raise_for_status()
                    js = await r.json()
            else:
                js = await self._get(
                    "/channels/followers",
                    params={"broadcaster_id": user_id, "first": "1"},
                    log_on_error=False,
                )
            if not isinstance(js, dict):
                return None
            total = js.get("total")
            return int(total) if total is not None else None
        except aiohttp.ClientResponseError as exc:
            if exc.status in {401, 403, 404, 410}:
                self._log.debug("Follower-API nicht verfuegbar (%s) fuer %s", exc.status, user_id)
                return None
            self._log.debug("Follower-API Fehler fuer %s: %s", user_id, exc)
            return None
        except Exception:
            self._log.debug("get_followers_total failed for %s", user_id, exc_info=True)
            return None

    async def get_broadcaster_subscriptions(self, user_id: str, user_token: str) -> Optional[Dict]:
        """
        Liefert Subscription-Daten für einen Broadcaster.
        Benötigt Scope: channel:read:subscriptions
        """
        if not user_id or not user_token:
            return None
        try:
            self._ensure_session()
            assert self._session is not None
            url = f"{TWITCH_API_BASE}/subscriptions"
            async with self._session.get(
                url,
                headers={"Client-ID": self.client_id, "Authorization": f"Bearer {user_token}"},
                params={"broadcaster_id": user_id, "first": "1"},
            ) as r:
                if r.status != 200:
                    txt = await r.text()
                    self._log.debug(
                        "GET /subscriptions failed: HTTP %s: %s",
                        r.status,
                        txt[:180].replace("\n", " "),
                    )
                    return None
                js = await r.json()
                return js
        except Exception:
            self._log.debug("get_broadcaster_subscriptions failed for %s", user_id, exc_info=True)
            return None

    async def get_ad_schedule(self, user_id: str, user_token: str) -> Optional[Dict]:
        """
        Liefert den aktuellen Ads-Schedule eines Broadcasters.
        Benötigt Scope: channel:read:ads
        """
        if not user_id or not user_token:
            return None
        try:
            token = user_token.strip()
            if token.lower().startswith("oauth:"):
                token = token.split(":", 1)[1]
            self._ensure_session()
            assert self._session is not None
            url = f"{TWITCH_API_BASE}/channels/ads"
            async with self._session.get(
                url,
                headers={"Client-ID": self.client_id, "Authorization": f"Bearer {token}"},
                params={"broadcaster_id": user_id},
            ) as r:
                if r.status != 200:
                    txt = await r.text()
                    self._log.debug(
                        "GET /channels/ads failed: HTTP %s: %s",
                        r.status,
                        txt[:180].replace("\n", " "),
                    )
                    return None
                js = await r.json()
                data = js.get("data", []) or []
                if not data:
                    return None
                first = data[0]
                return first if isinstance(first, dict) else None
        except Exception:
            self._log.debug("get_ad_schedule failed for %s", user_id, exc_info=True)
            return None

    async def subscribe_eventsub_websocket(
        self,
        *,
        session_id: str,
        sub_type: str,
        condition: Dict[str, str],
        version: str = "1",
        oauth_token: Optional[str] = None,
    ) -> Dict:
        """Register a WebSocket EventSub subscription (e.g. stream.offline)."""
        payload = {
            "type": sub_type,
            "version": version,
            "condition": condition,
            "transport": {"method": "websocket", "session_id": session_id},
        }
        return await self._post(
            "/eventsub/subscriptions",
            json=payload,
            oauth_token=oauth_token,
            max_attempts=1,
            request_timeout_total=8.0,
        )

    async def subscribe_eventsub_webhook(
        self,
        *,
        sub_type: str,
        condition: Dict[str, str],
        webhook_url: str,
        secret: str,
        version: str = "1",
        oauth_token: Optional[str] = None,
    ) -> Dict:
        """Registriert eine Webhook-basierte EventSub Subscription."""
        payload = {
            "type": sub_type,
            "version": version,
            "condition": condition,
            "transport": {
                "method": "webhook",
                "callback": webhook_url,
                "secret": secret,
            },
        }
        return await self._post(
            "/eventsub/subscriptions",
            json=payload,
            oauth_token=oauth_token,
            max_attempts=1,
            request_timeout_total=10.0,
        )

    async def delete_eventsub_subscription(self, subscription_id: str, oauth_token: Optional[str] = None) -> bool:
        """Löscht eine EventSub Subscription per ID."""
        await self._ensure_token()
        self._ensure_session()
        assert self._session is not None

        token_override = (oauth_token or "").strip()
        if token_override.lower().startswith("oauth:"):
            token_override = token_override.split(":", 1)[1]
        if not token_override:
            token_override = self._token or ""

        url = f"{TWITCH_API_BASE}/eventsub/subscriptions"
        headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {token_override}"}
        try:
            async with self._session.delete(url, headers=headers, params={"id": subscription_id}) as r:
                if r.status == 204:
                    return True
                txt = await r.text()
                self._log.warning("DELETE /eventsub/subscriptions?id=%s: HTTP %s: %s", subscription_id, r.status, txt[:200])
                return False
        except Exception as exc:
            self._log.error("delete_eventsub_subscription(%s) fehlgeschlagen: %s", subscription_id, exc)
            return False

    async def list_eventsub_subscriptions(
        self,
        *,
        status: str = "enabled",
        oauth_token: Optional[str] = None,
    ) -> List[Dict]:
        """Listet aktive EventSub Subscriptions (paginiert)."""
        await self._ensure_token()
        self._ensure_session()
        assert self._session is not None

        token_override = (oauth_token or "").strip()
        if token_override.lower().startswith("oauth:"):
            token_override = token_override.split(":", 1)[1]
        if not token_override:
            token_override = self._token or ""

        headers = {"Client-ID": self.client_id, "Authorization": f"Bearer {token_override}"}
        url = f"{TWITCH_API_BASE}/eventsub/subscriptions"
        results: List[Dict] = []
        cursor: Optional[str] = None

        for _ in range(20):  # Max 20 Seiten (Schutz vor Endlosschleife)
            params: List[Tuple[str, str]] = []
            if status:
                params.append(("status", status))
            if cursor:
                params.append(("after", cursor))
            try:
                async with self._session.get(url, headers=headers, params=params or None) as r:
                    if r.status != 200:
                        txt = await r.text()
                        self._log.warning("GET /eventsub/subscriptions: HTTP %s: %s", r.status, txt[:200])
                        break
                    js = await r.json()
            except Exception as exc:
                self._log.error("list_eventsub_subscriptions fehlgeschlagen: %s", exc)
                break

            data = js.get("data") or []
            results.extend(data)
            pagination = js.get("pagination") or {}
            cursor = pagination.get("cursor")
            if not cursor:
                break

        return results
