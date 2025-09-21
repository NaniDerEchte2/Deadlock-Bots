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
    - Keine Secrets im Log
    - Timeouts + Backoff bei 5xx/429
    - Kategorien via /search/categories; Streams via /streams; Profile via /users
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
        self._log = logging.getLogger("TwitchDeadlock")

    # ---- Session lifecycle -------------------------------------------------
    def _ensure_session(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
            self._own_session = True

    async def aclose(self) -> None:
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        self._ensure_session()
        return self

    async def __aexit__(self, *exc):
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
        backoff = 1.0
        for _ in range(4):
            try:
                async with self._session.get(
                    f"{TWITCH_API_BASE}{path}", headers=self._headers(), params=params
                ) as r:
                    if r.status == 429:
                        await asyncio.sleep(min(10, backoff))
                        backoff *= 2
                        continue
                    r.raise_for_status()
                    return await r.json()
            except aiohttp.ClientResponseError as e:
                if e.status in (500, 502, 503, 504):
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        raise RuntimeError("Twitch API retries exhausted")

    # ---- Categories (Games) -----------------------------------------------
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
                out[u["login"].lower()] = u
        return out

    async def get_streams(
        self,
        *,
        user_logins: Optional[List[str]] = None,
        game_id: Optional[str] = None,
        language: Optional[str] = None,
        first: int = 100,
    ) -> List[Dict]:
        params: List[Tuple[str, str]] = []
        if user_logins:
            for u in user_logins[:100]:
                params.append(("user_login", u))
        if game_id:
            params.append(("game_id", game_id))
        if language:
            params.append(("language", language))
        params.append(("first", str(min(max(first, 1), 100))))
        js = await self._get("/streams", params=params)
        return js.get("data", [])
