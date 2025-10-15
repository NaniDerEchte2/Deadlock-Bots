# filename: steam_master.py
from __future__ import annotations
import os, sys, time, asyncio, threading
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import discord
from discord.ext import commands

from steam.client import SteamClient
from steam.enums import EResult
from steam.enums.emsg import EMsg

# ---- Pfade für persistente Tokens ----
DATA_DIR = Path(".steam-data"); DATA_DIR.mkdir(exist_ok=True)
LOGIN_KEY_FILE = DATA_DIR / "login_key.txt"   # Dauer-Token (äquivalent zur JS-"Refresh"-Wirkung)
SENTRY_FILE    = DATA_DIR / "sentry.bin"      # Machine-Auth ("remember this device")

# ---- ENV laden ----
load_dotenv(r"C:\Users\Nani-Admin\Documents\.env")
DISCORD_TOKEN  = os.getenv("DISCORD_TOKEN", "")
STEAM_USERNAME = os.getenv("STEAM_USERNAME", "")
STEAM_PASSWORD = os.getenv("STEAM_PASSWORD", "")
if not (DISCORD_TOKEN and STEAM_USERNAME and STEAM_PASSWORD):
    print("Bitte DISCORD_TOKEN, STEAM_USERNAME, STEAM_PASSWORD in der .env setzen.")
    sys.exit(1)

