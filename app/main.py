
from fastapi import FastAPI, UploadFile, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlalchemy.orm import selectinload
from pathlib import Path
from datetime import datetime, date
import csv, io, uuid, zipfile, os

# Optional imports for remote storage / HTTP fetches
try:
    import requests  # type: ignore
except Exception:
    requests = None  # we'll fallback to urllib
import urllib.request

try:
    import boto3  # type: ignore
    from botocore.config import Config  # type: ignore
except Exception:
    boto3 = None
    Config = None

import gpxpy

from .db import init_db, get_session
from .models import Horse, Ride
from .metrics import parse_gpx_points, compute_metrics

# ---------------- Persistent storage paths ----------------
app = FastAPI(title="GPX Analyzer – Horse Dashboard")
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = (Path("/data/gpx") if Path("/data").exists() else BASE_DIR / "data" / "gpx")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# --------------- Optional Cloudflare R2 config --------------
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")
R2_PUBLIC_BASEURL = os.getenv("R2_PUBLIC_BASEURL")

def r2_client():
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_BUCKET):
        return None
    if boto3 is None or Config is None:
        return None
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

# ------------------- Static/Template mounts ----------------
app.mount("/static", StaticFiles(directory=str((Path(__file__).parent / "static").resolve())), name="static")
templates = Jinja2Templates(directory=str((Path(__file__).parent / "templates").resolve()))

@app.on_event("startup")
def on_startup():
    init_db()

# ---------------------- Helpers ----------------------------
def accum_periods(rides):
    monthly, weekly, yearly = {}, {}, {}
    for r in rides:
        ym = r.ride_date.strftime("%Y-%m")
        iso = r.ride_date.isocalendar()
        yw = f"{iso[0]}-W{iso[1]:02d}"
        yy = r.ride_date.strftime("%Y")
        for bucket, key in ((monthly, ym),(weekly, yw),(yearly, yy)):
            d = bucket.setdefault(key, {"km":0.0,"rides":0,"avg_sum":0.0})
            d["km"] += r.distance_km; d["rides"] += 1; d["avg_sum"] += r.avg_speed_kmh
    def rows(bucket):
        return [{"period":k,"rides":v["rides"],"km":round(v["km"],2),"avg_kmh":round(v["avg_sum"]/v["rides"],2)} for k,v in sorted(bucket.items(), reverse=True)]
    return rows(monthly), rows(weekly), rows(yearly)

# ---------------------- Routes -----------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with get_session() as s:
        horses = s.exec(select(Horse).order_by(Horse.name)).all()
        rides = s.exec(
            select(Ride)
            .options(selectinload(Ride.horse))
            .order_by(Ride.ride_date.desc(), Ride.id.desc())
        ).all()
        monthly_rows, weekly_rows, yearly_rows = accum_periods(rides)
    return templates.TemplateResponse("index.html", {
        "request": request, "horses": horses, "rides": rides,
        "monthly": monthly_rows, "weekly": weekly_rows, "yearly": yearly_rows
    })

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

    # store either to R2 (if configured) or local disk (/data)
    r2 = r2_client()
    if r2:
        key = f"{uid}.gpx"
        r2.put_object(Bucket=R2_BUCKET, Key=key, Body=contents, ContentType="application/gpx+xml")
        gpx_ref = f"{R2_PUBLIC_BASEURL}/{key}" if R2_PUBLIC_BASEURL else f"s3://{R2_BUCKET}/{key}"
    else:
        dest = DATA_DIR / f"{uid}.gpx"
        dest.write_bytes(contents)
        gpx_ref = str(dest)

    # compute metrics to fill ride fields
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
            # convert moving speed from m/s to km/h
            avg_moving_speed_kmh=round(metrics.avg_moving_speed_mps*3.6, 2),
            max_speed_kmh=round(metrics.max_speed_mps*3.6, 2),
            ascent_m=round(metrics.ascent_m, 1),
            descent_m=round(metrics.descent_m, 1),
            min_elev_m=metrics.min_elev_m,
            max_elev_m=metrics.max_elev_m,
            gpx_path=gpx_ref,
            horse_id=horse.id if horse else None
        )
        s.add(r); s.commit(); s.refresh(r)
        return RedirectResponse(url=f"/ride/{r.id}", status_code=303)

