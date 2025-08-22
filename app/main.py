from fastapi import FastAPI, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlalchemy.orm import selectinload
from pathlib import Path
from datetime import datetime, date
import csv, io, uuid, zipfile
import gpxpy

from .db import init_db, get_session
from .models import Horse, Ride
from .metrics import parse_gpx_points, compute_metrics

app = FastAPI(title="GPX Analyzer – Horse Dashboard")
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = (Path("/data/gpx") if Path("/data").exists() else BASE_DIR / "data" / "gpx")
DATA_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str((Path(__file__).parent / "static").resolve())), name="static")
templates = Jinja2Templates(directory=str((Path(__file__).parent / "templates").resolve()))


@app.on_event("startup")
def on_startup():
    init_db()


def accum_periods(rides):
    monthly, weekly, yearly = {}, {}, {}
    for r in rides:
        ym = r.ride_date.strftime("%Y-%m")
        iso = r.ride_date.isocalendar()
        yw = f"{iso[0]}-W{iso[1]:02d}"
        yy = r.ride_date.strftime("%Y")
        for bucket, key in ((monthly, ym), (weekly, yw), (yearly, yy)):
            d = bucket.setdefault(key, {"km": 0.0, "rides": 0, "avg_sum": 0.0})
            d["km"] += r.distance_km
            d["rides"] += 1
            d["avg_sum"] += r.avg_speed_kmh

    def rows(bucket):
        return [
            {
                "period": k,
                "rides": v["rides"],
                "km": round(v["km"], 2),
                "avg_kmh": round(v["avg_sum"] / v["rides"], 2),
            }
            for k, v in sorted(bucket.items(), reverse=True)
        ]

    return rows(monthly), rows(weekly), rows(yearly)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Eager load koně u jízd, aby šablona nemusela lazy-loadovat po zavření session
    with get_session() as s:
        horses = s.exec(select(Horse).order_by(Horse.name)).all()
        rides = s.exec(
            select(Ride)
            .options(selectinload(Ride.horse))
            .order_by(Ride.ride_date.desc(), Ride.id.desc())
        ).all()
        monthly_rows, weekly_rows, yearly_rows = accum_periods(rides)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "horses": horses,
            "rides": rides,
            "monthly": monthly_rows,
            "weekly": weekly_rows,
            "yearly": yearly_rows,
        },
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload_gpx(
    request: Request,
    file: UploadFile,
    horse_name: str = Form(default=""),
    ride_title: str = Form(default=""),
    ride_date: str = Form(default=""),
):
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
                s.add(horse)
                s.commit()
                s.refresh(horse)

        rd = (
            date.fromisoformat(ride_date.strip())
            if ride_date.strip()
            else (metrics.start_time or datetime.utcnow()).date()
        )

        r = Ride(
            title=ride_title.strip() or file.filename,
            ride_date=rd,
            distance_km=round(metrics.distance_m / 1000.0, 3),
            total_time_s=metrics.total_time_s,
            moving_time_s=metrics.moving_time_s,
            avg_speed_kmh=round(metrics.avg_speed_mps * 3.6, 2),
            avg_moving_speed_kmh=round(metrics.avg_moving_speed_mps * 3.6, 2),
            max_speed_kmh=round(metrics.max_speed_mps * 3.6, 2),
            ascent_m=round(metrics.ascent_m, 1),
            descent_m=round(metrics.descent_m, 1),
            min_elev_m=metrics.min_elev_m,
            max_elev_m=metrics.max_elev_m,
            gpx_path=str(dest),
            horse_id=horse.id if horse else None,
        )
        s.add(r)
        s.commit()
        s.refresh(r)
        return RedirectResponse(url=f"/ride/{r.id}", status_code=303)


@app.get("/ride/{ride_id}", response_class=HTMLResponse)
def ride_detail(request: Request, ride_id: int):
    with get_session() as s:
        ride = s.exec(
            select(Ride).options(selectinload(Ride.horse)).where(Ride.id == ride_id)
        ).first()
        if not ride:
            return HTMLResponse("Ride not found", status_code=404)

    text = Path(ride.gpx_path).read_text(encoding="utf-8", errors="ignore")
    pts = parse_gpx_points(text)
    metrics = compute_metrics(pts)
    speed_ts = [{"t": (t.isoformat() if t else None), "v": v * 3.6} for t, v in metrics.speed_series]
    elev_profile = [{"d": d / 1000.0, "e": e} for d, e in metrics.elev_profile]

    # pro mapu – seznam [lat, lon, ele]
    gpx = gpxpy.parse(text)
    coords = []
    for trk in gpx.tracks:
        for seg in trk.segments:
            for p in seg.points:
                coords.append([p.latitude, p.longitude, p.elevation if p.elevation is not None else 0.0])

    return templates.TemplateResponse(
        "ride_detail.html",
        {
            "request": request,
            "ride": ride,
            "horse": ride.horse,
            "speed_ts": speed_ts,
            "elev_profile": elev_profile,
            "coords": coords,
        },
    )


