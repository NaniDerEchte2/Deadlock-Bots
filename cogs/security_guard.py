import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, Iterable, List, Optional, Set, Tuple

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# ---------------- Static Config (edit here, no ENV needed) ----------------
SECURITY_CONFIG: Dict[str, object] = {
    # ID eines Textkanals, in den Beweise/Embeds gepostet werden.
    "REVIEW_CHANNEL_ID": 0,
    # ID des Mod-Kanals fuer Ban-Logs, Appeals und Unban-Button.
    "MOD_CHANNEL_ID": 1315684135175716978,
    # Aktion bei Treffer: "ban" oder "timeout".
    "PUNISHMENT": "ban",
    # Beobachtungsfenster fuer Burst-Detection (Sekunden).
    "WINDOW_SECONDS": 3600,  # 1h
    # Schwellen fuer Mehrkanal-Spam (z.B. 3 Nachrichten in 3 Channels).
    "CHANNEL_THRESHOLD": 3,
    "MESSAGE_THRESHOLD": 3,
    # Account muss juenger als X Stunden sein UND Join juenger als Y Minuten.
    "ACCOUNT_MAX_AGE_HOURS": 24,
    "JOIN_WATCH_MINUTES": 60,
    # Dauer des Timeouts in Minuten (Default: 24h).
    "TIMEOUT_MINUTES": 1440,
    # Timeout fuer Buttons (Sekunden).
    "VIEW_TIMEOUT_SECONDS": 86400,  # 24h
    # Appeal Textlaenge.
    "APPEAL_MIN_CHARS": 4,
    "APPEAL_MAX_CHARS": 800,
    # Attachment-Handling fuer Beweissicherung.
    "ATTACHMENT_FORWARD_LIMIT": 4,
    "ATTACHMENT_MAX_BYTES": 7_000_000,
    # Optional: Nur auf bestimmten Guilds aktivieren (leer = alle).
    "GUILD_IDS": [],
    # Schlagwort-Netz fuer typische Scam-Messages.
    "KEYWORDS": [
        "telegram",
        "dm me",
        "pm me",
        "friend request",
        "how to start earning",
        "100k",
        "usdt",
        "withdrawal",
        "payout",
        "profit",
        "woamax",
        "promo code",
        "bonus",
        "first 10 people",
        "earning $",
    ],
}


@dataclass
class RecentMessage:
    """Lightweight container for tracking a user's recent activity."""
    message: discord.Message
    channel_id: int
    created_at: datetime
    content: str
    attachments: List[discord.Attachment]


@dataclass
class IncidentCase:
    case_id: str
    guild_id: int
    user_id: int
    user_tag: str
    reason: str
    created_at: datetime
    action: str


class AppealModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "SecurityGuard",
        case_id: str,
        *,
        min_chars: int,
        max_chars: int,
    ) -> None:
        super().__init__(title="Appeal")
        self.cog = cog
        self.case_id = case_id
        self.appeal_reason = discord.ui.TextInput(
            label="Appeal reason",
            style=discord.TextStyle.paragraph,
            min_length=min_chars,
            max_length=max_chars,
            placeholder="Explain why this ban should be reviewed.",
        )
        self.add_item(self.appeal_reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_appeal_submission(
            interaction,
            self.case_id,
            self.appeal_reason.value.strip(),
        )


class AppealView(discord.ui.View):
    def __init__(self, cog: "SecurityGuard", case_id: str) -> None:
        super().__init__(timeout=cog.view_timeout_seconds)
        self.cog = cog
        self.case_id = case_id

    @discord.ui.button(label="Appeal", style=discord.ButtonStyle.primary)
    async def appeal_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            AppealModal(
                self.cog,
                self.case_id,
                min_chars=self.cog.appeal_min_chars,
                max_chars=self.cog.appeal_max_chars,
            )
        )


class UnbanView(discord.ui.View):
    def __init__(self, cog: "SecurityGuard", guild_id: int, user_id: int, case_id: str) -> None:
        super().__init__(timeout=cog.view_timeout_seconds)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.case_id = case_id

    @discord.ui.button(label="Unban", style=discord.ButtonStyle.danger)
    async def unban_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_unban_request(interaction, self.guild_id, self.user_id, self.case_id, button, self)

