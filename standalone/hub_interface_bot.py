import os
import logging
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

# ========= Grundsetup =========
ENV_PATH = r"C:\Users\Nani-Admin\Documents\.env"
load_dotenv(ENV_PATH)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError(f"DISCORD_TOKEN fehlt in {ENV_PATH}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("hub-bot")

# Kategorien (kannst du anpassen)
CATEGORY_FUN    = "Entspannte Lanes"
CATEGORY_GRIND  = "Grind Lanes"
CATEGORY_RANK   = "Ranked Lanes"
CATEGORY_NEWBIE = "Neue Spieler"

# Deadlock Ränge (Anzeigereihenfolge)
RANKS = [
    "Obscurus", "Initiate", "Seeker", "Alchemist", "Arcanist", "Ritualist",
    "Emissary", "Archon", "Oracle", "Phantom", "Ascendant", "Eternus"
]
RANK_NAME_TO_VAL = {n: i for i, n in enumerate(RANKS)}

# ========= Helpers =========
async def get_or_create_category(guild: discord.Guild, name: str) -> discord.CategoryChannel:
    cat = discord.utils.get(guild.categories, name=name)
    if cat:
        return cat
    log.info("Erzeuge Kategorie: %s", name)
    return await guild.create_category(name=name, reason="Hub Interface Auto-Setup")

def build_channel_name(kind: str, limit: int, rank_min: str, rank_max: str, mode: str) -> str:
    tag = {
        "fun": "Spaß",
        "grind": "Grind",
        "rank": "Rank",
        "newbie": "Newbie",
    }.get(kind, "Lane")
    rank_span = f"{rank_min}-{rank_max}" if rank_min and rank_max else "alle"
    return f"{tag} • {mode} • {limit} • {rank_span}".replace(" ", "")

async def create_lane_voice(
    guild: discord.Guild,
    *,
    kind: str,
    limit: int,
    rank_min: Optional[str],
    rank_max: Optional[str],
    mode: str,
    author: discord.Member,
) -> discord.VoiceChannel:
    if kind == "fun":
        cat = await get_or_create_category(guild, CATEGORY_FUN)
    elif kind == "grind":
        cat = await get_or_create_category(guild, CATEGORY_GRIND)
    elif kind == "rank":
        cat = await get_or_create_category(guild, CATEGORY_RANK)
    else:
        cat = await get_or_create_category(guild, CATEGORY_NEWBIE)

    name = build_channel_name(kind, limit, rank_min or "", rank_max or "", mode)
    ch = await guild.create_voice_channel(
        name=name,
        user_limit=limit,
        category=cat,
        reason=f"Lane via Hub von {author} erstellt",
    )
    return ch

async def move_user_to_first_live_stream(guild: discord.Guild, member: discord.Member) -> Optional[discord.VoiceChannel]:
    for vc in guild.voice_channels:
        for m in vc.members:
            vs = m.voice
            if vs and getattr(vs, "self_stream", False):
                try:
                    await member.move_to(vc, reason="Zuschauen: Live-Stream gefunden")
                    return vc
                except discord.Forbidden:
                    log.warning("Keine Rechte, %s nach %s zu moven", member, vc.name)
                except discord.HTTPException as e:
                    log.error("Move HTTPException: %s", e)
    return None

# ========= UI: Formular-View mit Dropdowns =========
class ModeSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Modus wählen …",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Solo"),
                discord.SelectOption(label="Duo"),
                discord.SelectOption(label="Stack"),
                discord.SelectOption(label="Neue Helden lernen"),
                discord.SelectOption(label="Spiel kennenlernen"),
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: "FormView" = self.view  # type: ignore
        view.mode = self.values[0]
        await interaction.response.edit_message(view=view)

class MaxUsersSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Max. Nutzer (2–8) …",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=str(i)) for i in range(2, 9)],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        view: "FormView" = self.view  # type: ignore
        view.max_users = int(self.values[0])
        await interaction.response.edit_message(view=view)

class RankMinSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Min-Rang …",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=r) for r in RANKS],
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        view: "FormView" = self.view  # type: ignore
        view.rank_min = self.values[0]
        # Wenn min > max → max leeren
        if view.rank_max and RANK_NAME_TO_VAL[view.rank_min] > RANK_NAME_TO_VAL[view.rank_max]:
            view.rank_max = None
        await interaction.response.edit_message(view=view)

class RankMaxSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Max-Rang …",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=r) for r in RANKS],
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        view: "FormView" = self.view  # type: ignore
        candidate = self.values[0]
        # Validierung: max >= min (falls min gesetzt)
        if view.rank_min and RANK_NAME_TO_VAL[candidate] < RANK_NAME_TO_VAL[view.rank_min]:
            await interaction.response.send_message(
                "⚠️ Max-Rang darf nicht unter Min-Rang liegen.",
                ephemeral=True
            )
            return
        view.rank_max = candidate
        await interaction.response.edit_message(view=view)

