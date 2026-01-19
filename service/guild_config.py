"""
Zentrale Guild-spezifische Konfiguration.
Verschiebt hardcoded IDs aus verschiedenen Cogs in eine Config.
"""
from typing import Set
from dataclasses import dataclass


@dataclass
class GuildIDs:
    """Hardcoded Guild/Channel/Role IDs - zentral verwaltet."""

    # TempVoice
    TEMPVOICE_STAGING_CASUAL: int = 1330278323145801758
    TEMPVOICE_STAGING_RANKED: int = 1357422958544420944
    TEMPVOICE_STAGING_SPECIAL: int = 1412804671432818890

    TEMPVOICE_CATEGORY_GRIND: int = 1412804540994162789
    TEMPVOICE_CATEGORY_NORMAL: int = 1289721245281292290
    TEMPVOICE_CATEGORY_RANKED: int = 1357422957017698478

    TEMPVOICE_INTERFACE_CHANNEL: int = 1371927143537315890
    ENGLISH_ONLY_ROLE: int = 1309741866098491479

    # Rank Voice Manager
    RANK_CATEGORY_CASUAL_1: int = 1290343267974406234
    RANK_CATEGORY_CASUAL_2: int = 1332065827867230339
    RANK_CATEGORY_RANKED_1: int = 1290343215927070732
    RANK_CATEGORY_RANKED_2: int = 1332065944548712560

    # Deadlock Voice Status
    VOICE_STATUS_CATEGORY_GRIND: int = 1412804540994162789
    VOICE_STATUS_CATEGORY_CASUAL: int = 1289721245281292290
    VOICE_STATUS_CATEGORY_RANKED: int = 1357422957017698478

    # Steam Verified Role
    STEAM_VERIFIED_GUILD_ID: int = 1075099396802904185
    STEAM_VERIFIED_ROLE_ID: int = 1313559094994391063
    STEAM_VERIFIED_ANNOUNCE_CHANNEL_ID: int = 1313587932169859083

    @property
    def tempvoice_staging_channels(self) -> Set[int]:
        """Alle TempVoice Staging Channel IDs."""
        return {
            self.TEMPVOICE_STAGING_CASUAL,
            self.TEMPVOICE_STAGING_RANKED,
            self.TEMPVOICE_STAGING_SPECIAL,
        }

    @property
    def tempvoice_minrank_categories(self) -> Set[int]:
        """Alle TempVoice MinRank Category IDs."""
        return {
            self.TEMPVOICE_CATEGORY_GRIND,
            self.TEMPVOICE_CATEGORY_NORMAL,
            self.TEMPVOICE_CATEGORY_RANKED,
        }

    @property
    def rank_voice_categories(self) -> Set[int]:
        """Alle Rank Voice Manager Category IDs."""
        return {
            self.RANK_CATEGORY_CASUAL_1,
            self.RANK_CATEGORY_CASUAL_2,
            self.RANK_CATEGORY_RANKED_1,
            self.RANK_CATEGORY_RANKED_2,
            self.TEMPVOICE_CATEGORY_GRIND,
            self.TEMPVOICE_CATEGORY_NORMAL,
            self.TEMPVOICE_CATEGORY_RANKED,
        }

    @property
    def voice_status_categories(self) -> Set[int]:
        """Alle Voice Status Category IDs."""
        return {
            self.VOICE_STATUS_CATEGORY_GRIND,
            self.VOICE_STATUS_CATEGORY_CASUAL,
            self.VOICE_STATUS_CATEGORY_RANKED,
        }


# Singleton instance
_guild_ids: GuildIDs | None = None


def get_guild_config() -> GuildIDs:
    """
    Zugriff auf Guild Config (Singleton).

    Kann später auf ENV-basierte Overrides erweitert werden:
    ```python
    TEMPVOICE_STAGING_CASUAL = int(os.getenv(
        "TEMPVOICE_STAGING_CASUAL",
        str(defaults.TEMPVOICE_STAGING_CASUAL)
    ))
    ```
    """
    global _guild_ids
    if _guild_ids is None:
        _guild_ids = GuildIDs()

    return _guild_ids
