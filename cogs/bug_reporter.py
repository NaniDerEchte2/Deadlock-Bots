from __future__ import annotations

import asyncio
import logging
import re
import shlex
from textwrap import dedent

import discord
from discord import app_commands
from discord.ext import commands

from cogs import privacy_core as privacy
from service import db, issue_reports

log = logging.getLogger(__name__)

PRIMARY_MODEL = "gpt-4o-mini"
FALLBACK_MODEL = "gemini-2.0-flash"
MAX_OUTPUT_TOKENS = 700
LOCAL_CODEX_CMD = "powershell -NoProfile -ExecutionPolicy Bypass -File codex.ps1"
REMOTE_FALLBACK_ENABLED = False  # Lokal only; kein Remote-Fallback
CODEX_TIMEOUT_SEC = 300
CODEX_ALLOWED_CATEGORIES = {
    "steam_verification",
    "beta_invite",
    "bot_command",
    "build_publishing",
    "ai_features",
}
ROLE_BLACKLIST = {
    1304169657124782100,
    1337518124647579661,
    1355081243519225868,
    1411000883155832852,
    1401891955931222110,
    1414182925460836443,
    1304416311383818240,
}
ROLE_EXCEPTION_ID = 1313624729466441769
CODEX_BLOCKED_ACTIONS = (
    "Keine manuellen Rollenvergaben, außer den vorgesehenen Auto-Rollen-Flows "
    "(z. B. Verified durch Steam-Link, Patchnotes/LFG/CustomGames Ping per Bot-Logik). "
    "Keine Mod-/Admin-Rechte setzen. Keine User-Bans/Unbans durchführen."
)
TICKET_CHANNEL_ID = 1475218607213514926
KV_NS = "bug_reporter:panel"

SYSTEM_PROMPT = dedent(
    """
    Du bist Codex, der interne Auto-Fixer der Deutschen Deadlock Community.
    - Antworte immer auf Deutsch.
    - Liefere zuerst eine kurze, freundliche Antwort für den meldenden User.
    - Danach stichpunktartige Schritte, wie das Problem diagnostiziert/behoben wird.
    - Sei konkret (Dateien, Logs, Befehle). Keine Floskeln, keine erfundenen Fakten.
    - Wenn Infos fehlen, stelle maximal drei präzise Rückfragen.
    - Sende erst eine Antwort, wenn du die Ursache verstanden und (soweit möglich) einen Fix angewendet hast oder einen klaren Workaround nennen kannst.
    - Wenn der Fix ausgeführt wurde, erwähne kurz die konkrete Aktion (z. B. Neustart, Reload, Konfig-Anpassung, Log-Hinweis).
    - Verboten: {blocked_actions}
    - Handle nur Tickets folgender Kategorien: {allowed_categories}. Bei anderen Kategorien antworte mit: "Kein Codex-Handling (Kategorie außerhalb Scope)."
    - Erlaubte Admin-Aktionen (nur bot/serverseitig): Bot-Neustart, Reload eines Cogs, Reload aller Cogs. Wenn du eine Aktion ausführst, benenne sie knapp.
    Format:
    Antwort: <Text für User, max 6 Sätze>
    Maßnahmen:
    - Schritt 1
    - Schritt 2
    Status: behoben | workaround | braucht mehr infos | weiterleitung nötig
    Rollen-Policy:
    - Niemals folgende Rollen anfassen: {role_blacklist}
    - Einzige erlaubte Ausnahme: Rolle {role_exception} darf nur gesetzt werden, wenn der Nutzer in der DB als verifiziert (steam_links.verified=1 UND is_steam_friend=1) geführt wird.
    """
).strip()


CATEGORY_CHOICES: tuple[tuple[str, str], ...] = (
    ("Steam-Verifizierung", "steam_verification"),
    ("Beta-Invite / Zugang", "beta_invite"),
    ("Bot-Command/Feature", "bot_command"),
    ("Build-Publishing", "build_publishing"),
    ("AI-Features (FAQ/Onboarding)", "ai_features"),
    ("User-Management/Beschwerde", "user_management"),
    ("Sonstiges", "other"),
)


