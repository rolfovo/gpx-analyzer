from fastapi import FastAPI, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select, col
from pathlib import Path
from datetime import datetime, date
import csv, io, uuid

from .db import init_db, get_session
from .models import Horse, Ride
from .metrics import parse_gpx_points, compute_metrics

app = FastAPI(title="GPX Analyzer")
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data" / "gpx"
DATA_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str((Path(__file__).parent / "static").resolve())), name="static")
templates = Jinja2Templates(directory=str((Path(__file__).parent / "templates").resolve()))

DEFAULT_WALK_TROT = 7.0
DEFAULT_TROT_CANTER = 13.0

@app.on_event("startup")
def on_startup():
    init_db()

def apply_filters(stmt, q_horse: str|None, q_from: str|None, q_to: str|None):
    if q_horse:
        stmt = stmt.where(col(Ride.horse_id) == col(select(Horse.id).where(Horse.name == q_horse).scalar_subquery()))
    if q_from:
        stmt = stmt.where(Ride.ride_date >= date.fromisoformat(q_from))
    if q_to:
        stmt = stmt.where(Ride.ride_date <= date.fromisoformat(q_to))
    return stmt

def accum_periods(rides):
    # Build monthly, weekly (YYYY-Www), yearly summaries
    monthly, weekly, yearly = {}, {}, {}
    for r in rides:
        ym = r.ride_date.strftime("%Y-%m")
        iso = r.ride_date.isocalendar()  # (year, week, weekday)
        yw = f"{iso[0]}-W{iso[1]:02d}"
        yy = r.ride_date.strftime("%Y")
        for bucket, key in ((monthly, ym),(weekly, yw),(yearly, yy)):
            d = bucket.setdefault(key, {"km":0.0,"rides":0,"avg_sum":0.0})
            d["km"] += r.distance_km
            d["rides"] += 1
            d["avg_sum"] += r.avg_speed_kmh
    def rows(bucket):
        return [{"period":k,"rides":v["rides"],"km":round(v["km"],2),"avg_kmh":round(v["avg_sum"]/v["rides"],2)} for k,v in sorted(bucket.items(), reverse=True)]
    return rows(monthly), rows(weekly), rows(yearly)

@app.get("/", response_class=HTMLResponse)
def index(request: Request, q_horse: str | None = None, q_from: str | None = None, q_to: str | None = None):
    with get_session() as s:
        horses = s.exec(select(Horse).order_by(Horse.name)).all()
        stmt = apply_filters(select(Ride).order_by(Ride.ride_date.desc(), Ride.id.desc()), q_horse, q_from, q_to)
        rides = s.exec(stmt).all()
        total_km = round(sum(r.distance_km for r in rides), 2) if rides else 0.0
        avg_kmh = round(sum(r.avg_speed_kmh for r in rides)/len(rides), 2) if rides else 0.0
        monthly_rows, weekly_rows, yearly_rows = accum_periods(rides)
    return templates.TemplateResponse("index.html", {"request": request, "horses": horses, "rides": rides,
                                                     "q_horse": q_horse or "", "q_from": q_from or "", "q_to": q_to or "",
                                                     "total_km": total_km, "avg_kmh": avg_kmh,
                                                     "monthly": monthly_rows, "weekly": weekly_rows, "yearly": yearly_rows})

@app.post("/upload", response_class=HTMLResponse)
async def upload_gpx(request: Request, file: UploadFile, horse_name: str = Form(default=""), ride_title: str = Form(default=""), ride_date: str = Form(default="")):
    contents = await file.read()
    uid = uuid.uuid4().hex
    dest = DATA_DIR / f"{uid}.gpx"
    dest.write_bytes(contents)

    pts = parse_gpx_points(contents.decode("utf-8", errors="ignore"))
    metrics = compute_metrics(pts)

    with get_session() as s:
        horse = None
        if horse_name.strip():
            horse = s.exec(select(Horse).where(Horse.name == horse_name.strip())).first()
            if not horse:
                horse = Horse(name=horse_name.strip())
                s.add(horse); s.commit(); s.refresh(horse)

        rd = date.fromisoformat(ride_date.strip()) if ride_date.strip() else (metrics.start_time or datetime.utcnow()).date()

        r = Ride(
            title=ride_title.strip() or file.filename,
            ride_date=rd,
            distance_km=round(metrics.distance_m/1000.0, 3),
            total_time_s=metrics.total_time_s,
            moving_time_s=metrics.moving_time_s,
            avg_speed_kmh=round(metrics.avg_speed_mps*3.6, 2),
            avg_moving_speed_kmh=round(metrics.avg_moving_speed_mps*3.6, 2),
            max_speed_kmh=round(metrics.max_speed_mps*3.6, 2),
            ascent_m=round(metrics.ascent_m, 1),
            descent_m=round(metrics.descent_m, 1),
            min_elev_m=metrics.min_elev_m,
            max_elev_m=metrics.max_elev_m,
            gpx_path=str(dest),
            horse_id=horse.id if horse else None
        )
        s.add(r); s.commit(); s.refresh(r)
        return RedirectResponse(url=f"/ride/{r.id}", status_code=303)

