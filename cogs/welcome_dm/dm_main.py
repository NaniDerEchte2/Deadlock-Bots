# cogs/welcome_dm/dm_main.py
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import discord
from discord.ext import commands

from service import db as service_db

from . import base as base_module
from .step_intro import IntroView  # Intro info/weiter Button (persistente Steuerung)
from .step_status import PlayerStatusView
from .step_steam_link import SteamLinkStepView, steam_link_dm_description
from .step_rules import RulesView
from .step_streamer import StreamerIntroView  # Optionaler Schritt


def _fallback_build_step_embed(title, desc, step, total, color=0x5865F2):
    footer = "Einführung • Deutsche Deadlock Community" if step is None else f"Frage {step} von {total} • Deutsche Deadlock Community"
    emb = discord.Embed(title=title, description=desc, color=color)
    emb.set_footer(text=footer)
    return emb


build_step_embed = getattr(base_module, "build_step_embed", _fallback_build_step_embed)
logger = getattr(base_module, "logger", logging.getLogger(__name__))

STATUS_NEED_BETA = getattr(base_module, "STATUS_NEED_BETA", "need_beta")
STATUS_NEW_PLAYER = getattr(base_module, "STATUS_NEW_PLAYER", "new_player")
STATUS_PLAYING = getattr(base_module, "STATUS_PLAYING", "already_playing")
STATUS_RETURNING = getattr(base_module, "STATUS_RETURNING", "returning")

DEFAULT_BETA_INVITE_CHANNEL_URL = "https://discord.com/channels/1289721245281292288/1428745737323155679"
DEFAULT_BETA_INVITE_SUPPORT_CONTACT = "@earlysalty"

BETA_INVITE_CHANNEL_URL = getattr(
    base_module,
    "BETA_INVITE_CHANNEL_URL",
    DEFAULT_BETA_INVITE_CHANNEL_URL,
)
BETA_INVITE_SUPPORT_CONTACT = getattr(
    base_module,
    "BETA_INVITE_SUPPORT_CONTACT",
    DEFAULT_BETA_INVITE_SUPPORT_CONTACT,
)

REQUIRED_WELCOME_ROLE_ID = 1304216250649415771


PERSISTENCE_NAMESPACE = "welcome_dm:persistent_views"

_VIEW_REGISTRY: Dict[str, Any] = {
    "intro": IntroView,
    "status": PlayerStatusView,
    "steam": SteamLinkStepView,
    "rules": RulesView,
}