class BugReportModal(discord.ui.Modal):
    def __init__(self, cog: BugReporter, *, category: str | None) -> None:
        super().__init__(title="Bug oder Problem melden", timeout=None)
        self.cog = cog
        self.category = category

        self.title_input = discord.ui.TextInput(
            label="Kurzbeschreibung",
            placeholder="z. B. Voice-Kanal bricht ab, Statistiken falsch, Command error ...",
            required=False,
            max_length=150,
        )
        self.details_input = discord.ui.TextInput(
            label="Was genau ist das Problem? (bitte präzise)",
            style=discord.TextStyle.long,
            placeholder="Konkrete Fehlerbeschreibung, Repro-Schritte, erwartetes Ergebnis, Fehlermeldung/ID, betroffene Befehle/Module.",
            required=True,
            max_length=1800,
        )

        self.add_item(self.title_input)
        self.add_item(self.details_input)

    async def on_submit(
        self, interaction: discord.Interaction
    ) -> None:  # pragma: no cover - runtime
        title = str(self.title_input.value).strip()
        details = str(self.details_input.value).strip()
        await self.cog.handle_submission(
            interaction,
            title=title,
            details=details,
            category=self.category,
        )


class TicketButtonView(discord.ui.View):
    def __init__(self, cog: BugReporter, *, persistent: bool = False):
        super().__init__(timeout=None if persistent else 900)
        self.cog = cog

    @discord.ui.button(
        label="Ticket erstellen",
        style=discord.ButtonStyle.primary,
        custom_id="bugreporter:create",
    )
    async def create_ticket(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ):  # pragma: no cover - runtime
        await interaction.response.send_modal(BugReportModal(self.cog, category=None))


