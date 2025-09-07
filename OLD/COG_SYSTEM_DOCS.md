# 🤖 Automatisches Cog-Loading System

## Überblick

Der Main Bot (`main_bot.py`) lädt jetzt **automatisch alle Cogs** aus dem `cogs/` Verzeichnis ohne manuelle Anpassungen. Neue Scripts können einfach hinzugefügt und ohne Bot-Neustart geladen werden.

## ✨ Neue Features

### 🔍 Automatisches Cog-Discovery
- **Auto-Erkennung:** Alle `.py` Dateien im `cogs/` Verzeichnis werden automatisch gefunden
- **Intelligente Filterung:** Nur Dateien mit `setup()` Funktion oder Cog-Klassen werden geladen
- **Keine manuelle Liste:** Der Main Bot muss nie mehr angepasst werden

### 🔄 Hot-Reload System  
- **`!master reloadall`** - Lädt ALLE Cogs neu + entdeckt neue Cogs automatisch
- **`!master discover`** - Entdeckt nur neue Cogs ohne sie zu laden
- **`!master reload [cog]`** - Lädt einzelne Cogs neu
- **Kein Bot-Neustart nötig!**

### 🆕 Neue Cogs hinzufügen

1. **Erstelle neue `.py` Datei** im `cogs/` Verzeichnis
2. **Verwende Template:** Kopiere `cogs/_template.py` als Basis
3. **Lade automatisch:** `!master reloadall` - fertig!

## 📋 Verfügbare Commands

| Command | Beschreibung |
|---------|-------------|
| `!master status` | Zeigt Bot-Status und geladene Cogs |
| `!master reloadall` | Lädt alle Cogs neu + Auto-Discovery |
| `!master discover` | Entdeckt neue Cogs (ohne laden) |
| `!master reload [cog]` | Lädt spezifisches Cog neu |
| `!master shutdown` | Bot beenden |

## 🛠️ Neues Cog erstellen

### 1. Template kopieren
```bash
cp cogs/_template.py cogs/mein_neuer_cog.py
```

### 2. Template anpassen
```python
import discord
from discord.ext import commands
import logging

logger = logging.getLogger(__name__)

class MeinNeuerCog(commands.Cog):
    """Beschreibung deines neuen Cogs"""
    
    def __init__(self, bot):
        self.bot = bot
        # Deine Initialisierung hier
        logger.info(f"✅ {self.__class__.__name__} initialized")
    
    def cog_unload(self):
        # Cleanup beim Entladen
        logger.info(f"🛑 {self.__class__.__name__} unloaded")
    
    @commands.command(name='meincommand')
    async def mein_command(self, ctx):
        """Dein neuer Command"""
        await ctx.send("✅ Mein neuer Command funktioniert!")

# WICHTIG: Setup-Funktion ist erforderlich!
async def setup(bot):
    await bot.add_cog(MeinNeuerCog(bot))
    logger.info("✅ MeinNeuerCog setup complete")
```

### 3. Automatisch laden
```
!master reloadall
```

## 🔧 Cog-Anforderungen

Damit ein Cog automatisch geladen wird, muss es:

1. **Im `cogs/` Verzeichnis** sein
2. **Setup-Funktion haben:** `async def setup(bot):` 
3. **Nicht mit `_` beginnen** (diese werden ignoriert)
4. **Eine Cog-Klasse enthalten:** `class XyzCog(commands.Cog):`

## 📁 Verzeichnis-Struktur

```
Deadlock/
├── main_bot.py              # Main Bot mit Auto-Loading
├── cogs/
│   ├── _template.py         # Template (wird ignoriert)
│   ├── dl_coaching.py       # Bestehende Cogs
│   ├── claim_system.py      # 
│   ├── tempvoice.py         #
│   └── mein_neuer_cog.py   # 🆕 Neues Cog (automatisch geladen!)
└── logs/
    └── master_bot.log       # Logs mit Auto-Discovery Info
```

## 🚀 Workflow für neue Features

1. **Neues Cog erstellen:** Basierend auf Template
2. **In `cogs/` speichern:** Datei muss `.py` enden und nicht mit `_` beginnen  
3. **Automatisch laden:** `!master reloadall`
4. **Testen:** Commands sind sofort verfügbar
5. **Hotfix möglich:** Cog ändern → `!master reload [cogname]`

## ⚡ Vorteile

- ✅ **Null Anpassung** des Main Bots für neue Features
- ✅ **Hot-Reload** ohne Bot-Neustart
- ✅ **Automatische Discovery** neuer Cogs
- ✅ **Unabhängige Cogs** mit eigener Konfiguration
- ✅ **Template-System** für schnelle Entwicklung
- ✅ **Robustes Error-Handling** bei Cog-Fehlern

## 🎯 Beispiel-Workflow

```bash
# Neues Feature entwickeln
cp cogs/_template.py cogs/raid_manager.py

# Template anpassen...
# [Entwicklung des RaidManager Cogs]

# Im Discord:
!master discover          # Zeigt: 1 neues Cog gefunden
!master reloadall        # Lädt automatisch alle inkl. neue

# Feature ist sofort aktiv!
!raid create             # Neuer Command funktioniert
```

Das System ist jetzt **vollständig automatisch** und erfordert **keine Anpassungen** am Main Bot für neue Features! 🎉