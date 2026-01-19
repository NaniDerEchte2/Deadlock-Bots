import asyncio
import logging
import os
from typing import Any, Dict, Optional, Tuple

from discord.ext import commands

try:
    from openai import OpenAI
except Exception:  # pragma: no cover - optional dependency
    OpenAI = None  # type: ignore[assignment]

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:  # pragma: no cover - optional dependency
    genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

DEFAULT_OPENAI_MODEL = os.getenv("AI_OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_GEMINI_MODEL = os.getenv("AI_GEMINI_MODEL", "gemini-2.0-flash")
DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "800") or "800")


class AIConnector(commands.Cog):
    """Zentrale AI-Verbindungsstelle (Gemini & OpenAI) für alle Cogs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._openai_client: Optional[OpenAI] = None
        self._openai_init_failed = False
        self._gemini_client: Optional[object] = None
        self._gemini_init_failed = False

    # ---------- Clients ----------
    def _get_openai_client(self) -> Optional[OpenAI]:
        if self._openai_init_failed:
            return None
        if self._openai_client:
            return self._openai_client

        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("DEADLOCK_OPENAI_KEY")
        if not api_key:
            self._openai_init_failed = True
            return None
        if OpenAI is None:
            self._openai_init_failed = True
            log.debug("OpenAI SDK nicht verfügbar – OpenAI deaktiviert.")
            return None
        try:
            self._openai_client = OpenAI(api_key=api_key)
        except Exception as exc:
            self._openai_init_failed = True
            log.exception("OpenAI Client konnte nicht initialisiert werden: %s", exc)
            return None
        return self._openai_client

    def _get_gemini_client(self) -> Optional[object]:
        if self._gemini_init_failed:
            return None
        if self._gemini_client:
            return self._gemini_client

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            self._gemini_init_failed = True
            return None
        if genai is None or genai_types is None:
            self._gemini_init_failed = True
            log.debug("google-genai Paket fehlt – Gemini deaktiviert.")
            return None

        try:
            self._gemini_client = genai.Client(api_key=api_key)
        except Exception as exc:
            self._gemini_init_failed = True
            log.exception("Gemini Client konnte nicht initialisiert werden: %s", exc)
            return None
        return self._gemini_client

    # ---------- Public API ----------
    async def generate_text(
        self,
        *,
        provider: str,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        temperature: float = 0.6,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """
        Einfache Text-Generierung über Gemini oder OpenAI Responses API.
        Returns (text|None, meta)
        """
        provider = provider.lower()
        meta: Dict[str, Any] = {
            "provider": provider,
            "model": model,
        }
        mot = max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS

        if provider == "gemini":
            text = await self._generate_gemini(
                prompt=prompt,
                system_prompt=system_prompt,
                model=model or DEFAULT_GEMINI_MODEL,
                max_output_tokens=mot,
                temperature=temperature,
            )
            meta["model"] = model or DEFAULT_GEMINI_MODEL
            if text is None:
                meta["error"] = "gemini_unavailable"
            return text, meta

        if provider == "openai":
            text, usage = await self._generate_openai(
                prompt=prompt,
                system_prompt=system_prompt,
                model=model or DEFAULT_OPENAI_MODEL,
                max_output_tokens=mot,
                temperature=temperature,
            )
            meta["model"] = model or DEFAULT_OPENAI_MODEL
            if usage:
                meta["usage"] = usage
            if text is None:
                meta["error"] = "openai_unavailable"
            return text, meta

        meta["error"] = "unknown_provider"
        return None, meta

    # ---------- Provider Implementierungen ----------
    async def _generate_gemini(
        self,
        *,
        prompt: str,
        system_prompt: Optional[str],
        model: str,
        max_output_tokens: int,
        temperature: float,
    ) -> Optional[str]:
        client = self._get_gemini_client()
        if not client or genai_types is None:
            return None

        contents = prompt
        if system_prompt:
            contents = f"{system_prompt}\n\n{prompt}"

        def _call_model() -> Optional[str]:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                    ),
                )
                text = getattr(response, "text", "") or ""
                return text.strip() if text else None
            except Exception as exc:
                log.debug("Gemini Request fehlgeschlagen: %s", exc)
                return None

        return await asyncio.to_thread(_call_model)

    async def _generate_openai(
        self,
        *,
        prompt: str,
        system_prompt: Optional[str],
        model: str,
        max_output_tokens: int,
        temperature: float,
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        client = self._get_openai_client()
        if not client:
            return None, None

        def _call_model():
            try:
                return client.responses.create(
                    model=model,
                    input=prompt,
                    instructions=system_prompt,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                )
            except TypeError:
                return client.responses.create(
                    model=model,
                    input=prompt,
                    instructions=system_prompt,
                    max_tokens=max_output_tokens,
                    temperature=temperature,
                )
            except Exception as exc:
                log.debug("OpenAI Request fehlgeschlagen: %s", exc)
                return None

        response = await asyncio.to_thread(_call_model)
        if response is None:
            return None, None

        # Extract text
        text = ""
        try:
            output_text = getattr(response, "output_text", None)
            if not output_text and isinstance(response, dict):
                output_text = response.get("output_text")
            if output_text:
                text = str(output_text).strip()
            else:
                out = getattr(response, "output", None) or getattr(response, "outputs", None)
                if out is None and isinstance(response, dict):
                    out = response.get("output") or response.get("outputs")
                fragments = []
                for item in out or []:
                    item_type = getattr(item, "type", None)
                    if item_type is None and isinstance(item, dict):
                        item_type = item.get("type")
                    if item_type != "message":
                        continue
                    item_content = getattr(item, "content", None)
                    if item_content is None and isinstance(item, dict):
                        item_content = item.get("content")
                    for part in item_content or []:
                        txt = getattr(part, "text", None)
                        if txt is None and isinstance(part, dict):
                            txt = part.get("text")
                        if txt:
                            fragments.append(str(txt))
                text = "".join(fragments).strip()
        except Exception:
            log.exception("Antwort-Parsing fehlgeschlagen")
            text = ""

        usage_raw = getattr(response, "usage", None)
        if usage_raw is None and isinstance(response, dict):
            usage_raw = response.get("usage")
        usage = None
        if usage_raw:
            usage = {
                "input_tokens": getattr(usage_raw, "input_tokens", None),
                "output_tokens": getattr(usage_raw, "output_tokens", None),
                "total_tokens": getattr(usage_raw, "total_tokens", None),
            }
        return text or None, usage

    # ---------- Commands ----------
    @commands.command(name="aiob")
    @commands.has_permissions(administrator=True)
    async def ai_onboarding_test(self, ctx: commands.Context):
        """
        Schickt dir den AI-Onboarding Start-Button in die DMs.
        Nutzt AIOnboarding Cog, falls geladen.
        """
        ai_ob = getattr(self.bot, "get_cog", lambda name: None)("AIOnboarding")
        if not ai_ob or not hasattr(ai_ob, "start_in_channel"):
            await ctx.reply(
                "AIOnboarding ist nicht geladen. Bitte Cog laden und erneut versuchen.",
                mention_author=False,
            )
            return

        try:
            dm = ctx.author.dm_channel or await ctx.author.create_dm()
        except Exception as exc:
            log.warning("Konnte DM fuer aiob nicht oeffnen: %s", exc)
            await ctx.reply("Konnte deine DMs nicht oeffnen.", mention_author=False)
            return

        ok = await ai_ob.start_in_channel(dm, ctx.author)  # type: ignore[attr-defined]
        if ok:
            await ctx.reply(
                "AI-Onboarding Test wurde an deine DMs geschickt. Keine automatischen DMs aktiv.",
                mention_author=False,
            )
        else:
            await ctx.reply("Konnte den AI-Onboarding Test nicht starten.", mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIConnector(bot))
