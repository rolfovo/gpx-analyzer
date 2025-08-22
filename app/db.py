from sqlmodel import SQLModel, create_engine, Session
from pathlib import Path
import os

DB_URL = os.getenv("DATABASE_URL", "").strip()
if DB_URL:
    engine = create_engine(DB_URL, pool_pre_ping=True)
else:
    DB_PATH = Path(__file__).resolve().parents[1] / "gpx_analyzer.db"
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})

def init_db():
    from .models import Horse, Ride  # noqa
    SQLModel.metadata.create_all(engine)

def get_session():
    return Session(engine)
