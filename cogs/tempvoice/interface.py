# cogs/tempvoice/interface.py
import discord
import logging
import asyncio
from discord.ext import commands
from typing import Optional
from .core import MINRANK_CATEGORY_IDS, RANK_ORDER

logger = logging.getLogger("cogs.tempvoice.interface")

async def setup(bot: commands.Bot):
    """
    Lokales Setup nur für den Fall, dass diese Extension
    separat geladen wird. Erkennt vorhandene Core/Util-Cogs
    und vermeidet Doppel-Registrierungen.
    """
    # 1) Core ermitteln oder anlegen
    core = bot.get_cog("TempVoiceCore")
    if core is None:
        try:
            from .core import TempVoiceCore
            core = TempVoiceCore(bot)
            await bot.add_cog(core)
        except Exception as e:
            logger.exception("TempVoiceInterface.setup: Konnte TempVoiceCore nicht initialisieren: %r", e)
            return

    # 2) Util ermitteln oder anlegen
    util = getattr(core, "util", None)
    if util is None:
        try:
            from .util import TempVoiceUtil
            util = TempVoiceUtil(core)
            # util ist ein Hilfsobjekt, kein Cog – muss i. d. R. nicht registriert werden
        except Exception as e:
            logger.exception("TempVoiceInterface.setup: Konnte TempVoiceUtil nicht initialisieren: %r", e)
            return

    # 3) Interface-Cog hinzufügen (nur einmal)
    if bot.get_cog("TempVoiceInterface") is None:
        await bot.add_cog(TempVoiceInterface(bot, core, util))


def _find_rank_emoji(guild: Optional[discord.Guild], rank: str):
    if not guild:
        return None
    return discord.utils.get(guild.emojis, name=rank)

