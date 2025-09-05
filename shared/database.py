from pathlib import Path
import sqlite3


def get_db_path() -> str:
    """Return path to the shared Deadlock database, ensuring directories exist."""
    db_dir = Path.home() / "Documents" / "Deadlock"
    db_dir.mkdir(parents=True, exist_ok=True)
    return str(db_dir / "deadlock.sqlite3")


def vacuum_db():
    """Run VACUUM on the shared database to clean up unused space."""
    db_path = get_db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("VACUUM")
    except sqlite3.Error:
        pass