@app.get("/ride/{ride_id}", response_class=HTMLResponse)
def ride_detail(request: Request, ride_id: int):
    with get_session() as s:
        ride = s.get(Ride, ride_id)
        if not ride: return HTMLResponse("Ride not found", status_code=404)
        horses = s.exec(select(Horse).order_by(Horse.name)).all()
    text = Path(ride.gpx_path).read_text(encoding="utf-8", errors="ignore")
    pts = parse_gpx_points(text)
    metrics = compute_metrics(pts)
    speed_ts = [{"t": (t.isoformat() if t else None), "v": v*3.6} for t, v in metrics.speed_series]
    elev_profile = [{"d": d/1000.0, "e": e} for d, e in metrics.elev_profile]
    return templates.TemplateResponse("ride_detail.html", {"request": request, "ride": ride, "horses": horses, "speed_ts": speed_ts, "elev_profile": elev_profile})

@app.post("/ride/{ride_id}/assign", response_class=HTMLResponse)
async def assign_horse(ride_id: int, horse_name: str = Form(...)):
    with get_session() as s:
        ride = s.get(Ride, ride_id)
        if not ride: return HTMLResponse("Ride not found", status_code=404)
        horse = s.exec(select(Horse).where(Horse.name == horse_name.strip())).first()
        if not horse:
            horse = Horse(name=horse_name.strip()); s.add(horse); s.commit(); s.refresh(horse)
        ride.horse_id = horse.id; s.add(ride); s.commit()
    return RedirectResponse(url=f"/ride/{ride_id}", status_code=303)

@app.get("/gpx/{ride_id}")
def download_gpx(ride_id: int):
    with get_session() as s:
        ride = s.get(Ride, ride_id)
        if not ride: return HTMLResponse("Ride not found", status_code=404)
        return FileResponse(path=ride.gpx_path, filename=Path(ride.gpx_path).name, media_type="application/gpx+xml")

@app.get("/horses", response_class=HTMLResponse)
def horses_page(request: Request):
    with get_session() as s:
        horses = s.exec(select(Horse).order_by(Horse.name)).all()
    return templates.TemplateResponse("horses.html", {"request": request, "horses": horses,
                                                      "def_walk_trot": DEFAULT_WALK_TROT,
                                                      "def_trot_canter": DEFAULT_TROT_CANTER})

@app.post("/horses/save", response_class=HTMLResponse)
async def horses_save(request: Request):
    form = await request.form()
    with get_session() as s:
        for key, val in form.items():
            if not key.startswith("h_"): continue
            _, field, id_str = key.split("_")
            horse = s.get(Horse, int(id_str))
            if not horse: continue
            v = float(val) if val else None
            if field == "walk": horse.walk_trot_kmh = v
            elif field == "trot": horse.trot_canter_kmh = v
            s.add(horse)
        s.commit()
    return RedirectResponse(url="/horses", status_code=303)

@app.get("/horse/{horse_id}", response_class=HTMLResponse)
def horse_detail(request: Request, horse_id: int):
    with get_session() as s:
        horse = s.get(Horse, horse_id)
        if not horse: return HTMLResponse("Kůň nenalezen", status_code=404)
        rides = s.exec(select(Ride).where(Ride.horse_id == horse_id).order_by(Ride.ride_date.desc())).all()
    total_km = round(sum(r.distance_km for r in rides), 2) if rides else 0.0
    avg_kmh = round(sum(r.avg_speed_kmh for r in rides)/len(rides), 2) if rides else 0.0
    monthly_rows, weekly_rows, yearly_rows = accum_periods(rides)
    return templates.TemplateResponse("horse_detail.html", {"request": request, "horse": horse, "rides": rides,
                                                            "total_km": total_km, "avg_kmh": avg_kmh,
                                                            "monthly": monthly_rows, "weekly": weekly_rows, "yearly": yearly_rows})

@app.get("/export/csv")
def export_csv(q_horse: str | None = None, q_from: str | None = None, q_to: str | None = None):
    with get_session() as s:
        rides = s.exec(apply_filters(select(Ride).order_by(Ride.ride_date), q_horse, q_from, q_to)).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id","date","title","horse","distance_km","avg_speed_kmh","max_speed_kmh","ascent_m","descent_m"])
    for r in rides:
        w.writerow([r.id, r.ride_date.isoformat(), r.title or "", (r.horse.name if r.horse else ""),
                    f"{r.distance_km:.3f}", f"{r.avg_speed_kmh:.2f}", f"{r.max_speed_kmh:.2f}", f"{r.ascent_m:.0f}", f"{r.descent_m:.0f}"])
    buf.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="gpx_export.csv"'}
    return StreamingResponse(iter([buf.read()]), media_type="text/csv", headers=headers)

@app.get("/export/horse/{horse_id}.csv")
def export_csv_horse(horse_id: int):
    with get_session() as s:
        rides = s.exec(select(Ride).where(Ride.horse_id==horse_id).order_by(Ride.ride_date)).all()
        horse = s.get(Horse, horse_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["horse","id","date","title","distance_km","avg_speed_kmh","max_speed_kmh","ascent_m","descent_m"])
    for r in rides:
        w.writerow([horse.name if horse else "", r.id, r.ride_date.isoformat(), r.title or "",
                    f"{r.distance_km:.3f}", f"{r.avg_speed_kmh:.2f}", f"{r.max_speed_kmh:.2f}", f"{r.ascent_m:.0f}", f"{r.descent_m:.0f}"])
    buf.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="{(horse.name if horse else "horse")}_export.csv"'}
    return StreamingResponse(iter([buf.read()]), media_type="text/csv", headers=headers)
