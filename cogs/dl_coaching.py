import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Modal, TextInput
import asyncio
import datetime
import json
import re
import socket
from pathlib import Path


class DlCoachingCog(commands.Cog):
    """DL Coaching System als vollwertiger Cog (portiert aus original_scripts/dl_coaching.py)."""

    # IDs anpassen, falls erforderlich
    CHANNEL_ID = 1357421075188813897
    EXISTING_MESSAGE_ID = 1383883328385454210

    SOCKET_HOST = "localhost"
    SOCKET_PORT = 45678

    # Deadlock-Ränge mit Emoji-IDs
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

        # Datenpfade
        self.base_dir = Path(__file__).resolve().parent.parent
        self.data_dir = self.base_dir / "coaching_data"
        self.logs_dir = self.base_dir / "logs"
        self._ensure_directories()
        self._create_default_files()

        # Temporäre Speicherung der Nutzerdaten
        self.user_data: dict[int, dict] = {}
        self.active_threads: dict[int, int] = {}
        self.thread_last_activity: dict[int, datetime.datetime] = {}

        # Background loop
        self._check_loop = self._build_check_loop()

    # ---------- FS helpers ----------
    def _ensure_directories(self) -> None:
        for d in (self.data_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _create_default_files(self) -> None:
        defaults = [
            (self.data_dir / "user_data.json", "{}"),
            (self.data_dir / "active_threads.json", "{}"),
            (self.logs_dir / "coaching_logs.txt", ""),
        ]
        for path, content in defaults:
            if not path.exists():
                path.write_text(content, encoding="utf-8")

    # ---------- Utilities ----------
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

    # ---------- UI Classes ----------
    class MatchIDModal(Modal):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(title="Match ID Eingabe")
            self.cog = cog
            self.match_id = TextInput(
                label="Bitte gib die Match ID ein",
                placeholder="Match ID hier eingeben...",
                max_length=20,
            )
            self.add_item(self.match_id)

        async def on_submit(self, interaction: discord.Interaction):
            user_id = interaction.user.id
            if user_id not in self.cog.user_data:
                self.cog.user_data[user_id] = {}

            self.cog.user_data[user_id]["match_id"] = self.match_id.value

            thread = await interaction.channel.create_thread(
                name=f"Match-Coaching: {interaction.user.display_name}",
                type=discord.ChannelType.private_thread,
            )

            self.cog.active_threads[user_id] = thread.id
            self.cog.thread_last_activity[thread.id] = datetime.datetime.now()
            await thread.add_user(interaction.user)

            await interaction.response.send_message(
                f"Ein Thread für dein Match-Coaching wurde erstellt. Bitte gehe zu {thread.mention}.",
                ephemeral=True,
            )

            await self.cog._start_analysis_in_thread(thread, interaction.user, self.match_id.value)

    class StartView(View):
        def __init__(self, cog: "DlCoachingCog"):
            super().__init__(timeout=None)
            self.cog = cog

            start_button = Button(
                label="Match-Coaching starten",
                style=discord.ButtonStyle.primary,
                custom_id="start_analysis",
            )
            start_button.callback = self.start_analysis
            self.add_item(start_button)

        async def start_analysis(self, interaction: discord.Interaction):
            await interaction.response.send_modal(DlCoachingCog.MatchIDModal(self.cog))

    # ---------- Flow helpers ----------
    async def _start_analysis_in_thread(self, thread: discord.Thread, user: discord.User | discord.Member, match_id: str):
        user_id = user.id
        self.user_data.setdefault(user_id, {})
        self.user_data[user_id]["match_id"] = match_id
        self.user_data[user_id]["step"] = "rank"
        self.user_data[user_id]["thread_id"] = thread.id

        embed = discord.Embed(
            title="Deadlock Match-Coaching",
            description=(
                f"Match-ID: {match_id}\n\nBitte reagiere mit deinem Rang auf diese Nachricht."
            ),
            color=discord.Color.blue(),
        )

        rank_message = await thread.send(embed=embed)
        self.user_data[user_id]["rank_message_id"] = rank_message.id

        for _, emoji_str in self.RANKS.items():
            m = re.search(r"<:([^:]+):(\d+)>", emoji_str)
            if m:
                emoji_id = int(m.group(2))
                emoji = discord.utils.get(thread.guild.emojis, id=emoji_id)
                if emoji:
                    await rank_message.add_reaction(emoji)

        self.thread_last_activity[thread.id] = datetime.datetime.now()

    # ---------- Periodic checks ----------
    def _build_check_loop(self):
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

            # Step checks
            for user_id, data in list(self.user_data.items()):
                step = data.get("step")
                thread_id = data.get("thread_id")
                if not step or not thread_id:
                    continue
                channel = self.bot.get_channel(thread_id)
                if not channel:
                    continue
                try:
                    if step == "rank" and "rank_message_id" in data:
                        msg = await channel.fetch_message(data["rank_message_id"])
                        await self._check_rank_reactions(msg, user_id)
                    elif step == "subrank" and "subrank_message_id" in data:
                        msg = await channel.fetch_message(data["subrank_message_id"])
                        await self._check_subrank_reactions(msg, user_id)
                    elif step == "hero" and "hero_message_id" in data:
                        msg = await channel.fetch_message(data["hero_message_id"])
                        await self._check_hero_reactions(msg, user_id)
                    elif step == "finish" and "summary_message_id" in data:
                        msg = await channel.fetch_message(data["summary_message_id"])
                        await self._check_finish_reactions(msg, user_id)
                except Exception:
                    continue

        return _loop

    async def _check_rank_reactions(self, message: discord.Message, user_id: int):
        if user_id not in self.user_data:
            return
        data = self.user_data[user_id]
        message = await message.channel.fetch_message(message.id)
        async for reaction in message.reactions:
            pass
        for reaction in message.reactions:
            async for user in reaction.users():
                if user.id != user_id:
                    continue
                emoji = reaction.emoji
                selected_rank = None
                for rank, emoji_str in self.RANKS.items():
                    if isinstance(emoji, discord.Emoji) and str(emoji.id) in emoji_str:
                        selected_rank = rank
                        break
                if selected_rank:
                    data["rank"] = selected_rank
                    data["step"] = "subrank"
                    embed = discord.Embed(
                        title="Deadlock Match-Coaching",
                        description=(
                            f"Match-ID: {data['match_id']}\nRang: {selected_rank} {self.RANKS[selected_rank]}\n\nBitte reagiere mit deinem Subrang auf diese Nachricht."
                        ),
                        color=discord.Color.blue(),
                    )
                    subrank_message = await message.channel.send(embed=embed)
                    data["subrank_message_id"] = subrank_message.id
                    # Use numeric emojis 1-5 and star for ✶
                    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "✶"]
                    for idx, sub in enumerate(self.SUBRANKS):
                        await subrank_message.add_reaction(number_emojis[idx])
                    self.thread_last_activity[message.channel.id] = datetime.datetime.now()
                    return

    async def _check_subrank_reactions(self, message: discord.Message, user_id: int):
        if user_id not in self.user_data:
            return
        data = self.user_data[user_id]
        message = await message.channel.fetch_message(message.id)
        number_map = {"1️⃣": "i", "2️⃣": "ii", "3️⃣": "iii", "4️⃣": "iv", "5️⃣": "v", "✶": "✶"}
        for reaction in message.reactions:
            async for user in reaction.users():
                if user.id != user_id:
                    continue
                emoji = str(reaction.emoji)
                if emoji in number_map:
                    data["subrank"] = number_map[emoji]
                    data["step"] = "hero"
                    embed = discord.Embed(
                        title="Deadlock Match-Coaching",
                        description=(
                            f"Match-ID: {data['match_id']}\nRang: {data['rank']} {self.RANKS[data['rank']]}\nSubrang: {data['subrank']}\n\nBitte reagiere mit deinem Helden auf diese Nachricht (Seite 1/2)."
                        ),
                        color=discord.Color.blue(),
                    )
                    hero_message = await message.channel.send(embed=embed)
                    data["hero_message_id"] = hero_message.id
                    data["hero_page"] = 1
                    for _, emoji_str in self.HEROES_PAGE_1.items():
                        m = re.search(r"<:([^:]+):(\d+)>", emoji_str)
                        if m:
                            emoji = discord.utils.get(message.guild.emojis, id=int(m.group(2)))
                            if emoji:
                                await hero_message.add_reaction(emoji)
                    await hero_message.add_reaction("➡️")
                    self.thread_last_activity[message.channel.id] = datetime.datetime.now()
                    return

    async def _check_hero_reactions(self, message: discord.Message, user_id: int):
        if user_id not in self.user_data:
            return
        data = self.user_data[user_id]
        message = await message.channel.fetch_message(message.id)
        for reaction in message.reactions:
            async for user in reaction.users():
                if user.id != user_id:
                    continue
                emoji = reaction.emoji
                # Pagination
                if str(emoji) == "➡️" and data.get("hero_page") == 1:
                    await message.clear_reactions()
                    embed = discord.Embed(
                        title="Deadlock Match-Coaching",
                        description=(
                            f"Match-ID: {data['match_id']}\nRang: {data['rank']} {self.RANKS[data['rank']]}\nSubrang: {data['subrank']}\n\nBitte reagiere mit deinem Helden auf diese Nachricht (Seite 2/2)."
                        ),
                        color=discord.Color.blue(),
                    )
                    await message.edit(embed=embed)
                    data["hero_page"] = 2
                    for _, emoji_str in self.HEROES_PAGE_2.items():
                        m = re.search(r"<:([^:]+):(\d+)>", emoji_str)
                        if m:
                            emoji = discord.utils.get(message.guild.emojis, id=int(m.group(2)))
                            if emoji:
                                await message.add_reaction(emoji)
                    await message.add_reaction("⬅️")
                    await message.remove_reaction("➡️", user)
                    self.thread_last_activity[message.channel.id] = datetime.datetime.now()
                    return
                elif str(emoji) == "⬅️" and data.get("hero_page") == 2:
                    await message.clear_reactions()
                    embed = discord.Embed(
                        title="Deadlock Match-Coaching",
                        description=(
                            f"Match-ID: {data['match_id']}\nRang: {data['rank']} {self.RANKS[data['rank']]}\nSubrang: {data['subrank']}\n\nBitte reagiere mit deinem Helden auf diese Nachricht (Seite 1/2)."
                        ),
                        color=discord.Color.blue(),
                    )
                    await message.edit(embed=embed)
                    data["hero_page"] = 1
                    for _, emoji_str in self.HEROES_PAGE_1.items():
                        m = re.search(r"<:([^:]+):(\d+)>", emoji_str)
                        if m:
                            emoji = discord.utils.get(message.guild.emojis, id=int(m.group(2)))
                            if emoji:
                                await message.add_reaction(emoji)
                    await message.add_reaction("➡️")
                    await message.remove_reaction("⬅️", user)
                    self.thread_last_activity[message.channel.id] = datetime.datetime.now()
                    return
                else:
                    selected_hero = None
                    heroes = self.HEROES_PAGE_1 if data.get("hero_page") == 1 else self.HEROES_PAGE_2
                    for hero, emoji_str in heroes.items():
                        if isinstance(emoji, discord.Emoji) and str(emoji.id) in emoji_str:
                            selected_hero = hero
                            break
                    if selected_hero:
                        data["hero"] = selected_hero
                        data["step"] = "comment"
                        embed = discord.Embed(
                            title="Deadlock Match-Coaching",
                            description=(
                                f"Match-ID: {data['match_id']}\nRang: {data['rank']} {self.RANKS[data['rank']]}\nSubrang: {data['subrank']}\nHeld: {selected_hero} {heroes[selected_hero]}\n\nBitte gib einen Kommentar zur Spielsituation ein (**antworte auf diese Nachricht**)."
                            ),
                            color=discord.Color.blue(),
                        )
                        comment_message = await message.channel.send(embed=embed)
                        data["comment_message_id"] = comment_message.id
                        self.thread_last_activity[message.channel.id] = datetime.datetime.now()
                        return

    async def _check_finish_reactions(self, message: discord.Message, user_id: int):
        if user_id not in self.user_data:
            return
        data = self.user_data[user_id]
        message = await message.channel.fetch_message(message.id)
        for reaction in message.reactions:
            async for user in reaction.users():
                if user.id == user_id and str(reaction.emoji) == "✅":
                    channel = message.channel
                    # Clean up previous messages
                    async for old in channel.history(limit=100):
                        try:
                            await old.delete()
                        except discord.HTTPException:
                            pass
                    content = (
                        f"**Match-Coaching**\n\n"
                        f"**Match ID:** {data.get('match_id')}\n"
                        f"**Rang:** {data.get('rank')} {self.RANKS.get(data.get('rank', ''), '')}\n"
                        f"**Subrang:** {data.get('subrank')}\n"
                    )
                    hero = data.get("hero", "")
                    hero_emoji = ""
                    if hero in self.HEROES_PAGE_1:
                        hero_emoji = self.HEROES_PAGE_1[hero]
                    elif hero in self.HEROES_PAGE_2:
                        hero_emoji = self.HEROES_PAGE_2[hero]
                    content += f"**Held:** {hero} {hero_emoji}\n"
                    content += f"**Kommentar:** {data.get('comment', 'Kein Kommentar angegeben.')}\n\n"
                    content += f"_______________________________\n"
                    content += f"Analysiert von: <@{user_id}>\n"
                    content += f"Coaching abgeschlossen! Danke für deine Eingaben."
                    await channel.send(content)

                    notif = {
                        "thread_id": channel.id,
                        "match_id": data.get("match_id"),
                        "rank": data.get("rank"),
                        "subrank": data.get("subrank"),
                        "hero": data.get("hero"),
                        "user_id": user_id,
                    }
                    self._notify_claim_bot(notif)

                    self.user_data.pop(user_id, None)
                    self.active_threads.pop(user_id, None)
                    self.thread_last_activity.pop(message.channel.id, None)
                    return

    # ---------- Events ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.reference and message.reference.message_id:
            user_id = message.author.id
            if user_id in self.user_data and self.user_data[user_id].get("step") == "comment":
                data = self.user_data[user_id]
                channel = message.channel
                data["comment"] = message.content

                embed = discord.Embed(title="Zusammenfassung deines Coachings", color=discord.Color.green())
                embed.add_field(name="Match ID", value=data.get("match_id"), inline=False)
                embed.add_field(
                    name="Rang",
                    value=f"{data.get('rank')} {self.RANKS.get(data.get('rank', ''), '')}",
                    inline=False,
                )
                embed.add_field(name="Subrang", value=data.get("subrank"), inline=False)
                hero = data.get("hero", "")
                hero_emoji = ""
                if hero in self.HEROES_PAGE_1:
                    hero_emoji = self.HEROES_PAGE_1[hero]
                elif hero in self.HEROES_PAGE_2:
                    hero_emoji = self.HEROES_PAGE_2[hero]
                embed.add_field(name="Held", value=f"{hero} {hero_emoji}", inline=False)
                embed.add_field(
                    name="Kommentar", value=data.get("comment", "Kein Kommentar angegeben."), inline=False
                )
                summary_message = await channel.send(embed=embed)
                data["summary_message_id"] = summary_message.id
                await summary_message.add_reaction("✅")
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                self.thread_last_activity[channel.id] = datetime.datetime.now()
        # HINWEIS: Kein process_commands() Aufruf hier, um Doppel-Ausführung zu vermeiden

    @commands.Cog.listener()
    async def on_ready(self):
        if not self._check_loop.is_running():
            self._check_loop.start()
        channel = self.bot.get_channel(self.CHANNEL_ID)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                coaching_message = await self._find_coaching_message_in_channel(channel) if isinstance(channel, discord.TextChannel) else None
                if coaching_message:
                    await coaching_message.edit(view=DlCoachingCog.StartView(self))
            except Exception:
                pass

    def cog_unload(self) -> None:
        try:
            if self._check_loop.is_running():
                self._check_loop.cancel()
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(DlCoachingCog(bot))
