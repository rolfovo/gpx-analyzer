from __future__ import annotations
from fastapi import FastAPI, UploadFile, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlalchemy.orm import selectinload
from pathlib import Path
from datetime import date, datetime
import uuid

from .db import init_db, get_session
from .models import Horse, Ride
from .metrics import parse_gpx_points, compute_metrics

app = FastAPI(title="GPX Analyzer")
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = (Path("/data/gpx") if Path("/data").exists() else BASE_DIR / "data" / "gpx")
DATA_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str((Path(__file__).parent / "static").resolve())), name="static")
templates = Jinja2Templates(directory=str((Path(__file__).parent / "templates").resolve()))

@app.on_event("startup")
def startup():
    init_db()

@app.post("/upload")
async def upload(file: UploadFile, horse_name: str = Form(default=""), ride_title: str = Form(default=""), ride_date: str = Form(default="")):
    text = (await file.read()).decode("utf-8", errors="ignore")
    uid = uuid.uuid4().hex; dest = DATA_DIR / f"{uid}.gpx"; dest.write_text(text, encoding="utf-8")
    pts = parse_gpx_points(text); m = compute_metrics(pts)
    with get_session() as s:
        horse = None
        if horse_name.strip():
            from sqlmodel import select
            horse = s.exec(select(Horse).where(Horse.name==horse_name.strip())).first()
            if not horse: 
                horse = Horse(name=horse_name.strip()); s.add(horse); s.commit(); s.refresh(horse)
        rd = date.fromisoformat(ride_date) if ride_date else (m.start_time or datetime.utcnow()).date()
        r = Ride(
            title=ride_title or file.filename,
            ride_date=rd,
            horse_id=horse.id if horse else None,
            distance_km=round(m.distance_m/1000,3),
            total_time_s=m.total_time_s,
            moving_time_s=(getattr(m,'moving_time_s', None) or m.total_time_s),  # NEW
            avg_speed_kmh=round(m.avg_speed_mps*3.6,2),
            max_speed_kmh=round(m.max_speed_mps*3.6,2),
            ascent_m=round(m.ascent_m,1),
            descent_m=round(m.descent_m,1),
            min_elev_m=m.min_elev_m,
            max_elev_m=m.max_elev_m,
            gpx_path=str(dest)
        )
        s.add(r); s.commit(); s.refresh(r)
        return RedirectResponse(f"/ride/{r.id}", status_code=303)
