from sqlmodel import SQLModel, create_engine, Session
from pathlib import Path
import os

def _normalize_db_url(url: str) -> str:
    if not url:
        return url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if "+psycopg2" in url:
        url = url.replace("+psycopg2", "+psycopg", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url

def _resolve_default_sqlite() -> str:
    for base in (Path("/data"), Path(__file__).resolve().parents[1] / "data"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return f"sqlite:///{(base / 'gpx_analyzer.db')}"
        except Exception:
            continue
    return f"sqlite:///{(Path(__file__).resolve().parents[1] / 'gpx_analyzer.db')}"

DB_URL = os.getenv("DATABASE_URL", "").strip()
DB_URL = _normalize_db_url(DB_URL) if DB_URL else _resolve_default_sqlite()

connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=connect_args, pool_pre_ping=True)

def init_db():
    from .models import Horse, Ride  # noqa
    SQLModel.metadata.create_all(engine)

def get_session():
    return Session(engine)