class BugReporter(commands.Cog):
    """Einfaches Ticket-/Bug-Interface mit automatischer Codex-Antwort."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.panel_message_id: int | None = None
        self.persistent_view: TicketButtonView | None = None

    @app_commands.command(
        name="ticket",
        description="Bug oder Problem melden – Codex versucht sofort eine Lösung.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        kategorie="Was für ein Ticket eröffnest du?",
    )
    @app_commands.choices(
        kategorie=[
            app_commands.Choice(name=label, value=value) for label, value in CATEGORY_CHOICES
        ]
    )
    async def ticket(
        self, interaction: discord.Interaction, kategorie: app_commands.Choice[str]
    ) -> None:
        """Öffnet das Meldeformular."""
        await interaction.response.send_modal(BugReportModal(self, category=kategorie.value))

    @commands.command(name="ticket")
    async def ticket_prefix(self, ctx: commands.Context) -> None:
        """Fallback für Prefix-User."""
        try:
            await ctx.reply(
                "Nutze bitte den Slash-Befehl /ticket, um einen Bug oder ein Problem zu melden.",
                mention_author=False,
            )
        except Exception:
            log.debug("Prefix-Antwort für /ticket konnte nicht gesendet werden", exc_info=True)

    async def handle_submission(
        self,
        interaction: discord.Interaction,
        *,
        title: str,
        details: str,
        category: str,
    ) -> None:
        if not details:
            await interaction.response.send_message(
                "Bitte beschreibe das Problem kurz, damit Codex starten kann.",
                ephemeral=True,
            )
            return

        if not category:
            category = self._infer_category(details)

        user_id = getattr(interaction.user, "id", None)
        if user_id and privacy.is_opted_out(user_id):
            await interaction.response.send_message(
                "Du hast Datenspeicherung deaktiviert. Ticket wurde nicht angelegt.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        report_id = await issue_reports.create_report(
            user_id=user_id,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            message_id=getattr(interaction.message, "id", None),
            category=category,
            title=title or None,
            description=details,
            status="processing",
        )

        if not report_id:
            await interaction.followup.send(
                "Ticket konnte nicht gespeichert werden. Bitte versuche es später erneut.",
                ephemeral=True,
            )
            return

        # Thread im Ticket-Channel erstellen
        thread: discord.Thread | None = None
        try:
            parent = self.bot.get_channel(TICKET_CHANNEL_ID) or await self.bot.fetch_channel(
                TICKET_CHANNEL_ID
            )
            if isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
                name = f"ticket-{report_id}-{interaction.user.display_name[:12]}"
                thread = await parent.create_thread(
                    name=name,
                    type=discord.ChannelType.private_thread
                    if hasattr(discord.ChannelType, "private_thread")
                    else discord.ChannelType.public_thread,
                    invitable=False,
                    reason=f"Ticket #{report_id} von {interaction.user}",
                )
                try:
                    await thread.add_user(interaction.user)
                except Exception:
                    pass
                await thread.send(
                    f"<@{interaction.user.id}> Ticket eröffnet.\n"
                    f"**Titel:** {title or 'Problem'}\n"
                    f"**Kategorie (auto):** {category}"
                )
        except Exception as exc:
            log.warning("Ticket-Thread konnte nicht erstellt werden: %s", exc)

        ack = (
            f"Ticket #{report_id} aufgenommen. "
            f"{'Codex arbeitet automatisch an einer Antwort.' if self._category_allows_codex(category) else 'Diese Kategorie wird manuell geprüft.'}"
        )
        await interaction.followup.send(ack, ephemeral=True)

        asyncio.create_task(
            self._process_report(
                interaction=interaction,
                report_id=report_id,
                title=title or "Problem",
                details=details,
                category=category,
            )
        )

    async def cog_load(self):
        # Registriere persistente View, damit Buttons nach Restart funktionieren.
        if self.persistent_view is None:
            self.persistent_view = TicketButtonView(self, persistent=True)
        self.bot.add_view(self.persistent_view)
        await self._ensure_panel()
        # async Healthcheck für lokalen Codex (non-blocking, 10s Timeout)
        asyncio.create_task(self._codex_healthcheck())

    async def _ensure_panel(self) -> None:
        """Stellt sicher, dass der Ticket-Button im Ziel-Channel liegt."""
        channel = self.bot.get_channel(TICKET_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(TICKET_CHANNEL_ID)
            except Exception as exc:
                log.warning("Ticket-Channel %s nicht erreichbar: %s", TICKET_CHANNEL_ID, exc)
                return

        stored_id = db.get_kv(KV_NS, "panel_msg")
        if stored_id:
            try:
                msg = await channel.fetch_message(int(stored_id))
                view = self.persistent_view or TicketButtonView(self, persistent=True)
                await msg.edit(view=view)
                self.panel_message_id = msg.id
                return
            except Exception:
                log.info("Panel-Nachricht nicht mehr vorhanden, erstelle neu.")

        embed = discord.Embed(
            title="Eröffne ein Ticket",
            description=(
                "Klicke auf **Ticket erstellen** und beschreibe dein Problem. "
                "Codex versucht es automatisch zu lösen (Bot/Server-Themen)."
            ),
            colour=discord.Colour.blurple(),
        )
        view = TicketButtonView(self, persistent=True)
        try:
            msg = await channel.send(embed=embed, view=view)
            self.bot.add_view(view, message_id=msg.id)
            db.set_kv(KV_NS, "panel_msg", str(msg.id))
            self.panel_message_id = msg.id
        except Exception as exc:
            log.warning("Ticket-Panel konnte nicht gesendet werden: %s", exc)

    def _category_allows_codex(self, category: str | None) -> bool:
        return (category or "").lower() in CODEX_ALLOWED_CATEGORIES

    def _infer_category(self, text: str) -> str:
        low = text.lower()
        if any(k in low for k in ("steam", "verifiz", "verify")):
            return "steam_verification"
        if any(k in low for k in ("beta", "invite", "zugang")):
            return "beta_invite"
        if any(k in low for k in ("build", "publish", "mirror")):
            return "build_publishing"
        if any(k in low for k in ("faq", "ai", "onboard", "ki")):
            return "ai_features"
        if any(k in low for k in ("command", "befehl", "bot", "error", "traceback")):
            return "bot_command"
        if any(k in low for k in ("ban", "kick", "mute", "user", "mod", "admin", "rolle")):
            return "user_management"
        return "other"

    def _is_verified_in_db(self, user_id: int | None) -> bool:
        """Prüft steam_links auf verified=1 und is_steam_friend=1 für den Discord-User."""
        if not user_id:
            return False
        try:
            from service import db

            row = db.query_one(
                """
                SELECT 1
                FROM steam_links
                WHERE user_id=? AND verified=1 AND is_steam_friend=1
                LIMIT 1
                """,
                (int(user_id),),
            )
            return bool(row)
        except Exception:
            log.debug("DB-Check für verified user_id=%s fehlgeschlagen", user_id, exc_info=True)
            return False

    def _detect_actions(self, details: str) -> list[dict[str, str]]:
        """Erkennt gewünschte Admin-Aktionen im Freitext."""
        actions: list[dict[str, str]] = []
        low = details.lower()
        if "restart bot" in low or "bot neu starten" in low or "bot neustart" in low:
            actions.append({"type": "restart_bot"})
        if "reload all" in low or "alle cogs neu laden" in low or "reloadall" in low:
            actions.append({"type": "reload_all"})
        # reload cog pattern
        for name in re.findall(r"reload\s+([\w\.]+)", low):
            actions.append({"type": "reload_cog", "target": name})
        return actions

    async def _run_actions(self, actions: list[dict[str, str]]) -> list[str]:
        """Führt erkannte Admin-Aktionen aus und liefert Ergebnis-Strings."""
        results: list[str] = []
        for action in actions:
            t = action.get("type")
            if t == "restart_bot":
                lifecycle = getattr(self.bot, "lifecycle", None)
                if lifecycle:
                    scheduled = await lifecycle.request_restart(reason="bug_reporter:auto")
                    results.append(
                        "Bot-Reboot angefordert"
                        if scheduled
                        else "Reboot bereits geplant/fehlgeschlagen"
                    )
                else:
                    results.append("Reboot nicht verfügbar (kein Lifecycle)")
            elif t == "reload_all":
                try:
                    ok, summary = await self.bot.reload_all_cogs_with_discovery()
                    if ok:
                        results.append("Alle Cogs neu geladen")
                    else:
                        results.append(f"Reload-All fehlgeschlagen: {summary}")
                except Exception as exc:
                    results.append(f"Reload-All Fehler: {exc}")
            elif t == "reload_cog":
                target = action.get("target") or ""
                resolved, suggestions = self.bot.resolve_cog_identifier(target)
                if not resolved:
                    if suggestions:
                        results.append(
                            f"Reload {target} unklar (Vorschläge: {', '.join(suggestions)})"
                        )
                    else:
                        results.append(f"Reload {target} nicht gefunden")
                    continue
                try:
                    ok, msg = await self.bot.reload_cog(resolved)
                    results.append(msg if ok else f"Reload {resolved} fehlgeschlagen")
                except Exception as exc:
                    results.append(f"Reload {resolved} Fehler: {exc}")
        return results

    async def _codex_healthcheck(self) -> None:
        """Kurzer Self-Test des lokalen Codex (nicht blockierend)."""
        try:
            text, err = await asyncio.wait_for(
                self._run_local_codex("healthcheck: Bitte antworte mit OK"),
                timeout=10,
            )
            if err:
                log.warning("Codex Healthcheck fehlgeschlagen: %s", err)
            elif not text or "ok" not in text.lower():
                log.warning("Codex Healthcheck unerwartete Antwort: %r", text)
            else:
                log.info("Codex Healthcheck erfolgreich.")
        except TimeoutError:
            log.warning("Codex Healthcheck Timeout (10s)")
        except Exception as exc:
            log.warning("Codex Healthcheck Fehler: %s", exc)

    def _compose_prompt(
        self,
        *,
        title: str,
        details: str,
        category: str,
        user: discord.abc.User | None,
    ) -> str:
        user_line = f"User: {getattr(user, 'display_name', getattr(user, 'name', 'Unbekannt'))}"
        user_id_line = f"User ID: {getattr(user, 'id', 'unbekannt')}"
        cat_line = f"Kategorie: {category}"
        return dedent(
            f"""
            Kontext:
            - {user_line}
            - {user_id_line}
            - {cat_line}
            - Verified in DB: {self._is_verified_in_db(getattr(user, "id", None))}
            - Logs/Code: Bitte selbst im Ordner ./logs nach relevanten Einträgen schauen und bei Bedarf die Codebase durchsuchen.

            Meldung:
            - Titel: {title or "Problem"}
            - Beschreibung: {details}

            Erstelle sofort eine hilfreiche Antwort und Schritte wie oben beschrieben.
            Verbotene Aktionen: {CODEX_BLOCKED_ACTIONS}
            Nur bearbeiten, wenn Kategorie in {", ".join(sorted(CODEX_ALLOWED_CATEGORIES))}; sonst kurz melden, dass der Fall manuell geprüft wird.
            """
        ).strip()

    def _system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(
            blocked_actions=CODEX_BLOCKED_ACTIONS,
            allowed_categories=", ".join(sorted(CODEX_ALLOWED_CATEGORIES)),
            role_blacklist=", ".join(str(rid) for rid in sorted(ROLE_BLACKLIST)),
            role_exception=str(ROLE_EXCEPTION_ID),
        )

    async def _process_report(
        self,
        *,
        interaction: discord.Interaction,
        report_id: int,
        title: str,
        details: str,
        category: str,
    ) -> None:
        ai = getattr(self.bot, "get_cog", lambda name: None)("AIConnector")
        prompt = self._compose_prompt(
            title=title,
            details=details,
            category=category,
            user=interaction.user,
        )

        response_text: str | None = None
        meta: dict[str, str | int | None] = {}
        action_results: list[str] = []

        # Kategorie außerhalb Codex-Scope -> sofort weiterleiten
        if not self._category_allows_codex(category):
            response_text = (
                "Dieses Ticket fällt nicht in den automatischen Codex-Bereich. "
                "Das Team schaut manuell drauf."
            )
            meta["model"] = "no-codex (handoff)"
            status = "handoff"
            await issue_reports.update_status(
                report_id,
                status=status,
                ai_response=response_text,
                ai_model=meta.get("model"),
                ai_error=None,
            )
            await self._send_result(
                interaction=interaction,
                report_id=report_id,
                title=title,
                content=response_text,
                status=status,
                model=str(meta.get("model") or "n/a"),
                actions=[],
            )
            return

        sys_prompt = self._system_prompt()

        # Automatische Admin-Aktionen (nur für Bot/Server-Themen)
        actions = self._detect_actions(details)
        if actions:
            action_results = await self._run_actions(actions)

        # Nur lokaler Codex (kein Remote-Fallback)
        response_text, local_err = await self._run_local_codex(prompt)
        if response_text:
            meta["model"] = "local-codex"
        else:
            meta["error"] = f"local_codex_failed: {local_err or 'unknown'}"

        if not response_text:
            response_text = (
                "Codex konnte gerade keine automatische Antwort erzeugen. "
                "Das Team schaut manuell drauf und meldet sich."
            )
            if "error" not in meta:
                meta["error"] = "no_response"

        success = bool(response_text) and not meta.get("error")
        status = "answered" if success else "failed"

        await issue_reports.update_status(
            report_id,
            status=status,
            ai_response=response_text,
            ai_model=str(meta.get("model") or ""),
            ai_error=str(meta.get("error") or "") if meta.get("error") else None,
        )

        await self._send_result(
            interaction=interaction,
            report_id=report_id,
            title=title,
            content=response_text,
            status=status,
            model=str(meta.get("model") or "n/a"),
            actions=action_results,
        )

    async def _run_local_codex(self, prompt: str) -> tuple[str | None, str | None]:
        """Startet den lokalen Codex-Prozess über CODEX_LOCAL_CMD und gibt (stdout|None, error|None) zurück."""
        cmd_text = LOCAL_CODEX_CMD.strip()
        if not cmd_text:
            return None, "CODEX_LOCAL_CMD leer"

        try:
            argv = shlex.split(cmd_text)
        except Exception as exc:  # pragma: no cover - defensive
            return None, f"cmd parse: {exc}"

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return None, f"cmd not found: {exc}"
        except Exception as exc:  # pragma: no cover
            return None, f"spawn failed: {exc}"

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=CODEX_TIMEOUT_SEC,
            )
        except Exception as exc:  # pragma: no cover
            proc.kill()
            return None, f"communicate failed: {exc}"

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore").strip()
            return None, err or f"exit {proc.returncode}"

        text = stdout.decode("utf-8", errors="ignore").strip()
        if not text:
            return None, "empty_output"
        return (text, None)

    async def _send_result(
        self,
        *,
        interaction: discord.Interaction,
        report_id: int,
        title: str,
        content: str,
        status: str,
        model: str,
        actions: list[str],
    ) -> None:
        trimmed = content[:4000]
        embed = discord.Embed(
            title=f"Ticket #{report_id}: {title or 'Problem'}",
            description=trimmed,
            colour=discord.Colour.green() if status == "answered" else discord.Colour.orange(),
        )
        embed.set_footer(text=f"Codex • Modell: {model or 'n/a'} • Status: {status}")
        if actions:
            embed.add_field(
                name="Automatische Aktionen",
                value="\n".join(f"- {line}" for line in actions)[:1024] or "—",
                inline=False,
            )

        try:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        except Exception as exc:
            log.debug("Followup für Ticket #%s fehlgeschlagen: %s", report_id, exc, exc_info=True)

        # Fallback: DM an den User
        try:
            user = interaction.user
            if user:
                await user.send(embed=embed)
        except Exception:
            log.exception("Ticket-Antwort konnte nicht zugestellt werden (id=%s)", report_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BugReporter(bot))
