from datetime import datetime, date
from typing import Optional, List
from sqlmodel import SQLModel, Field, Relationship

class Horse(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    notes: Optional[str] = None
    rides: List["Ride"] = Relationship(back_populates="horse")

class Ride(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: Optional[str] = None
    ride_date: date
    created_at: datetime = Field(default_factory=datetime.utcnow)

    distance_km: float
    total_time_s: int
    moving_time_s: int
    avg_speed_kmh: float
    avg_moving_speed_kmh: float
    max_speed_kmh: float
    ascent_m: float
    descent_m: float
    min_elev_m: Optional[float] = None
    max_elev_m: Optional[float] = None

    gpx_path: str

    horse_id: Optional[int] = Field(default=None, foreign_key="horse.id")
    horse: Optional[Horse] = Relationship(back_populates="rides")