# ==== Steam-Login-Thread ====
class SteamLoginManager(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.client = SteamClient()
        self.guard_code: Optional[str] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.logged_on = False
        self.last_result: Optional[EResult] = None

        self.client.on(EMsg.ClientUpdateMachineAuth, self._on_machine_auth)
        self.client.on(EMsg.ClientLogOnResponse, self._on_logon_response)
        self.client.on(EMsg.ClientLoggedOff, self._on_logged_off)

    # ---- API für Discord-Commands ----
    def set_guard_code(self, code: str):
        with self._lock:
            self.guard_code = code.strip()
        print("[Steam] Guard-Code übernommen.")

    def status(self) -> str:
        has_key = LOGIN_KEY_FILE.exists() and LOGIN_KEY_FILE.stat().st_size > 0
        has_sentry = SENTRY_FILE.exists() and SENTRY_FILE.stat().st_size > 0
        return f"logged_on={self.logged_on} last_result={self.last_result} login_key={'yes' if has_key else 'no'} sentry={'yes' if has_sentry else 'no'}"

    def clear_login_key(self) -> bool:
        try:
            if LOGIN_KEY_FILE.exists():
                LOGIN_KEY_FILE.unlink()
                return True
            return False
        except Exception:
            return False

    # ---- Persist helpers ----
    def _read_login_key(self) -> Optional[str]:
        try:
            return LOGIN_KEY_FILE.read_text(encoding="utf-8").strip() if LOGIN_KEY_FILE.exists() else None
        except Exception:
            return None

    def _write_login_key(self, key: str):
        try:
            LOGIN_KEY_FILE.write_text(key, encoding="utf-8")
            print("[Steam] login_key gespeichert.")
        except Exception as e:
            print(f"[Steam] login_key speichern fehlgeschlagen: {e}")

    def _write_sentry(self, data: bytes):
        try:
            SENTRY_FILE.write_bytes(data)
            print(f"[Steam] Sentry gespeichert: {SENTRY_FILE}")
        except Exception as e:
            print(f"[Steam] Sentry speichern fehlgeschlagen: {e}")

    # ---- Steam Events ----
    def _on_machine_auth(self, msg):
        data = getattr(msg.body, "bytes", b"")
        if data:
            self._write_sentry(data)

    def _on_logon_response(self, msg):
        self.last_result = msg.body.eresult
        if self.last_result == EResult.OK:
            self.logged_on = True
            # Manche Builds setzen den login_key als Attribut
            key = getattr(self.client, "login_key", None)
            if key:
                self._write_login_key(str(key))
            print(f"[Steam] Eingeloggt als: {self.client.user.name}")
        else:
            self.logged_on = False
            print(f"[Steam] LogOnResponse: {self.last_result}")

    def _on_logged_off(self, msg):
        self.logged_on = False
        print(f"[Steam] Logged off: {msg.body.eresult}")

    # ---- Login-Loop ----
    def _try_login(self) -> EResult:
        # 1) zuerst mit login_key
        login_key = self._read_login_key()
        if login_key:
            print("[Steam] Login mit login_key ...")
            return self.client.login(username=STEAM_USERNAME, login_key=login_key)

        # 2) Username/Passwort; 2FA nur wenn vom Benutzer via !sg geliefert
        with self._lock:
            code = self.guard_code
            self.guard_code = None

        if code:
            print("[Steam] Login mit Passwort + 2FA ...")
            return self.client.login(username=STEAM_USERNAME, password=STEAM_PASSWORD, two_factor_code=code)

        print("[Steam] Login mit Passwort (ohne 2FA)...")
        return self.client.login(username=STEAM_USERNAME, password=STEAM_PASSWORD)

    def run(self):
        while not self._stop.is_set():
            try:
                res = self._try_login()
                self.last_result = res

                if res == EResult.AccountLoginDeniedNeedTwoFactor:
                    print("[Steam] 2FA benötigt. Bitte per !sg CODE senden.")
                    # Warten bis Code ankommt
                    while not self._stop.is_set():
                        with self._lock:
                            if self.guard_code:
                                break
                        time.sleep(1)
                    # nächster Loop versucht erneut
                    continue

                if res != EResult.OK:
                    print(f"[Steam] Login fehlgeschlagen: {res}. Neuer Versuch in 15s.")
                    time.sleep(15)
                    continue

                # Erfolgreich -> Pumpen bis Disconnect
                self.client.run_forever()
                print("[Steam] Disconnected. Reconnect in 10s.")
                time.sleep(10)

            except Exception as e:
                print(f"[Steam] Fehler im Login-Loop: {e}")
                time.sleep(10)

    def stop(self):
        self._stop.set()
        try: self.client.logout()
        except Exception: pass

# ==== Discord-Bot: nur die Login/Token-Commands ====
intents = discord.Intents.none()
bot = commands.Bot(command_prefix="!", intents=intents)
steam_manager = SteamLoginManager()

@bot.event
async def on_ready():
    print(f"[Discord] eingeloggt als {bot.user} ({bot.user.id})")
    if not steam_manager.is_alive():
        steam_manager.start()

@bot.command(name="sg", aliases=["steam_guard","steamguard"])
@commands.has_permissions(administrator=True)
async def cmd_sg(ctx: commands.Context, code: str):
    steam_manager.set_guard_code(code)
    await ctx.reply("✅ Guard-Code gesetzt. Login wird erneut versucht.")

@bot.command(name="steam_status")
@commands.has_permissions(administrator=True)
async def cmd_status(ctx: commands.Context):
    await ctx.reply(f"```{steam_manager.status()}```")

@bot.command(name="steam_token")
@commands.has_permissions(administrator=True)
async def cmd_token(ctx: commands.Context):
    has_key = LOGIN_KEY_FILE.exists() and LOGIN_KEY_FILE.stat().st_size > 0
    await ctx.reply(f"🔐 login_key: {'vorhanden' if has_key else 'nicht vorhanden'}\nPfad: `{LOGIN_KEY_FILE}`")

@bot.command(name="steam_token_clear")
@commands.has_permissions(administrator=True)
async def cmd_token_clear(ctx: commands.Context):
    ok = steam_manager.clear_login_key()
    await ctx.reply("🧹 login_key gelöscht." if ok else "ℹ️ Kein login_key vorhanden.")

def main():
    try:
        bot.run(DISCORD_TOKEN, log_handler=None)
    finally:
        steam_manager.stop()

if __name__ == "__main__":
    main()
