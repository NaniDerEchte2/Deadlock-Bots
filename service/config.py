from typing import Optional
from pathlib import Path
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # --- Bot Core ---
    discord_token: SecretStr = Field(..., alias="DISCORD_TOKEN")
    owner_id: int = Field(0, alias="OWNER_ID")
    command_prefix: str = Field("!", alias="COMMAND_PREFIX")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    
    # --- Database ---
    deadlock_db_path: Optional[Path] = Field(None, alias="DEADLOCK_DB_PATH")
    deadlock_db_dir: Optional[Path] = Field(None, alias="DEADLOCK_DB_DIR")
    deadlock_db_busy_timeout_ms: int = Field(15000, alias="DEADLOCK_DB_BUSY_TIMEOUT_MS")

    # --- Steam / Deadlock Integration ---
    steam_api_key: Optional[SecretStr] = Field(None, alias="STEAM_API_KEY")
    steam_web_api_key: Optional[SecretStr] = Field(None, alias="STEAM_WEB_API_KEY")
    
    # --- TempVoice IDs ---
    # TODO: Move hardcoded sets from cogs/tempvoice/core.py here eventually
    # For now, we keep them there to avoid massive refactoring at once.
    
    # --- Deadlock Voice Status ---
    match_minute_offset: int = Field(3, alias="DEADLOCK_MATCH_MINUTE_OFFSET")
    # rename_cooldown_seconds: int = Field(360, alias="RANK_VOICE_RENAME_COOLDOWN_SECONDS") # Replaced by specific cooldown below
    deadlock_vs_rename_cooldown_seconds: int = Field(120, alias="DEADLOCK_VS_RENAME_COOLDOWN_SECONDS")
    rank_vs_rename_cooldown_seconds: int = Field(360, alias="RANK_VS_RENAME_COOLDOWN_SECONDS")

    # --- Feature Flags & Toggles ---
    master_dashboard_enabled: bool = Field(True, alias="MASTER_DASHBOARD_ENABLED")
    global_rename_throttle_seconds: int = Field(240, alias="GLOBAL_RENAME_THROTTLE_SECONDS") # 4 minutes default
    
    # --- External Worker Config ---
    # rename_worker_url: Optional[str] = Field(None, alias="RENAME_WORKER_URL") # Not used with DB communication
    use_db_rename_worker: bool = Field(False, alias="USE_DB_RENAME_WORKER")
    rename_worker_bot_token: Optional[SecretStr] = Field(None, alias="RENAME_WORKER_BOT_TOKEN")

    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8",
        extra="ignore"  # Ignore extra env vars
    )

# Singleton instance
try:
    settings = Settings()
except Exception as e:
    # Fallback for first-time setup or missing env
    print(f"⚠️ Config loading warning: {e}")
    # Create a dummy instance to prevent import errors during setup
    # Real validation happens when the bot starts properly
    class DummySettings(Settings):
        discord_token: SecretStr = SecretStr("dummy")
    settings = DummySettings()