class TempVoiceInterface(commands.Cog):
    """UI/Buttons – persistente View & Interface-Message-Handling"""

    def __init__(self, bot: commands.Bot, core, util):
        self.bot = bot
        self.core = core          # TempVoiceCore
        self.util = util          # TempVoiceUtil

    async def cog_load(self):
        self.bot.add_view(MainView(self.core, self.util))  # persistente View
        asyncio.create_task(self._startup())

    async def _startup(self):
        await self.bot.wait_until_ready()
        await self.ensure_interface_message()
        await self.refresh_all_interfaces()

    async def ensure_interface_message(self, channel_hint: Optional[discord.TextChannel] = None):
        """
        Stellt sicher, dass die Interface-Nachricht existiert – exakt in dem Textkanal,
        in dem der Command ausgeführt wurde (channel_hint).
        Fällt nicht auf andere Channels zurück.
        """
        ch: Optional[discord.TextChannel] = channel_hint if isinstance(channel_hint, discord.TextChannel) else None
        guild = ch.guild if ch else None

        if ch is None or guild is None:
            return None  # Muss mit einem Textkanal aufgerufen werden

        embed = discord.Embed(
            title="TempVoice Interface",
            description=(
                "• Join einen **Staging-Channel** → deine Lane wird erstellt und du wirst gemoved.\n"
                "• **Steuerung (hier im Interface)**:\n"
                "  - 🇩🇪/🇪🇺 Sprachfilter (Rolle „English Only“)\n"
                "  - 👑 Owner Claim (übernimmt die Lane)\n"
                "  - 🎚️ Limit setzen (0–99)\n"
                "  - 👢 Kick / 🚫 Ban / ♻️ Unban\n"
                "  - 🪪 Mindest-Rang (Grind & Ranked)"
            ),
            color=0x2ecc71
        )
        embed.set_footer(text="Deutsche Deadlock Community • TempVoice")

        # Immer in diesem Channel senden (kein Fallback/Fetch anderer Messages)
        try:
            msg = await ch.send(embed=embed, view=MainView(self.core, self.util))
            await self._record_interface_message(int(guild.id), int(ch.id), int(msg.id), None, None)
        except discord.Forbidden:
            logger.warning("ensure_interface_message: Keine Berechtigung zum Senden in %s (%s)", ch, ch.id)
            return None
        except discord.HTTPException as e:
            logger.warning("ensure_interface_message: HTTP-Fehler beim Senden/Speichern der Interface-Nachricht: %r", e)
            return None
        except Exception as e:
            logger.debug("ensure_interface_message: Fehler beim Senden/Speichern der Interface-Nachricht: %r", e)
            return None
        return ch

    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @commands.command(name="tvpanel", aliases=["tempvoicepanel", "tvinterface"])
    async def cmd_tvpanel(self, ctx: commands.Context):
        """
        Erstellt/aktualisiert das TempVoice Interface und speichert es, damit es nach Neustart erhalten bleibt.
        """
        try:
            async with ctx.typing():
                ch = await self.ensure_interface_message(
                    ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None
                )
        except discord.Forbidden:
            await ctx.reply("❌ Keine Berechtigung, um das Interface zu erstellen.", mention_author=False)
            return
        except Exception as e:
            logger.exception("tvpanel command failed: %r", e)
            await ctx.reply("❌ Konnte das Interface nicht erstellen (Fehler im Log).", mention_author=False)
            return
        try:
            if isinstance(ch, discord.TextChannel):
                await ctx.reply(f"✅ TempVoice Interface erstellt/aktualisiert in {ch.mention}.", mention_author=False)
            else:
                await ctx.reply("⚠️ Konnte das Interface nicht erstellen (kein Textkanal oder keine Rechte).", mention_author=False)
        except Exception as e:
            logger.exception("tvpanel command failed: %r", e)
            await ctx.reply("❌ Konnte das Interface nicht erstellen (Fehler im Log).", mention_author=False)

    async def _record_interface_message(self, guild_id: int, channel_id: int, message_id: int,
                                        category_id: Optional[int], lane_id: Optional[int]):
        try:
            if lane_id is not None:
                await self.core.db.exec(
                    """
                    INSERT INTO tempvoice_interface(guild_id, channel_id, message_id, category_id, lane_id, updated_at)
                    VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(lane_id) DO UPDATE SET
                        channel_id=excluded.channel_id,
                        message_id=excluded.message_id,
                        category_id=excluded.category_id,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (guild_id, channel_id, message_id, category_id, lane_id)
                )
            else:
                await self.core.db.exec(
                    """
                    INSERT INTO tempvoice_interface(guild_id, channel_id, message_id, category_id, lane_id, updated_at)
                    VALUES(?,?,?,?,NULL,CURRENT_TIMESTAMP)
                    ON CONFLICT(guild_id, message_id) DO UPDATE SET
                        channel_id=excluded.channel_id,
                        category_id=excluded.category_id,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (guild_id, channel_id, message_id, category_id)
                )
        except Exception as e:
            logger.debug("_record_interface_message: Persistenz fehlgeschlagen (guild=%s lane=%s): %r",
                         guild_id, lane_id, e)

    def _lane_embed(self, lane: discord.VoiceChannel, owner_id: Optional[int]) -> discord.Embed:
        owner_display = "Unbekannt"
        if owner_id:
            owner = lane.guild.get_member(int(owner_id))
            if owner:
                owner_display = owner.mention
            else:
                owner_display = f"<@{owner_id}>"
        embed = discord.Embed(
            title=f"TempVoice Interface – {lane.name}",
            description=(
                f"Owner: {owner_display}\n"
                "• Buttons funktionieren nur, wenn du in dieser Lane bist.\n"
                "• 🇩🇪/🇪🇺 Sprachfilter, Limit, Kick/Ban & Owner Claim direkt hier nutzen."
            ),
            color=0x2ecc71,
        )
        embed.set_footer(text="Deutsche Deadlock Community • TempVoice")
        return embed

    async def ensure_lane_interface(self, lane: discord.VoiceChannel, owner_id: Optional[int] = None):
        owner_id = owner_id or self.core.lane_owner.get(lane.id)
        embed = self._lane_embed(lane, owner_id)

        row = None
        try:
            row = await self.core.db.fetchone(
                "SELECT channel_id, message_id FROM tempvoice_interface WHERE lane_id=?",
                (int(lane.id),)
            )
        except Exception as e:
            logger.debug("ensure_lane_interface: DB-Select fehlgeschlagen für Lane %s: %r", lane.id, e)

        if row:
            target = self.bot.get_channel(int(row["channel_id"])) or lane
            try:
                msg = await target.fetch_message(int(row["message_id"]))
                await msg.edit(embed=embed, view=MainView(self.core, self.util))
                await self._record_interface_message(
                    int(lane.guild.id),
                    int(target.id),
                    int(msg.id),
                    int(lane.category_id) if lane.category_id else None,
                    int(lane.id)
                )
                return
            except (discord.NotFound, discord.Forbidden):
                await self._remove_lane_interface_record(int(lane.id))
            except discord.HTTPException as e:
                logger.debug("ensure_lane_interface: HTTP-Fehler beim Editieren (Lane %s): %r", lane.id, e)
                return
            except Exception as e:
                logger.debug("ensure_lane_interface: Fehler beim Editieren (Lane %s): %r", lane.id, e)
                return

        try:
            msg = await lane.send(embed=embed, view=MainView(self.core, self.util))
        except discord.Forbidden:
            logger.warning("TempVoice Lane Interface: Keine Berechtigung zum Senden in VoiceChannel %s.", lane.id)
            return
        except discord.HTTPException as e:
            logger.warning("TempVoice Lane Interface: HTTP-Fehler beim Senden in %s: %r", lane.id, e)
            return
        except Exception as e:
            logger.debug("TempVoice Lane Interface: Unerwarteter Fehler beim Senden in %s: %r", lane.id, e)
            return

        await self._record_interface_message(
            int(lane.guild.id),
            int(lane.id),
            int(msg.id),
            int(lane.category_id) if lane.category_id else None,
            int(lane.id)
        )

    async def rehydrate_lane_interfaces(self):
        try:
            rows = await self.core.db.fetchall(
                "SELECT channel_id, message_id, lane_id FROM tempvoice_interface WHERE lane_id IS NOT NULL"
            )
        except Exception as e:
            logger.debug("rehydrate_lane_interfaces: DB-Select fehlgeschlagen: %r", e)
            return

        for row in rows:
            lane_id = int(row["lane_id"])
            lane = self.bot.get_channel(lane_id)
            if not isinstance(lane, discord.VoiceChannel):
                await self._remove_lane_interface_record(lane_id)
                continue

            target = self.bot.get_channel(int(row["channel_id"])) or lane
            try:
                msg = await target.fetch_message(int(row["message_id"]))
            except (discord.NotFound, discord.Forbidden):
                await self._remove_lane_interface_record(lane_id)
                await self.ensure_lane_interface(lane)
                continue
            except discord.HTTPException as e:
                logger.debug("rehydrate_lane_interfaces: HTTP-Fehler beim Laden (Lane %s): %r", lane_id, e)
                continue
            except Exception as e:
                logger.debug("rehydrate_lane_interfaces: Fehler beim Laden (Lane %s): %r", lane_id, e)
                continue

            owner_id = self.core.lane_owner.get(lane_id)
            try:
                await msg.edit(embed=self._lane_embed(lane, owner_id), view=MainView(self.core, self.util))
                await self._record_interface_message(
                    int(lane.guild.id),
                    int(target.id),
                    int(msg.id),
                    int(lane.category_id) if lane.category_id else None,
                    lane_id
                )
            except discord.HTTPException as e:
                logger.debug("rehydrate_lane_interfaces: HTTP-Fehler beim Editieren (Lane %s): %r", lane_id, e)
            except Exception as e:
                logger.debug("rehydrate_lane_interfaces: Fehler beim Editieren (Lane %s): %r", lane_id, e)

    async def _refresh_global_interface_messages(self):
        """
        Aktualisiert Interface-Nachrichten (ohne Lane-Bindung) mit der aktuellen View,
        damit neue Buttons (z. B. Normale Lane) auch dort erscheinen.
        """
        try:
            rows = await self.core.db.fetchall(
                "SELECT guild_id, channel_id, message_id, category_id FROM tempvoice_interface WHERE lane_id IS NULL"
            )
        except Exception as e:
            logger.debug("refresh_global_interfaces: DB-Select fehlgeschlagen: %r", e)
            return

        for row in rows:
            channel = self.bot.get_channel(int(row["channel_id"]))
            if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel)):
                continue
            try:
                msg = await channel.fetch_message(int(row["message_id"]))
            except (discord.NotFound, discord.Forbidden):
                continue
            except discord.HTTPException as e:
                logger.debug("refresh_global_interfaces: fetch fehlgeschlagen f�r Message %s: %r", row["message_id"], e)
                continue
            except Exception as e:
                logger.debug("refresh_global_interfaces: unerwarteter Fehler f�r Message %s: %r", row["message_id"], e)
                continue

            try:
                await msg.edit(view=MainView(self.core, self.util))
                await self._record_interface_message(
                    int(row["guild_id"]),
                    int(channel.id),
                    int(msg.id),
                    int(row["category_id"]) if row["category_id"] else None,
                    None,
                )
            except discord.HTTPException as e:
                logger.debug("refresh_global_interfaces: edit fehlgeschlagen f�r Message %s: %r", msg.id, e)
            except Exception as e:
                logger.debug("refresh_global_interfaces: unerwarteter Fehler beim Editieren f�r Message %s: %r", msg.id, e)

    async def refresh_all_interfaces(self):
        await self._refresh_global_interface_messages()
        await self.rehydrate_lane_interfaces()

    async def _remove_lane_interface_record(self, lane_id: int):
        try:
            await self.core.db.exec(
                "DELETE FROM tempvoice_interface WHERE lane_id=?",
                (int(lane_id),)
            )
        except Exception as e:
            logger.debug("remove_lane_interface_record: DB-Delete fehlgeschlagen für Lane %s: %r", lane_id, e)

    @commands.Cog.listener()
    async def on_tempvoice_lane_created(self, lane: discord.VoiceChannel, owner: discord.Member):
        await self.ensure_lane_interface(lane, owner_id=owner.id)

    @commands.Cog.listener()
    async def on_tempvoice_lane_owner_changed(self, lane: discord.VoiceChannel, owner_id: int):
        await self.ensure_lane_interface(lane, owner_id=owner_id)

    @commands.Cog.listener()
    async def on_tempvoice_lane_deleted(self, lane_id: int):
        await self._remove_lane_interface_record(int(lane_id))


