import asyncio
import time
import logging
from typing import Dict, List, Optional, Tuple, Union

import aiohttp

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
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
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

    # ---- Core GET ----------------------------------------------------------
    async def _get(self, path: str, params: Optional[Union[Dict[str, str], List[Tuple[str, str]]]] = None) -> Dict:
        await self._ensure_token()
        self._ensure_session()
        assert self._session is not None
        url = f"{TWITCH_API_BASE}{path}"
        async with self._session.get(url, headers=self._headers(), params=params) as r:
            if r.status != 200:
                txt = await r.text()
                self._log.error("GET %s failed: HTTP %s: %s", path, r.status, txt[:300].replace("\n", " "))
                r.raise_for_status()
            return await r.json()

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