@app.get("/gpx/{ride_id}")
def download_gpx(ride_id: int):
    with get_session() as s:
        ride = s.get(Ride, ride_id)
        if not ride:
            return HTMLResponse("Ride not found", status_code=404)
        return FileResponse(
            path=ride.gpx_path,
            filename=Path(ride.gpx_path).name,
            media_type="application/gpx+xml",
        )


@app.get("/horse/{horse_id}", response_class=HTMLResponse)
def horse_detail(request: Request, horse_id: int):
    with get_session() as s:
        horse = s.get(Horse, horse_id)
        if not horse:
            return HTMLResponse("Kůň nenalezen", status_code=404)
        rides = s.exec(
            select(Ride)
            .where(Ride.horse_id == horse_id)
            .order_by(Ride.ride_date.desc())
        ).all()

    # souhrny & top jízdy
    monthly_rows, weekly_rows, yearly_rows = accum_periods(rides)
    km_month = [{"period": m["period"], "km": m["km"]} for m in monthly_rows]
    km_year = [{"period": y["period"], "km": y["km"]} for y in yearly_rows]
    avg_month = [{"period": m["period"], "avg": m["avg_kmh"]} for m in monthly_rows]

    top_long = sorted(rides, key=lambda r: r.distance_km, reverse=True)[:3]
    top_fast = sorted(rides, key=lambda r: r.max_speed_kmh, reverse=True)[:3]
    top_climb = sorted(rides, key=lambda r: r.ascent_m, reverse=True)[:3]

    return templates.TemplateResponse(
        "horse_detail.html",
        {
            "request": request,
            "horse": horse,
            "rides": rides,
            "monthly": monthly_rows,
            "weekly": weekly_rows,
            "yearly": yearly_rows,
            "km_month": km_month,
            "km_year": km_year,
            "avg_month": avg_month,
            "top_long": top_long,
            "top_fast": top_fast,
            "top_climb": top_climb,
        },
    )


@app.get("/export/horse/{horse_id}.csv")
def export_csv_horse(horse_id: int):
    with get_session() as s:
        rides = s.exec(
            select(Ride).where(Ride.horse_id == horse_id).order_by(Ride.ride_date)
        ).all()
        horse = s.get(Horse, horse_id)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["horse", "id", "date", "title", "distance_km", "avg_speed_kmh", "max_speed_kmh", "ascent_m", "descent_m"]
    )
    for r in rides:
        w.writerow(
            [
                horse.name if horse else "",
                r.id,
                r.ride_date.isoformat(),
                r.title or "",
                f"{r.distance_km:.3f}",
                f"{r.avg_speed_kmh:.2f}",
                f"{r.max_speed_kmh:.2f}",
                f"{r.ascent_m:.0f}",
                f"{r.descent_m:.0f}",
            ]
        )
    buf.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="{(horse.name if horse else "horse")}_export.csv"'}
    return StreamingResponse(iter([buf.read()]), media_type="text/csv", headers=headers)


@app.get("/backup.zip")
def backup_zip():
    with get_session() as s:
        horses = s.exec(select(Horse).order_by(Horse.id)).all()
        rides = s.exec(select(Ride).order_by(Ride.id)).all()
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        # horses.csv
        hout = io.StringIO()
        w = csv.writer(hout)
        w.writerow(["id", "name", "walk_trot_kmh", "trot_canter_kmh", "notes"])
        for h in horses:
            w.writerow([h.id, h.name, h.walk_trot_kmh or "", h.trot_canter_kmh or "", h.notes or ""])
        z.writestr("horses.csv", hout.getvalue())

        # rides.csv
        rout = io.StringIO()
        w = csv.writer(rout)
        w.writerow(
            ["id", "date", "title", "horse_id", "distance_km", "avg_speed_kmh", "max_speed_kmh", "ascent_m", "descent_m", "gpx_path"]
        )
        for r in rides:
            w.writerow(
                [r.id, r.ride_date.isoformat(), r.title or "", r.horse_id or "", r.distance_km, r.avg_speed_kmh, r.max_speed_kmh, r.ascent_m, r.descent_m, r.gpx_path]
            )
        z.writestr("rides.csv", rout.getvalue())
    mem.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="backup.zip"'}
    return StreamingResponse(iter([mem.read()]), media_type="application/zip", headers=headers)
