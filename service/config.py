import logging
import os
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


def _load_vault_secrets():
    """Injiziert Secrets aus dem Windows Tresor in os.environ."""
    try:
        import keyring

        service_name = "DeadlockBot"
        keys = [
            "DISCORD_TOKEN",
            "OWNER_ID",
            "COMMAND_PREFIX",
            "STEAM_API_KEY",
            "TWITCH_CLIENT_ID",
            "TWITCH_CLIENT_SECRET",
            "TWITCH_BOT_TOKEN",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "PUBLIC_BASE_URL",
            "TIKTOK_CLIENT_KEY",
            "TIKTOK_CLIENT_SECRET",
            "YOUTUBE_CLIENT_ID",
            "YOUTUBE_CLIENT_SECRET",
            "INSTAGRAM_CLIENT_ID",
            "INSTAGRAM_CLIENT_SECRET",
        ]
        count = 0
        for key in keys:
            # IMMER aus Tresor laden und überschreiben, das ist die sicherste Quelle
            val = keyring.get_password(service_name, key)
            if val:
                os.environ[key] = val
                count += 1
        # print(f"DEBUG: Config injizierte {count} Secrets aus Tresor.")
    except Exception as e:
        log.warning("Fehler beim Laden aus Tresor: %s", e)


# Vor der Klassen-Definition aufrufen!
_load_vault_secrets()


class Settings(BaseSettings):
    # --- Bot Core ---
    discord_token: SecretStr = Field(..., alias="DISCORD_TOKEN")
    owner_id: int = Field(0, alias="OWNER_ID")
    command_prefix: str = Field("!", alias="COMMAND_PREFIX")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    guild_id: int = Field(1289721245281292288, alias="GUILD_ID")

    # --- Database ---
    deadlock_db_path: Path | None = Field(None, alias="DEADLOCK_DB_PATH")
    deadlock_db_dir: Path | None = Field(None, alias="DEADLOCK_DB_DIR")
    deadlock_db_busy_timeout_ms: int = Field(15000, alias="DEADLOCK_DB_BUSY_TIMEOUT_MS")

    # --- Steam / Deadlock Integration ---
    steam_api_key: SecretStr | None = Field(None, alias="STEAM_API_KEY")
    steam_web_api_key: SecretStr | None = Field(None, alias="STEAM_WEB_API_KEY")
    public_base_url: str = Field("https://link.earlysalty.com", alias="PUBLIC_BASE_URL")
    
    # --- Roles & Channels ---
    verified_role_id: int = Field(1419608095533043774, alias="VERIFIED_ROLE_ID")
    verified_log_channel_id: int = Field(1374364800817303632, alias="VERIFIED_LOG_CHANNEL_ID")
    streamer_role_id: int = Field(1313624729466441769, alias="STREAMER_ROLE_ID")
    streamer_notify_channel_id: int = Field(1374364800817303632, alias="STREAMER_NOTIFY_CHANNEL_ID")

    # --- Steam Link UI & OAuth ---
    steam_return_path: str = Field("/steam/return", alias="STEAM_RETURN_PATH")
    http_host: str = Field("0.0.0.0", alias="HTTP_HOST")
    http_port: int = Field(8888, alias="STEAM_OAUTH_PORT")
    link_cover_image: str = Field("", alias="LINK_COVER_IMAGE")
    link_cover_label: str = Field("link.earlysalty.com", alias="LINK_COVER_LABEL")
    link_button_label: str = Field("Via Discord verknüpfen", alias="LINK_BUTTON_LABEL")
    steam_button_label: str = Field("Direkt bei Steam anmelden", alias="STEAM_BUTTON_LABEL")

    # --- TempVoice IDs ---
    # TODO: Move hardcoded sets from cogs/tempvoice/core.py here eventually
    # For now, we keep them there to avoid massive refactoring at once.

    # --- Deadlock Voice Status ---
    match_minute_offset: int = Field(3, alias="DEADLOCK_MATCH_MINUTE_OFFSET")
    rank_vs_rename_cooldown_seconds: int = Field(360, alias="RANK_VS_RENAME_COOLDOWN_SECONDS")

    # --- Feature Flags & Toggles ---
    master_dashboard_enabled: bool = Field(True, alias="MASTER_DASHBOARD_ENABLED")

    # --- External Worker Config ---
    # rename_worker_url: Optional[str] = Field(None, alias="RENAME_WORKER_URL") # Not used with DB communication
    use_db_rename_worker: bool = Field(False, alias="USE_DB_RENAME_WORKER")
    rename_worker_bot_token: SecretStr | None = Field(None, alias="RENAME_WORKER_BOT_TOKEN")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore extra env vars
    )


# Singleton instance
try:
    settings = Settings()
except Exception as e:
    # Fallback for first-time setup or missing env
    log.warning("Config loading warning: %s", e)

    # Create a dummy instance to prevent import errors during setup
    # Real validation happens when the bot starts properly
    class DummySettings(Settings):
        discord_token: SecretStr = SecretStr("dummy")

    settings = DummySettings()

# Touch the singleton once locally so static analysis knows it is intentional.
log.debug("Config loaded; dashboard=%s", getattr(settings, "master_dashboard_enabled", None))


def get_settings() -> Settings:
    """Return the shared settings instance used across the bot."""
    return settings


__all__ = ["Settings", "settings", "get_settings"]
