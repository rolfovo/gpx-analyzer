from sqlmodel import SQLModel, create_engine, Session
from pathlib import Path
import os


def _build_engine():
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        # psycopg3 driver
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+psycopg://")
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://")
        return create_engine(url, pool_pre_ping=True)

    # Fallback: SQLite (Render Disk /data, jinak lokální soubor)
    data_dir = Path("/data")
    if data_dir.exists():
        db_path = data_dir / "gpx_analyzer.db"
    else:
        db_path = Path(__file__).resolve().parents[1] / "gpx_analyzer.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


# Globální engine
engine = _build_engine()


def init_db():
    from .models import Horse, Ride
    SQLModel.metadata.create_all(engine)


def get_session():
    return Session(engine)