class FormView(discord.ui.View):
    def __init__(self, kind: str):
        super().__init__(timeout=180)
        self.kind = kind  # fun | grind | rank | newbie
        # State
        self.mode: Optional[str] = None
        self.max_users: int = 4
        self.rank_min: Optional[str] = None
        self.rank_max: Optional[str] = None

        # Items (max 5 ActionRows: 3 Selects + 1 Select + 2 Buttons -> wir legen 2 Buttons in eine Row)
        self.add_item(ModeSelect())        # row 0
        self.add_item(MaxUsersSelect())    # row 1
        self.add_item(RankMinSelect())     # row 2
        self.add_item(RankMaxSelect())     # row 3

        # Buttons (row 4)
        self.confirm_button = discord.ui.Button(label="Erstellen", style=discord.ButtonStyle.success, row=4)
        self.cancel_button  = discord.ui.Button(label="Abbrechen", style=discord.ButtonStyle.secondary, row=4)
        self.confirm_button.callback = self.on_confirm
        self.cancel_button.callback  = self.on_cancel
        self.add_item(self.confirm_button)
        self.add_item(self.cancel_button)

    async def on_confirm(self, interaction: discord.Interaction):
        if not self.mode:
            await interaction.response.send_message("Bitte zuerst einen **Modus** wählen.", ephemeral=True)
            return

        guild = interaction.guild
        user = interaction.user
        assert guild and isinstance(user, discord.Member)

        ch = await create_lane_voice(
            guild,
            kind=self.kind,
            limit=int(self.max_users),
            rank_min=self.rank_min,
            rank_max=self.rank_max,
            mode=self.mode,
            author=user,
        )
        try:
            if user.voice and user.voice.channel:
                await user.move_to(ch, reason="Eigener Lane-Channel erstellt")
        except discord.Forbidden:
            pass

        await interaction.response.edit_message(
            content=f"✅ **{ch.name}** wurde erstellt in **{ch.category.name}**.",
            embed=None,
            view=None
        )

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Abgebrochen.", embed=None, view=None)

# ========= Hub View (Hauptmenü) =========
class HubView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Spaß haben", style=discord.ButtonStyle.primary, emoji="🎉")
    async def btn_fun(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "🎉 Einstellungen für **Spaß haben**:",
            view=FormView("fun"),
            ephemeral=True
        )

    @discord.ui.button(label="Grinden", style=discord.ButtonStyle.primary, emoji="⚔️")
    async def btn_grind(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "⚔️ Einstellungen für **Grinden**:",
            view=FormView("grind"),
            ephemeral=True
        )

    @discord.ui.button(label="Ranken", style=discord.ButtonStyle.primary, emoji="🏆")
    async def btn_rank(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "🏆 Einstellungen für **Ranken**:",
            view=FormView("rank"),
            ephemeral=True
        )

    @discord.ui.button(label="Neu im Spiel", style=discord.ButtonStyle.primary, emoji="🧭")
    async def btn_newbie(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            "🧭 Einstellungen für **Neu im Spiel**:",
            view=FormView("newbie"),
            ephemeral=True
        )

    @discord.ui.button(label="Zuschauen", style=discord.ButtonStyle.secondary, emoji="👀")
    async def btn_watch(self, interaction: discord.Interaction, _: discord.ui.Button):
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        vc = await move_user_to_first_live_stream(interaction.guild, interaction.user)
        if vc:
            await interaction.response.send_message(f"📺 Live-Stream gefunden: **{vc.name}** – dich rübergezogen.", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ Aktuell kein Live-Stream gefunden.", ephemeral=True)

# ========= Bot =========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    bot.add_view(HubView())  # persistent buttons
    log.info("Angemeldet als %s (%s)", bot.user, bot.user.id)

@bot.command(name="hub", help="Postet das Hub-Interface in diesen Kanal.")
async def hub_command(ctx: commands.Context):
    embed = discord.Embed(
        title="Was möchtest du heute tun?",
        description=(
            "🎉 **Spaß haben** – neue Dinge lernen oder einfach nur bissle spielen\n"
            "⚔️ **Grinden** – ohne Trash-Talking, mit Winning-Absicht\n"
            "🏆 **Ranken** – explizit mit Leuten aus meinem Rang\n"
            "🧭 **Neu im Spiel** – Spiel kennenlernen / Neue Helden lernen\n"
            "👀 **Zuschauen** – in aktiven Stream-Channel moven"
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(
        name="So geht’s",
        value="Klick eine Option → wähle **Modus**, **Max. Nutzer**, **Min/Max-Rang** → **Erstellen**.",
        inline=False
    )
    await ctx.send(embed=embed, view=HubView())

@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send(f"Pong! ({round(bot.latency * 1000)} ms)")

if __name__ == "__main__":
    bot.run(TOKEN)
