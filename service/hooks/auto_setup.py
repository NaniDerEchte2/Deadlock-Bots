"""
Auto-Setup für Git Pre-Commit Hook
Aktiviert automatisch beim Bot-Start, ohne manuelle Intervention
Windows-kompatibel mit .cmd/.bat Hooks
"""

import sys
import stat
import logging
from pathlib import Path

logger = logging.getLogger("HookAutoSetup")


def setup_pre_commit_hook() -> bool:
    """
    Aktiviert den Pre-Commit Hook automatisch.
    Idempotent - kann beliebig oft aufgerufen werden.

    Returns:
        bool: True wenn Hook erfolgreich aktiviert wurde, False sonst
    """
    try:
        # Finde Git-Root (wo .git liegt)
        git_root = _find_git_root()
        if not git_root:
            logger.debug("Kein Git Repository gefunden - überspringe Hook-Setup")
            return False

        hooks_dir = git_root / ".git" / "hooks"
        hook_path = hooks_dir / "pre-commit"
        hook_cmd_path = hooks_dir / "pre-commit.cmd"
        hook_py_path = hooks_dir / "pre-commit.py"

        # Prüfe ob Hook-Dateien existieren
        if not (hook_path.exists() or hook_cmd_path.exists()):
            logger.warning(f"Pre-Commit Hook nicht gefunden in {hooks_dir}")
            return False

        # Konfiguriere Git für Hook-Verwendung
        _configure_git_hooks_path(git_root)

        # Auf Windows: Git findet .cmd/.bat automatisch
        if sys.platform == "win32":
            logger.debug("Windows erkannt - Git nutzt .cmd/.bat Hooks automatisch")
            logger.info("🔒 Datenbank-Schutz aktiviert (Pre-Commit Hook)")
            return True

        # Auf Linux/Mac: Mache Hook ausführbar
        for hook_file in [hook_path, hook_py_path]:
            if hook_file.exists():
                try:
                    current_permissions = hook_file.stat().st_mode
                    new_permissions = (
                        current_permissions | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                    )

                    if current_permissions != new_permissions:
                        hook_file.chmod(new_permissions)
                        logger.debug(f"Hook ausführbar gemacht: {hook_file}")
                except Exception as e:
                    logger.debug(f"Konnte {hook_file} nicht ausführbar machen: {e}")

        logger.info("🔒 Datenbank-Schutz aktiviert (Pre-Commit Hook)")
        return True

    except Exception as exc:
        logger.warning("Konnte Pre-Commit Hook nicht aktivieren: %s", exc)
        return False


def _find_git_root() -> Path | None:
    """Findet das Git-Root-Verzeichnis (wo .git liegt)"""
    current = Path(__file__).resolve()

    # Gehe bis zu 10 Ebenen nach oben
    for _ in range(10):
        git_dir = current / ".git"
        if git_dir.exists() and git_dir.is_dir():
            return current

        parent = current.parent
        if parent == current:  # Reached filesystem root
            break
        current = parent

    return None


def _configure_git_hooks_path(git_root: Path) -> None:
    """
    Konfiguriert Git für Hook-Nutzung.
    Wichtig für Windows, wo Hooks manchmal nicht automatisch erkannt werden.
    """
    try:
        import subprocess

        # Git config für hooks path setzen
        result = subprocess.run(
            ["git", "config", "core.hooksPath", ".git/hooks"],
            cwd=str(git_root),
            capture_output=True,
            universal_newlines=True,
            timeout=5,
        )

        if result.returncode == 0:
            logger.debug("Git hooks path konfiguriert")
        else:
            logger.debug("Git hooks path bereits konfiguriert")

    except Exception as exc:
        logger.debug(
            "Git config für hooks path fehlgeschlagen (nicht kritisch): %s", exc
        )


# Auto-Run beim Import
def auto_setup():
    """Wird automatisch beim Import ausgeführt"""
    try:
        success = setup_pre_commit_hook()
        if not success:
            logger.debug("Hook-Setup übersprungen oder fehlgeschlagen")
    except Exception as exc:
        logger.debug("Auto-Setup Hook fehlgeschlagen: %s", exc)


# Führe Auto-Setup aus, wenn dieses Modul importiert wird
auto_setup()
