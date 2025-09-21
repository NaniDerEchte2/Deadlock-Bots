# =========================================
# cogs/twitch_deadlock/twitch_api.py
# =========================================
import asyncio
import time
from typing import Dict, List, Optional, Tuple

import aiohttp

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

class TwitchAPI:
    """Thin async wrapper around Twitch Helix using app access tokens.

    Security:
      - No secrets logged (CWE-522)
      - Timeouts + backoff to mitigate resource exhaustion (CWE-770/CWE-400)
    """

    def __init__(self, client_id: str, client_secret: str, session: Optional[aiohttp.ClientSession] = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self._session = session
        self._own_session = False
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._lock = asyncio.Lock()
        self._game_cache: Dict[str, str] = {}  # name -> id

    async def __aenter__(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self

    async def __aexit__(self, *exc):
        if self._own_session and self._session:
            await self._session.close()

    # ------------------------
    # OAuth app access token
    # ------------------------
    async def _ensure_token(self):
        async with self._lock:
            if self._token and time.time() < self._token_expiry - 60:
                return
            assert self._session is not None
            data = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            }
            async with self._session.post(TWITCH_TOKEN_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)) as r:
                r.raise_for_status()
                js = await r.json()
                self._token = js.get("access_token")
                expires = js.get("expires_in", 3600)
                self._token_expiry = time.time() + float(expires)

    def _headers(self) -> Dict[str, str]:
        return {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self._token}",
        }

    # ------------------------
    # Core requests
    # ------------------------
    async def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict:
        await self._ensure_token()
        assert self._session is not None
        # Retry/backoff
        backoff = 1.0
        for attempt in range(4):
            try:
                async with self._session.get(
                    f"{TWITCH_API_BASE}{path}",
                    headers=self._headers(),
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 429:
                        # Basic rate-limit backoff
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

    # ------------------------
    # Public endpoints used
    # ------------------------
    async def get_games_by_name(self, names: List[str]) -> Dict[str, str]:
        """Return mapping name_lower -> game_id."""
        if not names:
            return {}
        # resolve via cache first
        remaining = [n for n in names if n.lower() not in self._game_cache]
        out = {n.lower(): self._game_cache[n.lower()] for n in names if n.lower() in self._game_cache}
        # Twitch allows up to 100 names
        batch = []
        for n in remaining:
            batch.append(n)
            if len(batch) == 100:
                js = await self._get("/games", params={"name": batch[0]}) if len(batch) == 1 else await self._get("/games", params=[("name", b) for b in batch])
                for g in js.get("data", []):
                    self._game_cache[g["name"].lower()] = g["id"]
                    out[g["name"].lower()] = g["id"]
                batch = []
        if batch:
            js = await self._get("/games", params=[("name", b) for b in batch])
            for g in js.get("data", []):
                self._game_cache[g["name"].lower()] = g["id"]
                out[g["name"].lower()] = g["id"]
        return out

    async def get_game_id(self, name: str) -> Optional[str]:
        name_l = name.lower()
        if name_l in self._game_cache:
            return self._game_cache[name_l]
        js = await self._get("/games", params={"name": name})
        data = js.get("data", [])
        if data:
            gid = data[0]["id"]
            self._game_cache[name_l] = gid
            return gid
        return None

    async def get_users(self, logins: List[str]) -> Dict[str, Dict]:
        """Return mapping login_lower -> user object (id, login, display_name, description, etc.)."""
        out: Dict[str, Dict] = {}
        if not logins:
            return out
        # 100 per request
        for i in range(0, len(logins), 100):
            chunk = logins[i:i+100]
            params: List[Tuple[str, str]] = [("login", x) for x in chunk]
            js = await self._get("/users", params=params)
            for u in js.get("data", []):
                out[u["login"].lower()] = u
        return out

    async def get_streams(self, *, user_logins: Optional[List[str]] = None, game_id: Optional[str] = None, language: Optional[str] = None, first: int = 100) -> List[Dict]:
        """Get active streams; filters are combined (AND)."""
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

