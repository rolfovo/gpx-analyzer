from typing import Optional, List
from datetime import date, datetime
from sqlmodel import SQLModel, Field, Relationship

class Horse(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    walk_trot_kmh: Optional[float] = None
    trot_canter_kmh: Optional[float] = None
    notes: Optional[str] = None
    rides: List['Ride'] = Relationship(back_populates='horse')

class Ride(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: Optional[str] = None
    ride_date: date
    horse_id: Optional[int] = Field(default=None, foreign_key="horse.id")
    horse: Optional[Horse] = Relationship(back_populates='rides')

    # metrics
    distance_km: float = 0.0
    total_time_s: int = 0
    moving_time_s: int = Field(default=0, nullable=False)  # NEW: keeps DB happy
    avg_speed_kmh: float = 0.0
    max_speed_kmh: float = 0.0
    ascent_m: float = 0.0
    descent_m: float = 0.0
    min_elev_m: Optional[float] = None
    max_elev_m: Optional[float] = None

    # file + timestamps
    gpx_path: str
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