class SecurityGuard(commands.Cog):
    """
    Guards against brand new accounts that shotgun messages across channels.
    - Detects multi-channel bursts from accounts younger than X hours and fresh joins.
    - Deletes the burst, bans or times the member out, and mirrors evidence to a review channel.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        cfg = SECURITY_CONFIG
        self.review_channel_id = int(cfg.get("REVIEW_CHANNEL_ID", 0) or 0)
        self.mod_channel_id = int(cfg.get("MOD_CHANNEL_ID", 0) or 0)
        self.punishment = str(cfg.get("PUNISHMENT", "ban") or "ban").strip().lower()
        self.window_seconds = int(cfg.get("WINDOW_SECONDS", 120) or 120)
        self.channel_threshold = max(2, int(cfg.get("CHANNEL_THRESHOLD", 3) or 3))
        self.message_threshold = max(2, int(cfg.get("MESSAGE_THRESHOLD", 3) or 3))
        self.account_max_age_hours = max(1, int(cfg.get("ACCOUNT_MAX_AGE_HOURS", 24) or 24))
        self.join_watch_minutes = max(5, int(cfg.get("JOIN_WATCH_MINUTES", 60) or 60))
        self.timeout_minutes = max(5, int(cfg.get("TIMEOUT_MINUTES", 1440) or 1440))
        self.view_timeout_seconds = max(60, int(cfg.get("VIEW_TIMEOUT_SECONDS", 86400) or 86400))
        self.appeal_min_chars = max(1, int(cfg.get("APPEAL_MIN_CHARS", 4) or 4))
        self.appeal_max_chars = max(self.appeal_min_chars, int(cfg.get("APPEAL_MAX_CHARS", 800) or 800))
        self.attachment_forward_limit = max(0, int(cfg.get("ATTACHMENT_FORWARD_LIMIT", 4) or 4))
        self.attachment_max_bytes = max(1_000_000, int(cfg.get("ATTACHMENT_MAX_BYTES", 7_000_000) or 7_000_000))

        if self.punishment not in ("ban", "timeout"):
            self.punishment = "ban"

        raw_guilds = cfg.get("GUILD_IDS", [])
        guild_ids: List[int] = []
        if isinstance(raw_guilds, (list, tuple, set)):
            for item in raw_guilds:
                try:
                    guild_ids.append(int(item))
                except Exception:
                    continue
        self.allowed_guild_ids: Set[int] = set(guild_ids)

        self._message_history: Dict[int, Deque[RecentMessage]] = defaultdict(lambda: deque(maxlen=20))
        self._active_cases: Set[int] = set()
        self.case_cache_limit = 250
        self._cases: Dict[str, IncidentCase] = {}
        self._case_order: Deque[str] = deque()

        # Simple keyword net for common scam phrasing
        kw = cfg.get("KEYWORDS", [])
        kws: Set[str] = set()
        if isinstance(kw, (list, tuple, set)):
            for item in kw:
                if isinstance(item, str):
                    kws.add(item.lower())
        self.suspicious_keywords = kws

    # ---------------- Events ----------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        member = message.author

        if self.allowed_guild_ids and message.guild.id not in self.allowed_guild_ids:
            return
        if member.guild_permissions.manage_messages or member.guild_permissions.manage_guild:
            return  # do not police staff

        now = discord.utils.utcnow()
        if not self._is_new_account(member, now) or not self._is_recent_join(member, now):
            self._prune_history(member.id, now)
            return

        history = self._message_history[member.id]
        history.append(
            RecentMessage(
                message=message,
                channel_id=message.channel.id,
                created_at=message.created_at or now,
                content=message.content or "",
                attachments=list(message.attachments),
            )
        )
        self._prune_history(member.id, now)
        recent_msgs = list(history)

        triggered, reason, meta = self._should_trigger(member, recent_msgs, now)
        if not triggered:
            return
        if member.id in self._active_cases:
            return

        self._active_cases.add(member.id)
        try:
            await self._handle_incident(member, recent_msgs, reason, meta)
        finally:
            self._message_history.pop(member.id, None)
            self._active_cases.discard(member.id)

    # ---------------- Core logic ----------------
    def _is_new_account(self, member: discord.Member, now: datetime) -> bool:
        created = member.created_at
        if not created:
            return False
        age = now - created.replace(tzinfo=timezone.utc)
        return age.total_seconds() <= self.account_max_age_hours * 3600

    def _is_recent_join(self, member: discord.Member, now: datetime) -> bool:
        joined = member.joined_at
        if not joined:
            return False
        age = now - joined.replace(tzinfo=timezone.utc)
        return age.total_seconds() <= self.join_watch_minutes * 60

    def _prune_history(self, user_id: int, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.window_seconds)
        history = self._message_history.get(user_id)
        if not history:
            return
        while history and history[0].created_at < cutoff:
            history.popleft()

    def _make_case_id(self, member: discord.Member, now: datetime) -> str:
        return f"{member.guild.id}-{member.id}-{int(now.timestamp())}"

    def _remember_case(self, record: IncidentCase) -> None:
        self._cases[record.case_id] = record
        self._case_order.append(record.case_id)
        while len(self._case_order) > self.case_cache_limit:
            old_case = self._case_order.popleft()
            self._cases.pop(old_case, None)

    def _contains_suspicious_text(self, text: str) -> bool:
        lower = text.lower()
        return any(key in lower for key in self.suspicious_keywords)

    def _should_trigger(
        self,
        member: discord.Member,
        msgs: List[RecentMessage],
        now: datetime,
    ) -> Tuple[bool, str, Dict[str, int]]:
        if not msgs:
            return False, "", {}

        unique_channels = {m.channel_id for m in msgs}
        total_msgs = len(msgs)
        attachment_count = sum(1 for m in msgs if m.attachments)
        attachment_channels = {m.channel_id for m in msgs if m.attachments}
        keyword_hit = any(self._contains_suspicious_text(m.content) for m in msgs)

        multi_channel_burst = len(unique_channels) >= self.channel_threshold and total_msgs >= self.message_threshold
        two_channel_sus = len(unique_channels) >= 2 and total_msgs >= 2 and (keyword_hit or attachment_count > 0)
        attachment_multi_channel = len(attachment_channels) >= 2

        if multi_channel_burst or two_channel_sus or attachment_multi_channel:
            reason_bits = []
            if multi_channel_burst:
                reason_bits.append("multi-channel burst")
            if two_channel_sus and not multi_channel_burst:
                reason_bits.append("suspicious content across 2+ channels")
            if attachment_multi_channel and not multi_channel_burst:
                reason_bits.append("attachments across 2+ channels")
            if keyword_hit:
                reason_bits.append("keyword match")
            if attachment_count:
                reason_bits.append(f"{attachment_count} attachment(s)")
            reason = "; ".join(reason_bits) or "burst from new account"
            meta = {
                "channel_count": len(unique_channels),
                "message_count": total_msgs,
                "attachment_count": attachment_count,
                "keyword_hit": int(keyword_hit),
            }
            return True, reason, meta

        return False, "", {}

    async def _handle_incident(
        self,
        member: discord.Member,
        msgs: List[RecentMessage],
        reason: str,
        meta: Dict[str, int],
    ) -> None:
        now = discord.utils.utcnow()
        case_id = self._make_case_id(member, now)
        record = IncidentCase(
            case_id=case_id,
            guild_id=member.guild.id,
            user_id=member.id,
            user_tag=str(member),
            reason=reason,
            created_at=now,
            action=self.punishment,
        )
        self._remember_case(record)

        # Copy attachments before deletion
        forwarded_files = await self._collect_attachments(msgs)

        dm_sent = await self._send_user_dm(member, reason, case_id)
        action, action_ok = await self._apply_action(member, reason, case_id)
        deleted = await self._delete_messages(msgs, reason)

        await self._log_incident(
            member,
            msgs,
            reason,
            meta,
            action,
            action_ok,
            deleted,
            forwarded_files,
            case_id,
            dm_sent,
        )

    async def _send_user_dm(self, member: discord.Member, reason: str, case_id: str) -> bool:
        action_label = "banned" if self.punishment == "ban" else "timed out"
        action_title = "Ban" if self.punishment == "ban" else "Timeout"
        embed = discord.Embed(
            title=f"You were {action_label} by SecurityGuard",
            color=0xE74C3C,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Guild", value=member.guild.name, inline=False)
        embed.add_field(name="Reason", value=reason or "auto-detected burst", inline=False)
        embed.add_field(name="Case ID", value=case_id, inline=True)
        footer = (
            "If you believe this is a mistake, use the Appeal button."
            if self.punishment == "ban"
            else f"{action_title} duration: {self.timeout_minutes} minutes. Appeal is still available."
        )
        embed.set_footer(text=footer)
        try:
            await member.send(embed=embed, view=AppealView(self, case_id))
            return True
        except discord.Forbidden:
            return False
        except discord.HTTPException as exc:
            log.warning("Failed to DM member %s: %s", member.id, exc)
            return False

    async def _apply_action(self, member: discord.Member, reason: str, case_id: str) -> Tuple[str, bool]:
        if self.punishment == "ban":
            return "ban", await self._apply_ban(member, reason, case_id)
        return "timeout", await self._apply_timeout(member, reason, case_id)

    async def _apply_ban(self, member: discord.Member, reason: str, case_id: str) -> bool:
        guild = member.guild
        me = guild.me
        if me is None:
            try:
                me = await guild.fetch_member(self.bot.user.id)
            except discord.HTTPException:
                log.warning("Could not resolve self member for guild %s", guild.id)
                return False

        if not me.guild_permissions.ban_members:
            log.warning("Missing Ban Members permission to ban %s", member.id)
            return False

        ban_reason = f"[SecurityGuard][case:{case_id}] {reason}"
        try:
            try:
                await guild.ban(member, reason=ban_reason, delete_message_seconds=0)
            except TypeError:
                await guild.ban(member, reason=ban_reason, delete_message_days=0)
            return True
        except discord.Forbidden:
            log.warning("Forbidden to ban member %s", member.id)
        except discord.HTTPException as exc:
            log.warning("Failed to ban member %s: %s", member.id, exc)
        return False

    async def _apply_timeout(self, member: discord.Member, reason: str, case_id: str) -> bool:
        guild = member.guild
        me = guild.me
        if me is None:
            try:
                me = await guild.fetch_member(self.bot.user.id)
            except discord.HTTPException:
                log.warning("Could not resolve self member for guild %s", guild.id)
                return False

        perms = me.guild_permissions
        if not perms.moderate_members:
            log.warning("Missing Moderate Members permission to timeout %s", member.id)
            return False

        until = discord.utils.utcnow() + timedelta(minutes=self.timeout_minutes)
        timeout_reason = f"[SecurityGuard][case:{case_id}] {reason}"
        try:
            await member.edit(communication_disabled_until=until, reason=timeout_reason)
            return True
        except discord.Forbidden:
            log.warning("Forbidden to timeout member %s", member.id)
        except discord.HTTPException as exc:
            log.warning("Failed to timeout member %s: %s", member.id, exc)
        return False

    async def _delete_messages(self, msgs: List[RecentMessage], reason: str) -> int:
        deleted = 0
        seen: Set[int] = set()
        for msg in msgs:
            if msg.message.id in seen:
                continue
            seen.add(msg.message.id)
            try:
                await msg.message.delete(reason=f"[SecurityGuard] {reason}")
                deleted += 1
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                log.warning("Failed to delete message %s: %s", msg.message.id, exc)
            await asyncio.sleep(0.2)
        return deleted

    async def _collect_attachments(self, msgs: List[RecentMessage]) -> List[discord.File]:
        files: List[discord.File] = []
        per_channel_taken: Dict[int, int] = {}
        for msg in msgs:
            for att in msg.attachments:
                taken = per_channel_taken.get(msg.channel_id, 0)
                if taken >= 1:
                    continue  # nur ein Anhang pro Channel spiegeln
                if len(files) >= self.attachment_forward_limit:
                    return files
                if att.size and att.size > self.attachment_max_bytes:
                    log.info("Skip attachment %s (%s bytes) - too large for mirror", att.filename, att.size)
                    continue
                try:
                    files.append(await att.to_file())
                    per_channel_taken[msg.channel_id] = taken + 1
                except Exception as exc:
                    log.debug("Failed to mirror attachment %s: %s", att.filename, exc)
        return files

    async def _resolve_review_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if not self.review_channel_id:
            return None
        ch = guild.get_channel(self.review_channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        try:
            fetched = await guild.fetch_channel(self.review_channel_id)
            if isinstance(fetched, discord.TextChannel):
                return fetched
        except discord.HTTPException as exc:
            log.warning("Could not fetch review channel %s: %s", self.review_channel_id, exc)
        return None

    async def _resolve_mod_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if not self.mod_channel_id:
            return None
        ch = guild.get_channel(self.mod_channel_id)
        if isinstance(ch, discord.TextChannel):
            return ch
        try:
            fetched = await guild.fetch_channel(self.mod_channel_id)
            if isinstance(fetched, discord.TextChannel):
                return fetched
        except discord.HTTPException as exc:
            log.warning("Could not fetch mod channel %s: %s", self.mod_channel_id, exc)
        return None

    async def handle_appeal_submission(
        self,
        interaction: discord.Interaction,
        case_id: str,
        appeal_text: str,
    ) -> None:
        case = self._cases.get(case_id)
        user = interaction.user
        guild = None
        guild_id = case.guild_id if case else None
        if guild_id is None:
            parts = case_id.split("-", 2)
            if parts and parts[0].isdigit():
                guild_id = int(parts[0])
        if guild_id:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                try:
                    guild = await self.bot.fetch_guild(guild_id)
                except discord.HTTPException:
                    guild = None

        mod_channel = await self._resolve_mod_channel(guild) if guild else None
        safe_appeal = appeal_text.replace("`", "'").strip()
        if not safe_appeal:
            safe_appeal = "(empty)"

        if mod_channel:
            embed = discord.Embed(
                title="Appeal submitted",
                color=0x3498DB,
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Member", value=f"{user.mention} ({user.id})", inline=False)
            embed.add_field(name="Case ID", value=case_id, inline=True)
            if case:
                embed.add_field(name="Original reason", value=case.reason or "n/a", inline=False)
            embed.add_field(name="Appeal reason", value=safe_appeal[:1000], inline=False)
            try:
                await mod_channel.send(embed=embed)
            except discord.HTTPException as exc:
                log.warning("Failed to post appeal for case %s: %s", case_id, exc)
        else:
            log.warning("No mod channel set; appeal case %s logged to stdout.", case_id)
            log.info("Appeal %s by %s: %s", case_id, user.id, safe_appeal)

        try:
            await interaction.response.send_message("Your appeal was sent to the moderators.")
        except discord.HTTPException:
            pass

    async def handle_unban_request(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        user_id: int,
        case_id: str,
        button: discord.ui.Button,
        view: discord.ui.View,
    ) -> None:
        perms = getattr(interaction.user, "guild_permissions", None)
        if not perms or not (perms.ban_members or perms.administrator):
            await interaction.response.send_message("You do not have permission to unban.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None or guild.id != guild_id:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                try:
                    guild = await self.bot.fetch_guild(guild_id)
                except discord.HTTPException:
                    await interaction.followup.send("Guild not found.", ephemeral=True)
                    return

        try:
            await guild.unban(
                discord.Object(id=user_id),
                reason=f"[SecurityGuard][case:{case_id}] unban by {interaction.user} ({interaction.user.id})",
            )
            button.disabled = True
            try:
                await interaction.message.edit(view=view)
            except discord.HTTPException:
                pass
            await interaction.followup.send("Unban completed.", ephemeral=True)
        except discord.NotFound:
            await interaction.followup.send("User is not banned.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("Bot lacks permission to unban.", ephemeral=True)
        except discord.HTTPException as exc:
            log.warning("Unban failed for case %s: %s", case_id, exc)
            await interaction.followup.send("Unban failed.", ephemeral=True)

    async def _log_incident(
        self,
        member: discord.Member,
        msgs: List[RecentMessage],
        reason: str,
        meta: Dict[str, int],
        action: str,
        action_ok: bool,
        deleted: int,
        files: List[discord.File],
        case_id: str,
        dm_sent: bool,
    ) -> None:
        now = discord.utils.utcnow()
        channel_names = {
            m.channel_id: getattr(m.message.channel, "mention", f"#{m.channel_id}")
            for m in msgs
        }
        lines = []
        for idx, msg in enumerate(sorted(msgs, key=lambda m: m.created_at)):
            channel_display = channel_names.get(msg.channel_id, str(msg.channel_id))
            ts = msg.created_at.strftime("%H:%M:%S")
            snippet = msg.content.strip().replace("`", "'")
            if len(snippet) > 160:
                snippet = snippet[:157] + "..."
            attach_note = f" [attachments: {len(msg.attachments)}]" if msg.attachments else ""
            if not snippet:
                snippet = "(kein Text)"
            lines.append(f"{idx+1}. {ts} {channel_display}: {snippet}{attach_note}")

        proof_blocks = self._chunk_lines(lines, 1900)
        review_channel = await self._resolve_review_channel(member.guild)
        mod_channel = await self._resolve_mod_channel(member.guild)

        action_label = "Ban" if action == "ban" else "Timeout"
        if action == "ban":
            action_text = f"Ban: {'yes' if action_ok else 'failed'}"
        else:
            action_text = f"Timeout {self.timeout_minutes}m: {'yes' if action_ok else 'failed'}"

        embed = discord.Embed(
            title=f"Auto-{action_label}: possible scam/spam burst",
            color=0xE74C3C,
            timestamp=now,
        )
        embed.add_field(name="Member", value=f"{member.mention} ({member.id})", inline=False)
        embed.add_field(name="Case ID", value=case_id, inline=True)
        embed.add_field(name="Account age", value=self._fmt_delta(now, member.created_at), inline=True)
        embed.add_field(name="Time since join", value=self._fmt_delta(now, member.joined_at), inline=True)
        embed.add_field(
            name="Activity window",
            value=f"{meta.get('message_count', 0)} msgs / {meta.get('channel_count', 0)} channels in {self.window_seconds}s",
            inline=False,
        )
        embed.add_field(
            name="Signals",
            value=f"Keywords: {bool(meta.get('keyword_hit'))} | Attachments: {meta.get('attachment_count', 0)}",
            inline=True,
        )
        embed.add_field(
            name="Actions",
            value=f"{action_text}\nDeleted: {deleted}\nDM sent: {'yes' if dm_sent else 'no'}",
            inline=True,
        )
        embed.add_field(name="Reason", value=reason or "auto-detected burst", inline=False)

        sent_any = False
        if mod_channel:
            try:
                view = UnbanView(self, member.guild.id, member.id, case_id) if action == "ban" and action_ok else None
                await mod_channel.send(embed=embed, view=view)
                for block in proof_blocks:
                    await mod_channel.send(f"```{block}```")
                if files:
                    await mod_channel.send(
                        content="Mirrored attachments (capped).",
                        files=files,
                    )
                sent_any = True
            except discord.HTTPException as exc:
                log.warning("Failed to send mod log: %s", exc)

        if review_channel and (not mod_channel or review_channel.id != mod_channel.id):
            try:
                await review_channel.send(embed=embed)
                for block in proof_blocks:
                    await review_channel.send(f"```{block}```")
                sent_any = True
            except discord.HTTPException as exc:
                log.warning("Failed to send review log: %s", exc)

        if not sent_any:
            log.warning("No log channel set; incident logged to stdout.")
            log.info("Incident %s: %s", member.id, "\n".join(lines))

    def _chunk_lines(self, lines: Iterable[str], max_len: int) -> List[str]:
        chunks: List[str] = []
        buf = ""
        for line in lines:
            if len(buf) + len(line) + 1 > max_len:
                chunks.append(buf.rstrip())
                buf = ""
            buf += line + "\n"
        if buf:
            chunks.append(buf.rstrip())
        return chunks

    def _fmt_delta(self, now: datetime, past: Optional[datetime]) -> str:
        if not past:
            return "n/a"
        delta = now - past.replace(tzinfo=timezone.utc)
        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours}h"
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"

    # ---------------- Commands ----------------
    @commands.command(name="security_diag", help="Zeigt die aktiven Spam-Guard Schwellen.")
    @commands.has_permissions(administrator=True)
    async def security_diag(self, ctx: commands.Context):
        review = f"<#{self.review_channel_id}>" if self.review_channel_id else "nicht gesetzt"
        mod = f"<#{self.mod_channel_id}>" if self.mod_channel_id else "nicht gesetzt"
        guilds = ", ".join(str(g) for g in sorted(self.allowed_guild_ids)) if self.allowed_guild_ids else "alle"
        desc = (
            f"Fenster: {self.window_seconds}s | Kanaele: >= {self.channel_threshold} | "
            f"Nachrichten: >= {self.message_threshold}\n"
            f"Account-Alter: <= {self.account_max_age_hours}h | Join: <= {self.join_watch_minutes}min\n"
            f"Aktion: {self.punishment} | Timeout: {self.timeout_minutes}m\n"
            f"Review-Channel: {review} | Mod-Channel: {mod}\n"
            f"Aktiv auf Guilds: {guilds}"
        )
        await ctx.reply(desc, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(SecurityGuard(bot))
