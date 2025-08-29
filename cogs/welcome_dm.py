import discord
from discord.ext import commands
import asyncio
import logging

logger = logging.getLogger(__name__)

class WelcomeDM(commands.Cog):
    """Cog für automatische Willkommensnachrichten per DM"""
    
    def __init__(self, bot):
        self.bot = bot
    
    @commands.Cog.listener()
    async def on_ready(self):
        print("✅ Welcome DM System geladen")
    
    async def send_welcome_messages(self, member):
        """Sendet Willkommensnachrichten an einen User - Original-Text mit korrekten Links"""
        
        # Original-Nachricht aufgeteilt - 2k Zeichen optimal nutzen
        messages = [
            # Nachricht 1: Begrüßung + Regelwerk (~1900 Zeichen)
            "👋 **Willkommen bei Deadlock DACH** 🎮\n\n"
            "Herzlich willkommen beim größten deutschsprachigen Deadlock Discord! "
            "Hier findest du Mitspieler, Guides, Patchnotes – und eine aktive Community.\n\n"
            "________________________________________\n\n"
            "📜 **Regelwerk – Das Wichtigste in Kürze**\n\n"
            "✔ Respektvoller Umgang – keine Beleidigungen oder persönlichen Angriffe\n"
            "✔ Null Toleranz bei Rassismus, Sexismus oder Hassrede\n"
            "✔ Keine NSFW / expliziten Inhalte\n"
            "✔ Privatsphäre respektieren – keine fremden Daten leaken\n"
            "✔ Kein Spam / unnötige Pings\n"
            "✔ Keine Fremdwerbung oder Schadsoftware\n\n"
            "👉 **Universalregel: Sei kein Arschloch.**\n"
            "👉 Vollständiges Regelwerk findest du in: https://discord.com/channels/1289721245281292288/1315684135175716975\n\n"
            "________________________________________",
            
            # Nachricht 2: Gaming Modi (~1950 Zeichen) 
            "\n🏆 **Unterschied Casual vs. Ranked**\n\n"
            "• **Casual Games** 🎉\n"
            "→ Lockeres Spielen ohne Druck\n"
            "→ Ideal zum Kennenlernen & Spaß haben\n"
            "→ Nutze dafür die Casual Lanes\n\n"
            "• **Ranked Games (Grind)** ⚔️\n"
            "→ Fokus auf Teamplay, Strategie & besser werden\n"
            "→ Perfekt zum Elo pushen mit ambitionierten Mates\n"
            "→ Nutze dafür die Rank Grind Lanes\n"
            "→ Wähle hier deine Rangrolle: https://discord.com/channels/1289721245281292288/1398021105339334666\n\n"
            "💡 **Empfehlung:** Starte erst Casual, lerne Leute kennen – danach geh ins Ranked.\n\n"
            "________________________________________\n\n"
            "🎮 **Custom Games**\n\n"
            "**Was sind Custom Games?**\n"
            "Customs sind selbsterstellte Lobbys, die nichts mit dem normalen Matchmaking zu tun haben. "
            "Hier legen wir eigene Regeln fest → Fokus auf Spaß, Lernen oder gemeinsames Training.\n\n"
            "**Dafür gibt es 2 Rollen:**\n"
            "• @Funny Custom Ping → Für Fun & kreative Custom-Runden 🤪\n"
            "• @Grind Custom Ping → Für Scrims & ernsthafte Trainings 💪\n\n"
            "➡ Über Reaktionen kannst du dir die Rolle(n) selbst geben, wenn du mitmachen willst.\n\n"
            "________________________________________",
            
            # Nachricht 3: Info-Kanäle + Mehrwert (~1850 Zeichen)
            "\n📢 **Wichtige Info-Kanäle**\n\n"
            "• https://discord.com/channels/1289721245281292288/1326973956825284628 → Offizielle Deadlock Patchnotes (übersetzt auf Deutsch)\n"
            "• https://discord.com/channels/1289721245281292288/1371952264620806214 → Neuigkeiten & Infos rund um den Server\n\n"
            "👉 **Hinweis:** Beide Channels sind nur zum Lesen – nicht zum Schreiben.\n\n"
            "________________________________________\n\n"
            "🗣️ **Finde deine Mitspieler & Mehrwert**\n\n"
            "• https://discord.com/channels/1289721245281292288/1326975033838665803 → Lernressourcen & Pro-Tipps\n"
            "• https://discord.com/channels/1289721245281292288/1376335502919335936 → Such dir Mitspieler für Casual oder Ranked\n"
            "• https://discord.com/channels/1289721245281292288/1304169815505637458 → Deutsche Deadlock-Streamer live\n"
            "• https://discord.com/channels/1289721245281292288/1407407213953286258 → Deadlock Leaks & Neuigkeiten\n"
            "• https://discord.com/channels/1289721245281292288/1357421075188813897 → Match-Reviews & Tipps, um deinen Skill zu verbessern\n\n"
            "________________________________________",
            
            # Nachricht 4: Support + Moderation + Abschluss (~1900 Zeichen)
            "\n💎 **Support & Extras**\n\n"
            "• **Nitro-Boosts:** Wenn du den Server boosten willst → gibt dir VIP-Vorteile + "
            "hilft der Community mit besseren Features (z. B. Banner, Audioqualität & Emotes).\n\n"
            "• **Beta-Zugang / Keys:** Falls du noch einen Key brauchst oder welchen vergeben willst "
            "→ frag gern im https://discord.com/channels/1289721245281292288/ nach.\n\n"
            "________________________________________\n\n"
            "⚔️ **Moderation & Hilfe**\n\n"
            "• Probleme? → Wende dich an @Moderatoren oder @Owner\n"
            "• Regelverstöße = Verwarnung, Timeout oder Bann (ggf. ohne Vorwarnung)\n"
            "• Feedback oder Ideen? → direkt an @earlysalty\n\n"
            "________________________________________\n\n"
            "✅ **Kurz gesagt**\n"
            "Dieser Server lebt davon, dass alle aktiv mitmachen:\n"
            "👉 **Sei aktiv, such Mitspieler, bring dich ein – dann macht's am meisten Spaß!**\n\n"
            "**Willkommen bei Deadlock DACH – let's go!** 🚀"
        ]
        
        # Nachrichten senden (nur noch 4 statt 8 - optimiert für ~2k Zeichen)
        for i, message in enumerate(messages, 1):
            try:
                await member.send(message)
                
                # Nach der 2. Nachricht das Rollen-Bild senden
                if i == 2:  # Nach Custom Games Info
                    try:
                        await asyncio.sleep(0.5)
                        await member.send("https://cdn.discordapp.com/attachments/1374364800817303632/1407474771251167509/D84325BE-A40F-4BEF-ACB8-A19E2F6162E5.png?ex=68a63c87&is=68a4eb07&hm=3c6a6a3d3d3a69b85d6ddd6466241e097bca5922abedd183228bd43b090bff88&")
                        await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning(f"Konnte Rollen-Bild nicht senden: {e}")
                
                # Pause zwischen Nachrichten
                if i < len(messages):
                    await asyncio.sleep(0.5)  # Schnellere Versendung
                    
            except discord.Forbidden:
                logger.warning(f"Konnte keine DM an {member.display_name} ({member.id}) senden - DMs deaktiviert")
                break
            except Exception as e:
                logger.error(f"Fehler beim Senden von Nachricht {i} an {member.display_name}: {e}")
                break
            
        logger.info(f"Willkommensnachrichten an {member.display_name} ({member.id}) gesendet")
        return True

    @commands.Cog.listener()
    async def on_member_join(self, member):
        """Event Handler für neue Mitglieder"""
        try:
            # Warte kurz, damit der User Zeit hat anzukommen
            await asyncio.sleep(2)
            await self.send_welcome_messages(member)
        except Exception as e:
            logger.error(f"Fehler beim Verarbeiten von on_member_join für {member.display_name}: {e}")

    @commands.command(name='testwelcome')
    @commands.has_permissions(administrator=True)
    async def test_welcome(self, ctx, user: discord.Member = None):
        """Testet die Willkommensnachrichten für einen User
        
        Verwendung: !testwelcome @user
        """
        if user is None:
            await ctx.send("❌ Bitte gib einen User an: `!testwelcome @user`")
            return
        
        try:
            await ctx.send(f"📤 Sende Willkommensnachrichten an {user.mention}...")
            success = await self.send_welcome_messages(user)
            
            if success:
                await ctx.send(f"✅ Willkommensnachrichten erfolgreich an {user.mention} gesendet!")
            else:
                await ctx.send(f"⚠️ Fehler beim Senden der Nachrichten an {user.mention}")
                
        except discord.Forbidden:
            await ctx.send(f"❌ {user.mention} hat DMs deaktiviert oder blockiert den Bot")
        except Exception as e:
            await ctx.send(f"❌ Fehler: {str(e)}")
            logger.error(f"Fehler beim Testen der Willkommensnachricht für {user.display_name}: {e}")

async def setup(bot):
    """Setup-Funktion für den Cog"""
    await bot.add_cog(WelcomeDM(bot))