# -------------------- UI Komponenten --------------------

class MainView(discord.ui.View):
    def __init__(self, core, util):
        super().__init__(timeout=None)
        self.core = core
        self.util = util
        # Row 0: Region + OwnerClaim + Limit
        self.add_item(RegionDEButton(core))
        self.add_item(RegionEUButton(core))
        self.add_item(OwnerClaimButton(core))
        self.add_item(LimitButton(core))
        # Row 1: Kick/Ban/Unban
        self.add_item(KickButton(util))
        self.add_item(BanButton(util))
        self.add_item(UnbanButton(util))
        # Row 2: MinRank (eigene Reihe!)
        self.add_item(MinRankSelect(core))
        # Row 3: Quick Templates
        self.add_item(ResetLaneButton(core))
        self.add_item(DuoCallButton(core))
        self.add_item(TrioCallButton(core))
        self.add_item(LurkerButton(util))

    @staticmethod
    def lane_of(itx: discord.Interaction) -> Optional[discord.VoiceChannel]:
        m: discord.Member = itx.user  # type: ignore
        return m.voice.channel if (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)) else None


class RegionDEButton(discord.ui.Button):
    def __init__(self, core):
        super().__init__(label="🇩🇪 DE", style=discord.ButtonStyle.primary, row=0, custom_id="tv_region_de")
        self.core = core
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
            return
        owner_id = self.core.lane_owner.get(lane.id, m.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            await itx.response.send_message("Nur Owner/Mods dürfen den Sprachfilter ändern.", ephemeral=True)
            return
        await self.core.set_owner_region(owner_id, "DE")
        await self.core.apply_owner_region_to_lane(lane, owner_id)
        await itx.response.send_message("Deutsch-Only aktiv.", ephemeral=True)

class RegionEUButton(discord.ui.Button):
    def __init__(self, core):
        super().__init__(label="🇪🇺 EU", style=discord.ButtonStyle.secondary, row=0, custom_id="tv_region_e")
        self.core = core
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
            return
        owner_id = self.core.lane_owner.get(lane.id, m.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            await itx.response.send_message("Nur Owner/Mods dürfen den Sprachfilter ändern.", ephemeral=True)
            return
        await self.core.set_owner_region(owner_id, "EU")
        await self.core.apply_owner_region_to_lane(lane, owner_id)
        await itx.response.send_message("Sprachfilter aufgehoben (EU).", ephemeral=True)

class OwnerClaimButton(discord.ui.Button):
    def __init__(self, core):
        super().__init__(label="👑 Owner Claim", style=discord.ButtonStyle.success, row=0, custom_id="tv_owner_claim")
        self.core = core
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
            return
        await self.core.transfer_owner(lane, m.id)
        await itx.response.send_message("Du bist jetzt Owner dieser Lane.", ephemeral=True)

class LimitButton(discord.ui.Button):
    def __init__(self, core):
        super().__init__(label="🎚️ Limit setzen", style=discord.ButtonStyle.secondary, row=0, custom_id="tv_limit_btn")
        self.core = core
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
            return
        owner_id = self.core.lane_owner.get(lane.id, m.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            await itx.response.send_message("Nur Owner/Mods dürfen das Limit setzen.", ephemeral=True)
            return
        await itx.response.send_modal(LimitModal(self.core, lane))

class LimitModal(discord.ui.Modal, title="Limit setzen"):
    value = discord.ui.TextInput(label="Limit (0-99)", placeholder="z.B. 6", required=True, max_length=2)
    def __init__(self, core, lane: discord.VoiceChannel):
        super().__init__(timeout=120)
        self.core = core
        self.lane = lane
    async def on_submit(self, itx: discord.Interaction):
        txt = str(self.value.value).strip()
        try:
            val = int(txt)
        except ValueError:
            await itx.response.send_message("Bitte Zahl (0-99) eingeben.", ephemeral=True)
            return
        if val < 0 or val > 99:
            await itx.response.send_message("Limit muss 0-99 sein.", ephemeral=True)
            return
        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except discord.HTTPException as e:
            logger.debug("LimitModal: defer fehlgeschlagen: %r", e)
        except Exception as e:
            logger.debug("LimitModal: unerwarteter defer-Fehler: %r", e)
        await self.core.safe_edit_channel(self.lane, desired_limit=val, reason="TempVoice: Limit gesetzt")
        await self.core.refresh_name(self.lane)
        try:
            await itx.followup.send(f"Limit auf {val} gesetzt.", ephemeral=True)
        except discord.HTTPException as e:
            logger.debug("LimitModal: followup.send fehlgeschlagen: %r", e)
        except Exception as e:
            logger.debug("LimitModal: unerwarteter followup-Fehler: %r", e)

class QuickTemplateButton(discord.ui.Button):
    def __init__(self, core, *, label: str, template_name: str, limit: int, custom_id: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, row=3, custom_id=custom_id)
        self.core = core
        self.template_name = template_name
        self.limit = limit

    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
            return
        owner_id = self.core.lane_owner.get(lane.id, m.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            await itx.response.send_message("Nur Owner/Mods d�rfen Templates benutzen.", ephemeral=True)
            return
        await self.core.set_lane_template(lane, base_name=self.template_name, limit=self.limit)
        await itx.response.send_message(
            f"Lane auf {self.template_name} gestellt (Limit {self.limit}).",
            ephemeral=True,
        )

class DuoCallButton(QuickTemplateButton):
    def __init__(self, core):
        super().__init__(core, label="Duo Call (2)", template_name="Duo Call", limit=2, custom_id="tv_tpl_duo")

class TrioCallButton(QuickTemplateButton):
    def __init__(self, core):
        super().__init__(core, label="Trio Call (3)", template_name="Trio Call", limit=3, custom_id="tv_tpl_trio")

class ResetLaneButton(discord.ui.Button):
    def __init__(self, core):
        super().__init__(label="Normale Lane", style=discord.ButtonStyle.secondary, row=3, custom_id="tv_tpl_reset")
        self.core = core

    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
            return
        owner_id = self.core.lane_owner.get(lane.id, m.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            await itx.response.send_message("Nur Owner/Mods d�rfen Templates benutzen.", ephemeral=True)
            return
        base, limit = await self.core.reset_lane_template(lane)
        await itx.response.send_message(
            f"Lane auf {base} zur�ckgesetzt (Limit {limit}).",
            ephemeral=True,
        )

class MinRankSelect(discord.ui.Select):
    def __init__(self, core):
        self.core = core
        guild = None
        ref_guild = self.core.first_guild()
        if ref_guild:
            guild = ref_guild
        options = [discord.SelectOption(label="Kein Limit (Jeder)", value="unknown", emoji=_find_rank_emoji(guild,"unknown") or "✅")]
        for r in RANK_ORDER[1:]:
            options.append(discord.SelectOption(label=r.capitalize(), value=r, emoji=_find_rank_emoji(guild,r)))
        super().__init__(placeholder="Mindest-Rang (Lane, falls aktiviert)", min_values=1, max_values=1, options=options, row=2, custom_id="tv_minrank")
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        if not (m.voice and isinstance(m.voice.channel, discord.VoiceChannel)):
            await itx.response.send_message("Tritt zuerst deiner Lane bei.", ephemeral=True)
            return
        lane: discord.VoiceChannel = m.voice.channel
        if lane.category_id not in MINRANK_CATEGORY_IDS:
            await itx.response.send_message("Mindest-Rang ist hier deaktiviert.", ephemeral=True)
            return
        choice = self.values[0]
        def _idx(name: str) -> int:
            order = ["unknown","initiate","seeker","alchemist","arcanist","ritualist","emissary","archon","oracle","phantom","ascendant","eternus"]
            try:
                return order.index(name)
            except ValueError:
                return 0

        member_rank_idx = 0
        for role in m.roles:
            member_rank_idx = max(member_rank_idx, _idx(role.name.lower()))
        choice_idx = _idx(choice)
        if choice_idx > member_rank_idx:
            user_rank_label = RANK_ORDER[member_rank_idx].capitalize() if member_rank_idx < len(RANK_ORDER) else "Unknown"
            await itx.response.send_message(
                f"Du kannst keinen Mindest-Rang über deinem eigenen setzen. Dein Rang: {user_rank_label}.",
                ephemeral=True,
            )
            return

        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except discord.HTTPException as e:
            logger.debug("MinRankSelect: defer fehlgeschlagen: %r", e)
        except Exception as e:
            logger.debug("MinRankSelect: unerwarteter defer-Fehler: %r", e)

        self.core.lane_min_rank[lane.id] = choice
        await self.core._apply_min_rank(lane, choice)  # type: ignore[attr-defined]
        await self.core.refresh_name(lane)

        label = "Kein Limit" if choice == "unknown" else choice.capitalize()
        try:
            await itx.followup.send(f"Mindest-Rang gesetzt auf: {label}.", ephemeral=True)
        except Exception as e:
            logger.debug("MinRankSelect followup fehlgeschlagen: %r", e)

class KickButton(discord.ui.Button):
    def __init__(self, util):
        super().__init__(label="👢 Kick", style=discord.ButtonStyle.secondary, row=1, custom_id="tv_kick")
        self.util = util
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Du musst in einer Lane sein.", ephemeral=True)
            return
        owner_id = itx.client.get_cog("TempVoiceCore").lane_owner.get(lane.id, m.id)  # type: ignore
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            await itx.response.send_message("Nur Owner/Mods dürfen kicken.", ephemeral=True)
            return
        options = [discord.SelectOption(label=u.display_name, value=str(u.id)) for u in lane.members if u.id != m.id]
        if not options:
            await itx.response.send_message("Niemand zum Kicken vorhanden.", ephemeral=True)
            return
        view = KickSelectView(self.util, lane, options)
        await itx.response.send_message("Wen möchtest du kicken?", view=view, ephemeral=True)

class KickSelect(discord.ui.Select):
    def __init__(self, options, placeholder="Mitglied wählen …"):
        super().__init__(min_values=1, max_values=1, options=options, placeholder=placeholder)
    async def callback(self, itx: discord.Interaction):
        view: "KickSelectView" = self.view  # type: ignore
        await view.handle_kick(itx, int(self.values[0]))

class KickSelectView(discord.ui.View):
    def __init__(self, util, lane: discord.VoiceChannel, options):
        super().__init__(timeout=60)
        self.util = util
        self.lane = lane
        self.add_item(KickSelect(options))
    async def handle_kick(self, itx: discord.Interaction, target_id: int):
        ok, msg = await self.util.kick(self.lane, target_id, reason=f"Kick durch {itx.user}")
        await itx.response.send_message(msg, ephemeral=True)

class BanButton(discord.ui.Button):
    def __init__(self, util):
        super().__init__(label="🚫 Ban", style=discord.ButtonStyle.danger, row=1, custom_id="tv_ban")
        self.util = util
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Du musst in einer Lane sein.", ephemeral=True)
            return
        owner_id = itx.client.get_cog("TempVoiceCore").lane_owner.get(lane.id)  # type: ignore
        if owner_id is None:
            await itx.response.send_message("Aktuell ist kein Owner gesetzt.", ephemeral=True)
            return
        perms = lane.permissions_for(m)
        if owner_id != m.id and not perms.administrator:
            await itx.response.send_message("Nur der Owner darf bannen.", ephemeral=True)
            return
        await itx.response.send_modal(BanModal(self.util, lane, action="ban"))

class UnbanButton(discord.ui.Button):
    def __init__(self, util):
        super().__init__(label="♻️ Unban", style=discord.ButtonStyle.primary, row=1, custom_id="tv_unban")
        self.util = util
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Du musst in einer Lane sein.", ephemeral=True)
            return
        owner_id = itx.client.get_cog("TempVoiceCore").lane_owner.get(lane.id)  # type: ignore
        if owner_id is None:
            await itx.response.send_message("Aktuell ist kein Owner gesetzt.", ephemeral=True)
            return
        perms = lane.permissions_for(m)
        if owner_id != m.id and not perms.administrator:
            await itx.response.send_message("Nur der Owner darf entbannen.", ephemeral=True)
            return
        await itx.response.send_modal(BanModal(self.util, lane, action="unban"))

class BanModal(discord.ui.Modal, title="User (Un)Ban"):
    target = discord.ui.TextInput(label="User (@Mention/Name/ID)", placeholder="@Name oder 123456789012345678", required=True, max_length=64)
    def __init__(self, util, lane: discord.VoiceChannel, action: str):
        super().__init__(timeout=120)
        self.util = util
        self.lane = lane
        self.action = action
    async def on_submit(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        owner_id = itx.client.get_cog("TempVoiceCore").lane_owner.get(self.lane.id)  # type: ignore
        if owner_id is None:
            await itx.response.send_message("Aktuell ist kein Owner gesetzt.", ephemeral=True)
            return
        perms = self.lane.permissions_for(m)
        if owner_id != m.id and not perms.administrator:
            await itx.response.send_message("Nur der Owner darf (un)bannen.", ephemeral=True)
            return
        raw = str(self.target.value).strip()
        try:
            await itx.response.defer(ephemeral=True, thinking=False)
        except discord.HTTPException as e:
            logger.debug("BanModal: defer fehlgeschlagen: %r", e)
        except Exception as e:
            logger.debug("BanModal: unerwarteter defer-Fehler: %r", e)

        if self.action == "ban":
            ok, msg = await self.util.ban(self.lane, owner_id, raw)
        else:
            ok, msg = await self.util.unban(self.lane, owner_id, raw)
        try:
            await itx.followup.send(msg, ephemeral=True)
        except discord.HTTPException as e:
            logger.debug("BanModal: followup.send fehlgeschlagen: %r", e)
        except Exception as e:
            logger.debug("BanModal: unerwarteter followup-Fehler: %r", e)

class LurkerButton(discord.ui.Button):
    def __init__(self, util):
        super().__init__(label="👻 Lurker", style=discord.ButtonStyle.secondary, row=3, custom_id="tv_lurker")
        self.util = util
    async def callback(self, itx: discord.Interaction):
        m: discord.Member = itx.user  # type: ignore
        lane = MainView.lane_of(itx)
        if not lane:
            await itx.response.send_message("Du musst in einer Lane sein.", ephemeral=True)
            return
        # Permission check: Owner or Mod
        owner_id = itx.client.get_cog("TempVoiceCore").lane_owner.get(lane.id, m.id)
        perms = lane.permissions_for(m)
        if not (owner_id == m.id or perms.manage_channels or perms.administrator):
            await itx.response.send_message("Nur Owner/Mods dürfen Lurker ernennen.", ephemeral=True)
            return

        options = [discord.SelectOption(label=u.display_name, value=str(u.id)) for u in lane.members]
        if not options:
            await itx.response.send_message("Niemand da.", ephemeral=True)
            return
        
        view = LurkerSelectView(self.util, lane, options)
        await itx.response.send_message("Wer soll Lurker sein?", view=view, ephemeral=True)

class LurkerSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(min_values=1, max_values=1, options=options, placeholder="Mitglied wählen …")
    async def callback(self, itx: discord.Interaction):
        view: "LurkerSelectView" = self.view  # type: ignore
        await view.handle_selection(itx, int(self.values[0]))

class LurkerSelectView(discord.ui.View):
    def __init__(self, util, lane: discord.VoiceChannel, options):
        super().__init__(timeout=60)
        self.util = util
        self.lane = lane
        self.add_item(LurkerSelect(options))
    async def handle_selection(self, itx: discord.Interaction, target_id: int):
        await itx.response.defer(ephemeral=True)
        ok, msg = await self.util.make_lurker(self.lane, target_id)
        await itx.followup.send(msg, ephemeral=True)

