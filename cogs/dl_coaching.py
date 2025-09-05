import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Modal, TextInput, Select
import datetime
import json
import socket
from pathlib import Path


class DlCoachingCog(commands.Cog):
    """DL Coaching System mit Dropdowns (keine Emoji-Reaktionen)."""

    CHANNEL_ID = 1357421075188813897
    EXISTING_MESSAGE_ID = 1383883328385454210

    SOCKET_HOST = "localhost"
    SOCKET_PORT = 45680

    RANKS = {
        "initiate": "<:initiate:1316457822518775869>",
        "seeker": "<:seeker:1316458138886475876>",
        "alchemist": "<:alchemist:1316455291629342750>",
        "arcanist": "<:arcanist:1316455305315352587>",
        "ritualist": "<:ritualist:1316458203298660533>",
        "emissary": "<:emissary:1316457650367496306>",
        "archon": "<:archon:1316457293801324594>",
        "oracle": "<:oracle:1316457885743579317>",
        "phantom": "<:phantom:1316457982363701278>",
        "ascendant": "<:ascendant:1316457367818338385>",
        "eternus": "<:eternus:1316457737621868574>",
    }

    SUBRANKS = ["i", "ii", "iii", "iv", "v", "✶"]

    HEROES_PAGE_1 = {
        "abrams": "<:abrams:1371194882483294280>",
        "bebot": "<:bebot:1371194884547023080>",
        "calico": "<:calico:1371194886845632582>",
        "dynamo": "<:dynamo:1371194889592766514>",
        "grey_talon": "<:grey_talon:1371194891362898002>",
        "haze": "<:haze:1371194893640142858>",
        "holiday": "<:holiday:1371194895686963304>",
        "infernus": "<:Infernus:1371194897939566663>",
        "ivy": "<:ivy:1371194899432476722>",
        "kelvin": "<:kelvin:1371194901391474860>",
        "lady_geist": "<:lady_geist:1371194903018733758>",
        "lash": "<:lash:1371194904545333428>",
        "mirage": "<:mirage:1371194910232809552>",
    }

    HEROES_PAGE_2 = {
        "mo": "<:mo:1371194912489472091>",
        "paradox": "<:paradox:1371194915551182858>",
        "pocket": "<:pocket:1371194917627494420>",
        "seven": "<:seven:1371209369177427989>",
        "mcginnis": "<:mcginnis:1371209373350629428>",
        "sinclair": "<:sinclair:1371209380117968976>",
        "viscous": "<:viscous:1371209383586785380>",
        "viper": "<:viper:1371209397506406411>",
        "vyper": "<:vyper:1371209401519575192>",
        "warden": "<:warden:1371209405068214442>",
        "wraith": "<:wraith:1371209407781666826>",
        "yamato": "<:yamato:1371209416258359376>",
    }

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        self.base_dir = Path(__file__).resolve().parent.parent
        self.data_dir = self.base_dir / "coaching_data"
        self.logs_dir = self.base_dir / "logs"
        self._ensure_directories()
        self._create_default_files()

        self.user_data: dict[int, dict] = {}
        self.active_threads: dict[int, int] = {}
        self.thread_last_activity: dict[int, datetime.datetime] = {}

        self._timeout_loop = self._build_timeout_loop()

    # ---------- Helpers ----------
    @staticmethod
    def _to_partial_emoji(mention: str) -> discord.PartialEmoji | None:
        try:
            if not mention:
                return None
            # format: <:{name}:{id}>
            if mention.startswith("<:") and mention.endswith(">"):
                inner = mention[2:-1]
                name, sid = inner.split(":", 1)
                return discord.PartialEmoji(name=name, id=int(sid))
        except Exception:
            return None
        return None

    @staticmethod
    def _safe_option_emoji(guild: discord.Guild | None, mention: str) -> discord.PartialEmoji | None:
        try:
            if not guild:
                return None
            if not mention or not mention.startswith("<:"):
                return None
            inner = mention[2:-1]
            _name, sid = inner.split(":", 1)
            em = guild.get_emoji(int(sid))
            if em:
                return discord.PartialEmoji(name=em.name, id=em.id)
        except Exception:
            return None
        return None

    # ---------- Filesystem helpers ----------
    def _ensure_directories(self) -> None:
        for d in (self.data_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _create_default_files(self) -> None:
        defaults = [
            (self.data_dir / "user_data.json", "{}"),
            (self.data_dir / "active_threads.json", "{}"),
            (self.logs_dir / "coaching_logs.txt", ""),
        ]
        for p, content in defaults:
            if not p.exists():
                p.write_text(content, encoding="utf-8")

    # ---------- Socket notify ----------
    def _notify_claim_bot(self, thread_data: dict) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((self.SOCKET_HOST, self.SOCKET_PORT))
                payload = json.dumps(thread_data).encode("utf-8")
                s.sendall(len(payload).to_bytes(4, byteorder="big"))
                s.sendall(payload)
        except Exception:
            pass

    async def _find_coaching_message_in_channel(self, channel: discord.TextChannel):
        try:
            async for message in channel.history(limit=50):
                if (
                    message.author.id == self.bot.user.id
                    and message.embeds
                    and len(message.embeds) > 0
                    and "Deadlock Match-Coaching" in (message.embeds[0].title or "")
                ):
                    return message
            return None
        except Exception:
            return None

    # ---------- UI Components ----------
    class MatchIDModal(Modal):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(title="Match ID Eingeben")
            self.cog = cog
            self.match_id = TextInput(label="Match ID", placeholder="z.B. 12345-ABCDE", max_length=50)
            self.add_item(self.match_id)

        async def on_submit(self, interaction: discord.Interaction):
            user_id = interaction.user.id
            self.cog.user_data.setdefault(user_id, {})
            self.cog.user_data[user_id]["match_id"] = str(self.match_id.value)

            # Reagiere schnell, dann arbeite weiter (vermeidet Interaktions-Timeout)
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
            except Exception:
                pass

            # Thread erstellen und erste View senden
            thread = await interaction.channel.create_thread(
                name=f"Match-Coaching: {interaction.user.display_name}",
                type=discord.ChannelType.private_thread,
            )

            self.cog.active_threads[user_id] = thread.id
            self.cog.user_data[user_id]["thread_id"] = thread.id
            self.cog.thread_last_activity[thread.id] = datetime.datetime.now()
            try:
                await thread.add_user(interaction.user)
            except Exception:
                pass

            emb = discord.Embed(
                title="Deadlock Match-Coaching",
                description=f"Match-ID: {self.match_id.value}\n\nBitte wähle deinen Rang.",
                color=discord.Color.blue(),
            )
            await thread.send(embed=emb, view=DlCoachingCog.RankView(self.cog, thread.guild))

            # Bestätigung als Followup
            try:
                await interaction.followup.send(f"Thread erstellt: {thread.mention}", ephemeral=True)
            except Exception:
                pass

    class StartView(View):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(timeout=None)
            self.cog = cog

        @discord.ui.button(label="Match-Coaching starten", style=discord.ButtonStyle.primary, custom_id="dl_start")
        async def start(self, interaction: discord.Interaction, button: Button):
            await interaction.response.send_modal(DlCoachingCog.MatchIDModal(self.cog))

class RankSelect(Select):
        def __init__(self, cog: "DlCoachingCog", guild: discord.Guild | None):
            self.cog = cog
            self.guild = guild
            options = []
            for key, mention in cog.RANKS.items():
                em = DlCoachingCog._safe_option_emoji(guild, mention)
                options.append(discord.SelectOption(label=key.title(), value=key, emoji=em))
            super().__init__(placeholder="Wähle deinen Rang", min_values=1, max_values=1, options=options, custom_id="dl_rank")

        async def callback(self, interaction: discord.Interaction):
            uid = interaction.user.id
            d = self.cog.user_data.setdefault(uid, {})
            d["rank"] = self.values[0]
            d["step"] = "subrank"
            if d.get("thread_id"):
                self.cog.thread_last_activity[d["thread_id"]] = datetime.datetime.now()
            emb = discord.Embed(
                title="Deadlock Match-Coaching",
                description=f"Match-ID: {d.get('match_id')}\nRang: {d['rank']} {self.cog.RANKS.get(d['rank'],'')}\n\nBitte wähle deinen Subrang.",
                color=discord.Color.blue(),
            )
            await interaction.response.edit_message(embed=emb, view=DlCoachingCog.SubrankView(self.cog))

class RankView(View):
        def __init__(self, cog: "DlCoachingCog", guild: discord.Guild | None):
            super().__init__(timeout=None)
            self.add_item(DlCoachingCog.RankSelect(cog, guild))

    class SubrankSelect(Select):
        def __init__(self, cog: "DlCoachingCog"):
            self.cog = cog
            options = [
                discord.SelectOption(label="I", value="i"),
                discord.SelectOption(label="II", value="ii"),
                discord.SelectOption(label="III", value="iii"),
                discord.SelectOption(label="IV", value="iv"),
                discord.SelectOption(label="V", value="v"),
                discord.SelectOption(label="✶", value="✶"),
            ]
            super().__init__(placeholder="Wähle deinen Subrang", min_values=1, max_values=1, options=options, custom_id="dl_subrank")

        async def callback(self, interaction: discord.Interaction):
            uid = interaction.user.id
            d = self.cog.user_data.setdefault(uid, {})
            d["subrank"] = self.values[0]
            d["step"] = "hero"
            if d.get("thread_id"):
                self.cog.thread_last_activity[d["thread_id"]] = datetime.datetime.now()
            emb = discord.Embed(
                title="Deadlock Match-Coaching",
                description=f"Match-ID: {d.get('match_id')}\nRang: {d.get('rank')} {self.cog.RANKS.get(d.get('rank'),'')}\nSubrang: {d['subrank']}\n\nBitte wähle deinen Helden.",
                color=discord.Color.blue(),
            )
            await interaction.response.edit_message(embed=emb, view=DlCoachingCog.HeroView(self.cog, interaction.guild))

    class SubrankView(View):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(timeout=None)
            self.add_item(DlCoachingCog.SubrankSelect(cog))

    class HeroSelectPage1(Select):
        def __init__(self, cog: "DlCoachingCog", guild: discord.Guild | None):
            self.cog = cog
            options = []
            for name, mention in cog.HEROES_PAGE_1.items():
                em = DlCoachingCog._safe_option_emoji(guild, mention)
                options.append(discord.SelectOption(label=name.replace('_',' ').title(), value=name, emoji=em))
            super().__init__(placeholder="Helden (A–M)", min_values=1, max_values=1, options=options, custom_id="dl_hero_p1")

        async def callback(self, interaction: discord.Interaction):
            await DlCoachingCog._hero_selected(self.cog, interaction, self.values[0])

    class HeroSelectPage2(Select):
        def __init__(self, cog: "DlCoachingCog", guild: discord.Guild | None):
            self.cog = cog
            options = []
            for name, mention in cog.HEROES_PAGE_2.items():
                em = DlCoachingCog._safe_option_emoji(guild, mention)
                options.append(discord.SelectOption(label=name.replace('_',' ').title(), value=name, emoji=em))
            super().__init__(placeholder="Helden (N–Z)", min_values=1, max_values=1, options=options, custom_id="dl_hero_p2")

        async def callback(self, interaction: discord.Interaction):
            await DlCoachingCog._hero_selected(self.cog, interaction, self.values[0])

    class HeroView(View):
        def __init__(self, cog: "DlCoachingCog", guild: discord.Guild | None):
            super().__init__(timeout=None)
            self.add_item(DlCoachingCog.HeroSelectPage1(cog, guild))
            self.add_item(DlCoachingCog.HeroSelectPage2(cog, guild))

    @staticmethod
    async def _hero_selected(cog: "DlCoachingCog", interaction: discord.Interaction, hero_value: str):
        uid = interaction.user.id
        d = cog.user_data.setdefault(uid, {})
        d["hero"] = hero_value
        d["step"] = "comment"
        if d.get("thread_id"):
            cog.thread_last_activity[d["thread_id"]] = datetime.datetime.now()
        emb = discord.Embed(
            title="Deadlock Match-Coaching",
            description=(
                f"Match-ID: {d.get('match_id')}\\nRang: {d.get('rank')} {cog.RANKS.get(d.get('rank'),'')}\\nSubrang: {d.get('subrank')}\\nHeld: {d['hero']}\\n\\nKlicke auf den Button, um deinen Kommentar einzugeben."
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.edit_message(embed=emb, view=DlCoachingCog.CommentView(cog))

    class CommentModal(Modal):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(title="Kommentar eingeben")
            self.cog = cog
            self.comment = TextInput(label="Kommentar", style=discord.TextStyle.paragraph, max_length=1000)
            self.add_item(self.comment)

        async def on_submit(self, interaction: discord.Interaction):
            uid = interaction.user.id
            d = self.cog.user_data.setdefault(uid, {})
            d["comment"] = str(self.comment.value)
            d["step"] = "finish"
            if d.get("thread_id"):
                self.cog.thread_last_activity[d["thread_id"]] = datetime.datetime.now()

            emb = discord.Embed(title="Zusammenfassung deines Coachings", color=discord.Color.green())
            emb.add_field(name="Match ID", value=d.get("match_id"), inline=False)
            emb.add_field(name="Rang", value=f"{d.get('rank')} {self.cog.RANKS.get(d.get('rank'),'')}", inline=False)
            emb.add_field(name="Subrang", value=d.get("subrank"), inline=False)
            emb.add_field(name="Held", value=d.get("hero"), inline=False)
            emb.add_field(name="Kommentar", value=d.get("comment", "-"), inline=False)
            await interaction.response.send_message(embed=emb, view=DlCoachingCog.FinishView(self.cog))

    class CommentView(View):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(timeout=None)
            self.cog = cog

        @discord.ui.button(label="Kommentar eingeben", style=discord.ButtonStyle.primary, custom_id="dl_comment")
        async def open_comment(self, interaction: discord.Interaction, button: Button):
            await interaction.response.send_modal(DlCoachingCog.CommentModal(self.cog))

    class FinishView(View):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(timeout=None)
            self.cog = cog

        @discord.ui.button(label="Abschließen", style=discord.ButtonStyle.success, custom_id="dl_finish")
        async def finish(self, interaction: discord.Interaction, button: Button):
            uid = interaction.user.id
            d = self.cog.user_data.get(uid)
            if not d:
                await interaction.response.send_message("Keine Daten gefunden.", ephemeral=True)
                return
            channel = interaction.channel
            if not isinstance(channel, (discord.Thread, discord.TextChannel)):
                await interaction.response.send_message("Ungültiger Kanal.", ephemeral=True)
                return
            content = (
                f"**Match-Coaching**\n\n"
                f"**Match ID:** {d.get('match_id')}\n"
                f"**Rang:** {d.get('rank')} {self.cog.RANKS.get(d.get('rank',''), '')}\n"
                f"**Subrang:** {d.get('subrank')}\n"
                f"**Held:** {d.get('hero')}\n"
                f"**Kommentar:** {d.get('comment', '-')}\n\n"
                f"_______________________________\n"
                f"Analysiert von: <@{uid}>\n"
                f"Coaching abgeschlossen! Danke für deine Eingaben."
            )
            try:
                if not interaction.response.is_done():
                    await interaction.response.edit_message(view=None)
            except Exception:
                pass
            await channel.send(content)

            notif = {
                "thread_id": channel.id,
                "match_id": d.get("match_id"),
                "rank": d.get("rank"),
                "subrank": d.get("subrank"),
                "hero": d.get("hero"),
                "user_id": uid,
            }
            self.cog._notify_claim_bot(notif)

            self.cog.user_data.pop(uid, None)
            self.cog.active_threads.pop(uid, None)
            self.cog.thread_last_activity.pop(getattr(channel, 'id', None), None)
            try:
                await interaction.message.delete()
            except Exception:
                pass

    # ---------- Flow starters ----------
    async def _start_analysis_in_thread(self, thread: discord.Thread, user: discord.User | discord.Member, match_id: str):
        uid = user.id
        self.user_data.setdefault(uid, {})
        self.user_data[uid]["match_id"] = match_id
        self.user_data[uid]["thread_id"] = thread.id
        self.user_data[uid]["step"] = "rank"

        emb = discord.Embed(
            title="Deadlock Match-Coaching",
            description=f"Match-ID: {match_id}\n\nBitte wähle deinen Rang.",
            color=discord.Color.blue(),
        )
        await thread.send(embed=emb, view=DlCoachingCog.RankView(self))
        self.thread_last_activity[thread.id] = datetime.datetime.now()

    # ---------- Timeout loop ----------
    def _build_timeout_loop(self):
        @tasks.loop(seconds=1)
        async def _loop():
            now = datetime.datetime.now()
            TIMEOUT_SECONDS = 600
            to_close: list[int] = []
            for thread_id, last in list(self.thread_last_activity.items()):
                if (now - last).total_seconds() > TIMEOUT_SECONDS:
                    to_close.append(thread_id)
            for thread_id in to_close:
                try:
                    channel = self.bot.get_channel(thread_id)
                    if channel and isinstance(channel, discord.Thread):
                        await channel.send("Timeout erreicht. Thread wird geschlossen.")
                        await channel.edit(archived=True, locked=True)
                    self.thread_last_activity.pop(thread_id, None)
                    for uid, data in list(self.user_data.items()):
                        if data.get("thread_id") == thread_id:
                            self.user_data.pop(uid, None)
                            self.active_threads.pop(uid, None)
                except Exception:
                    pass
        return _loop

    # ---------- Events ----------
    @commands.Cog.listener()
    async def on_ready(self):
        if not self._timeout_loop.is_running():
            self._timeout_loop.start()
        # Persistente Views für Reloads/Restarts registrieren
        self._register_persistent_views()
        channel = self.bot.get_channel(self.CHANNEL_ID)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                coaching_message = await self._find_coaching_message_in_channel(channel) if isinstance(channel, discord.TextChannel) else None
                if coaching_message:
                    await coaching_message.edit(view=DlCoachingCog.StartView(self))
            except Exception:
                pass

    def cog_load(self) -> None:
        # Wird auch bei Reload aufgerufen – Views registrieren und Bestandsnachricht später anhängen
        try:
            self._register_persistent_views()
            # Bestandsnachricht asynchron aktualisieren
            async def _reattach():
                await self.bot.wait_until_ready()
                ch = self.bot.get_channel(self.CHANNEL_ID)
                if isinstance(ch, discord.TextChannel):
                    try:
                        msg = await self._find_coaching_message_in_channel(ch)
                        if msg:
                            await msg.edit(view=DlCoachingCog.StartView(self))
                    except Exception:
                        pass
            self.bot.loop.create_task(_reattach())
        except Exception:
            pass

    def _register_persistent_views(self) -> None:
        try:
            # Nur StartView registrieren (persistente Interaktion). Die weiteren Views
            # werden kontextbezogen im Thread mit Guild-gebundenen Emojis erzeugt.
            self.bot.add_view(DlCoachingCog.StartView(self))
        except Exception:
            pass

    def cog_unload(self) -> None:
        try:
            if self._timeout_loop.is_running():
                self._timeout_loop.cancel()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(DlCoachingCog(bot))