class WelcomeDM(commands.Cog):
    """Welcome-Onboarding: verwaltet persistente Step-Views für den Kanal-Flow.
       Re-registriert laufende Views nach Neustarts automatisch."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- Intern ----------------

    def _view_key_for(self, view: discord.ui.View) -> Optional[str]:
        for key, cls in _VIEW_REGISTRY.items():
            try:
                if isinstance(view, cls):
                    return key
            except Exception:
                continue
        return None

    def _bind_view_persistence(
        self,
        message: discord.Message,
        view: discord.ui.View,
        *,
        target_user_id: Optional[int],
    ) -> None:
        key = self._view_key_for(view)
        if key is None:
            return

        payload: Dict[str, Any] = {
            "view": key,
            "user_id": int(target_user_id) if target_user_id is not None else None,
            "created_at": getattr(view, "created_at", datetime.now()).isoformat(),
        }

        if key == "steam":
            payload["show_next"] = bool(getattr(view, "show_next", True))

        try:
            encoded = json.dumps(payload)
        except Exception:
            logger.exception("Konnte Persistenz-Payload für View %s nicht serialisieren", key)
            return

        try:
            with service_db.get_conn() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO kv_store (ns, k, v) VALUES (?, ?, ?)",
                    (PERSISTENCE_NAMESPACE, str(message.id), encoded),
                )
        except Exception:
            logger.exception("Persistente View konnte nicht gespeichert werden (message_id=%s)", message.id)
        else:
            binder = getattr(view, "bind_persistence", None)
            if callable(binder):
                try:
                    binder(self, message.id)
                except Exception:
                    logger.exception("Konnte Persistenz-Bindung für View %s nicht setzen", key)

    def _unpersist_view(self, message_id: int) -> None:
        try:
            with service_db.get_conn() as conn:
                conn.execute(
                    "DELETE FROM kv_store WHERE ns = ? AND k = ?",
                    (PERSISTENCE_NAMESPACE, str(message_id)),
                )
        except Exception:
            logger.exception("Persistenz-Eintrag konnte nicht entfernt werden (message_id=%s)", message_id)

    def _restore_persistent_views(self) -> None:
        try:
            with service_db.get_conn() as conn:
                rows = conn.execute(
                    "SELECT k, v FROM kv_store WHERE ns = ?",
                    (PERSISTENCE_NAMESPACE,),
                ).fetchall()
        except Exception:
            logger.exception("Persistente Welcome-Views konnten nicht geladen werden")
            return

        restored = 0
        for row in rows:
            try:
                message_id = int(row["k"] if isinstance(row, dict) else row[0])
            except Exception:
                continue
            data_raw = row["v"] if isinstance(row, dict) else row[1]
            try:
                data = json.loads(data_raw)
            except Exception:
                logger.debug("Persistente View konnte nicht geparst werden (message_id=%s)", message_id, exc_info=True)
                self._unpersist_view(message_id)
                continue

            key = data.get("view")
            factory = _VIEW_REGISTRY.get(key)
            if not factory:
                logger.debug("Unbekannter View-Typ %s für message_id=%s", key, message_id)
                self._unpersist_view(message_id)
                continue

            kwargs: Dict[str, Any] = {}
            user_id = data.get("user_id")
            if user_id is not None:
                try:
                    kwargs["allowed_user_id"] = int(user_id)
                except Exception:
                    logger.debug("Konnte user_id %r nicht verarbeiten (message_id=%s)", user_id, message_id)
            created_at_raw = data.get("created_at")
            if created_at_raw:
                try:
                    kwargs["created_at"] = datetime.fromisoformat(created_at_raw)
                except Exception:
                    logger.debug("Konnte created_at %r nicht parsen (message_id=%s)", created_at_raw, message_id)
            if key == "steam" and "show_next" in data:
                kwargs["show_next"] = bool(data.get("show_next", True))

            try:
                view = factory(**kwargs)
            except Exception:
                logger.exception("Konnte View %s nicht instanziieren (message_id=%s)", key, message_id)
                self._unpersist_view(message_id)
                continue

            binder = getattr(view, "bind_persistence", None)
            if callable(binder):
                try:
                    binder(self, message_id)
                except Exception:
                    logger.debug("Bindung für persistente View %s fehlgeschlagen (message_id=%s)", key, message_id, exc_info=True)

            try:
                self.bot.add_view(view, message_id=message_id)
            except Exception:
                logger.exception("Persistente View konnte nicht registriert werden (message_id=%s)", message_id)
                self._unpersist_view(message_id)
                continue

            restored += 1

        if restored:
            logger.info("%s WelcomeDM-Views nach Neustart reaktiviert", restored)
        else:
            logger.debug("Keine WelcomeDM-Views zur Reaktivierung gefunden")

    async def cog_load(self):
        self._restore_persistent_views()
        logger.info("WelcomeDM geladen (persistente Step-Views aktiv).")

    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System bereit")

    async def _send_step_embed_channel(
        self,
        channel: discord.abc.Messageable,
        *,
        title: str,
        desc: str,
        step: int | None,
        total: int,
        view: discord.ui.View,
        color: int = 0x5865F2,
    ) -> bool:
        """Sendet einen Step in einen (Thread-)Kanal, wartet auf Abschluss und räumt auf."""
        emb = build_step_embed(title, desc, step, total, color=color)
        msg = await channel.send(embed=emb, view=view)
        if hasattr(view, "bound_message"):
            view.bound_message = msg
        allowed_user = getattr(view, "allowed_user_id", None)
        self._bind_view_persistence(msg, view, target_user_id=allowed_user)
        try:
            await view.wait()
        finally:
            self._unpersist_view(msg.id)
            try:
                await msg.delete()
            except Exception as exc:
                logger.debug("_send_step_embed_channel: Nachricht konnte nicht gelöscht werden: %s", exc)
        return bool(getattr(view, "proceed", False))

    @staticmethod
    def _beta_invite_message() -> discord.Embed:
        description = (
            f"Schau in {BETA_INVITE_CHANNEL_URL} vorbei – dort bekommst du einen Beta-Invite mit `/betainvite`.\n"
            f"Sollten Probleme auftreten, ping bitte {BETA_INVITE_SUPPORT_CONTACT}."
        )
        embed = discord.Embed(
            title="🎟️ Beta-Invite benötigt?",
            description=description,
            color=discord.Color.blue() # Using a standard blue color for information
        )
        return embed

    async def run_flow_in_channel(self, channel: discord.abc.Messageable, member: discord.Member) -> bool:
        """Gleicher Flow im (privaten) Thread/Channel. Zählung 1/5–5/5; Intro ohne Zählung."""
        try:
            # Intro (ohne Zählung)
            intro_desc = (
                "👋 **Willkommen!** Ich helfe dir, dein Erlebnis hier optimal einzustellen. "
                "Die nächsten 2–3 Minuten genügen."
            )
            total_steps = 5
            ok = await self._send_step_embed_channel(
                channel,
                title="Willkommen 💙",
                desc=intro_desc,
                step=None,
                total=3,
                view=IntroView(allowed_user_id=member.id),
                color=0x00AEEF,
            )
            if not ok:
                return False

            # 1/3 Status
            status_view = PlayerStatusView(allowed_user_id=member.id)
            ok = await self._send_step_embed_channel(
                channel,
                title="Schritt 3/5 · Dein Status",
                desc="Sag kurz, wo du stehst – dann passen wir alles besser an.",
                step=3,
                total=total_steps,
                view=status_view,
                color=0x95A5A6,
            )
            if not ok:
                return False
            status_choice = status_view.choice or STATUS_PLAYING

            if status_choice == STATUS_NEED_BETA:
                try:
                    await channel.send(embed=self._beta_invite_message())
                except Exception as exc:
                    logger.debug("Beta-Invite Hinweis im Channel konnte nicht gesendet werden: %s", exc)
                return True

            # 4/5 Steam
            q2_desc = steam_link_dm_description()
            ok = await self._send_step_embed_channel(
                channel,
                title="Schritt 4/5 · Steam verknüpfen (skippbar)",
                desc=q2_desc,
                step=2,
                total=3,
                view=SteamLinkStepView(allowed_user_id=member.id),
                color=0x5865F2,
            )
            if not ok:
                return False

            # Optional: Streamer
            try:
                embed = StreamerIntroView.build_embed(member)
                view = StreamerIntroView()
                msg = await channel.send(embed=embed, view=view)
                await view.wait()
                try:
                    await msg.delete()
                except Exception as exc:
                    logger.debug("StreamerIntro Channel-Message nicht gelöscht: %s", exc)
            except Exception:
                logger.debug("StreamerIntro Schritt (Thread) übersprungen.", exc_info=True)

            # 5/5 Regeln
            q3_desc = (
                "📜 **Regelwerk**\n"
                "✔ Respektvoller Umgang, keine Beleidigungen/Hassrede\n"
                "✔ Keine NSFW/Explizites, keine Leaks fremder Daten\n"
                "✔ Kein Spam/unnötige Pings, keine Fremdwerbung/Schadsoftware\n"
                "👉 Universalregel: **Sei kein Arschloch.**"
            )
            ok = await self._send_step_embed_channel(
                channel,
                title="Schritt 5/5 · Regeln bestätigen",
                desc=q3_desc,
                step=3,
                total=3,
                view=RulesView(allowed_user_id=member.id),
                color=0xE67E22,
            )
            if not ok:
                return False

            # Abschluss-Text
            closing_embeds: list[discord.Embed] = []
            if status_choice == STATUS_NEW_PLAYER:
                embed = discord.Embed(
                    title="✨ Neu dabei?",
                    description="Stell Fragen – wir helfen gern. Kleine Einführung? Ping **@earlysalty** oder schreibe in **#allgemein**.",
                    color=discord.Color.gold()
                )
                closing_embeds.append(embed)
            if status_choice == STATUS_NEED_BETA:
                # _beta_invite_message already returns an embed
                closing_embeds.append(self._beta_invite_message())
            if status_choice == STATUS_RETURNING:
                embed = discord.Embed(
                    title="🔁 Willkommen zurück!",
                    description="Schau für Runden in LFG/Voice vorbei – viel Spaß!",
                    color=discord.Color.green()
                )
                closing_embeds.append(embed)
            if status_choice == STATUS_PLAYING:
                embed = discord.Embed(
                    title="✅ Viel Spaß!",
                    description="Check **Guides** & **Ankündigungen** – und ping uns, wenn du was brauchst.",
                    color=discord.Color.green()
                )
                closing_embeds.append(embed)

            if closing_embeds:
                try:
                    for embed_item in closing_embeds:
                        await channel.send(embed=embed_item)
                except Exception as exc:
                    logger.debug("Abschlussnachricht im Channel konnte nicht gesendet werden: %s", exc)

            return True

        except Exception as e:
            logger.error(f"run_flow_in_channel Fehler: {e}", exc_info=True)
            return False

    # ---------------- Events ----------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        logger.info(
            "WelcomeDM: Automatische Willkommens-DMs sind deaktiviert. Onboarding läuft über den Regelkanal. (%s)",
            member.id,
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeDM(bot))