@app.get("/ride/{ride_id}", response_class=HTMLResponse)
def ride_detail(request: Request, ride_id: int):
    with get_session() as s:
        ride = s.exec(
            select(Ride).options(selectinload(Ride.horse)).where(Ride.id == ride_id)
        ).first()
        if not ride:
            return HTMLResponse("Ride not found", status_code=404)

    # load GPX text no matter if local or URL
    p = Path(ride.gpx_path)
    missing_gpx, text = False, None
    if ride.gpx_path.startswith("http"):
        if requests is not None:
            resp = requests.get(ride.gpx_path, timeout=20)
            if resp.ok: text = resp.text
            else: missing_gpx = True
        else:
            try:
                with urllib.request.urlopen(ride.gpx_path, timeout=20) as f:
                    text = f.read().decode("utf-8", errors="ignore")
            except Exception:
                missing_gpx = True
    elif ride.gpx_path.startswith("s3://"):
        r2 = r2_client()
        if r2:
            _, _, bucket_key = ride.gpx_path.partition("s3://")
            bkt, _, key = bucket_key.partition("/")
            obj = r2.get_object(Bucket=bkt, Key=key)
            text = obj["Body"].read().decode("utf-8", errors="ignore")
        else:
            missing_gpx = True
    else:
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="ignore")
        else:
            missing_gpx = True

    speed_ts, elev_profile, coords = [], [], []
    if not missing_gpx and text:
        pts = parse_gpx_points(text)
        metrics = compute_metrics(pts)
        speed_ts = [{"t": (t.isoformat() if t else None), "v": v*3.6} for t, v in metrics.speed_series]
        elev_profile = [{"d": d/1000.0, "e": e} for d, e in metrics.elev_profile]
        gpx = gpxpy.parse(text)
        for trk in gpx.tracks:
            for seg in trk.segments:
                for pnt in seg.points:
                    coords.append([pnt.latitude, pnt.longitude, pnt.elevation or 0.0])

    return templates.TemplateResponse(
        "ride_detail.html",
        {"request": request, "ride": ride, "horse": ride.horse, "speed_ts": speed_ts,
         "elev_profile": elev_profile, "coords": coords, "missing_gpx": missing_gpx}
    )

@app.post("/ride/{ride_id}/delete")
def delete_ride(ride_id: int):
    with get_session() as s:
        ride = s.get(Ride, ride_id)
        if not ride:
            raise HTTPException(status_code=404, detail="Jízda nenalezena")
        # try delete local file
        try:
            if ride.gpx_path and ride.gpx_path.startswith("/"):
                p = Path(ride.gpx_path)
                if p.exists(): p.unlink()
        except Exception:
            pass
        s.delete(ride); s.commit()
    return RedirectResponse("/", status_code=303)

@app.get("/gpx/{ride_id}")
def download_gpx(ride_id: int):
    with get_session() as s:
        ride = s.get(Ride, ride_id)
        if not ride:
            return HTMLResponse("Ride not found", status_code=404)

        if ride.gpx_path.startswith("http"):
            return RedirectResponse(ride.gpx_path)
        if ride.gpx_path.startswith("s3://"):
            r2 = r2_client()
            if not r2: return HTMLResponse("Storage not configured", status_code=500)
            _, _, bucket_key = ride.gpx_path.partition("s3://")
            bkt, _, key = bucket_key.partition("/")
            url = r2.generate_presigned_url("get_object", Params={"Bucket": bkt, "Key": key}, ExpiresIn=600)
            return RedirectResponse(url)

        p = Path(ride.gpx_path)
        if not p.exists():
            return HTMLResponse("GPX soubor už není k dispozici.", status_code=404)
        return FileResponse(path=str(p), filename=p.name, media_type="application/gpx+xml")

@app.get("/horse/{horse_id}", response_class=HTMLResponse)
def horse_detail(request: Request, horse_id: int):
    with get_session() as s:
        horse = s.get(Horse, horse_id)
        if not horse:
            return HTMLResponse("Kůň nenalezen", status_code=404)
        rides = s.exec(
            select(Ride).where(Ride.horse_id == horse_id).order_by(Ride.ride_date.desc())
        ).all()

    stats = {
        "count": len(rides),
        "km": sum(r.distance_km for r in rides),
        "avg": (sum(r.avg_speed_kmh for r in rides) / len(rides)) if rides else 0,
        "max": max((r.max_speed_kmh for r in rides), default=0),
    }

    monthly_rows, weekly_rows, yearly_rows = accum_periods(rides)
    month_series = [{"label": m["period"], "km": m["km"]} for m in monthly_rows]
    week_series = [{"label": w["period"], "km": w["km"]} for w in weekly_rows]
    year_series = [{"label": y["period"], "km": y["km"]} for y in yearly_rows]

    top_long = sorted(rides, key=lambda r: r.distance_km, reverse=True)[:3]
    top_fast = sorted(rides, key=lambda r: r.max_speed_kmh, reverse=True)[:3]
    top_climb = sorted(rides, key=lambda r: r.ascent_m, reverse=True)[:3]

    return templates.TemplateResponse("horse_detail.html", {
        "request": request, "horse": horse, "rides": rides,
        "monthly": monthly_rows, "weekly": weekly_rows, "yearly": yearly_rows,
        "month_series": month_series, "week_series": week_series, "year_series": year_series,
        "stats": stats,
        "top_long": top_long, "top_fast": top_fast, "top_climb": top_climb,
        "q_from": None, "q_to": None,
    })

# ---- Horses management ----
@app.get("/horses", response_class=HTMLResponse)
def horses_page(request: Request):
    with get_session() as s:
        horses = s.exec(select(Horse).options(selectinload(Horse.rides)).order_by(Horse.name)).all()
    return templates.TemplateResponse("horses.html", {"request": request, "horses": horses})

@app.post("/horses/new")
def create_horse(name: str = Form(...), notes: str = Form(default="")):
    with get_session() as s:
        if s.exec(select(Horse).where(Horse.name == name.strip())).first():
            return RedirectResponse("/horses", status_code=303)
        h = Horse(name=name.strip(), notes=(notes.strip() or None))
        s.add(h); s.commit()
    return RedirectResponse("/horses", status_code=303)

@app.post("/horse/{horse_id}/update")
def update_horse(horse_id: int, name: str = Form(...), notes: str = Form(default=""),
                 walk_trot_kmh: float | None = Form(default=None),
                 trot_canter_kmh: float | None = Form(default=None)):
    with get_session() as s:
        h = s.get(Horse, horse_id)
        if not h: raise HTTPException(404, "Kůň nenalezen")
        h.name = name.strip()
        h.notes = notes.strip() or None
        h.walk_trot_kmh = walk_trot_kmh
        h.trot_canter_kmh = trot_canter_kmh
        s.add(h); s.commit()
    return RedirectResponse("/horses", status_code=303)

@app.post("/horse/{horse_id}/delete")
def delete_horse(horse_id: int):
    with get_session() as s:
        h = s.get(Horse, horse_id)
        if not h: raise HTTPException(404, "Kůň nenalezen")
        rides = s.exec(select(Ride).where(Ride.horse_id == horse_id)).all()
        for r in rides:
            r.horse_id = None
            s.add(r)
        s.delete(h); s.commit()
    return RedirectResponse("/horses", status_code=303)

@app.get("/backup.zip")
def backup_zip():
    with get_session() as s:
        horses = s.exec(select(Horse).order_by(Horse.id)).all()
        rides = s.exec(select(Ride).order_by(Ride.id)).all()
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        hout = io.StringIO(); w = csv.writer(hout)
        w.writerow(["id","name","walk_trot_kmh","trot_canter_kmh","notes"])
        for h in horses: w.writerow([h.id,h.name,h.walk_trot_kmh or "", h.trot_canter_kmh or "", h.notes or ""])
        z.writestr("horses.csv", hout.getvalue())
        rout = io.StringIO(); w = csv.writer(rout)
        w.writerow(["id","date","title","horse_id","distance_km","avg_speed_kmh","max_speed_kmh","ascent_m","descent_m","gpx_path"])
        for r in rides:
            w.writerow([r.id,r.ride_date.isoformat(),r.title or "", r.horse_id or "", r.distance_km, r.avg_speed_kmh, r.max_speed_kmh, r.ascent_m, r.descent_m, r.gpx_path])
        z.writestr("rides.csv", rout.getvalue())
    mem.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="backup.zip"'}
    return StreamingResponse(iter([mem.read()]), media_type="application/zip", headers=headers)
