from __future__ import annotations
from fastapi import FastAPI, UploadFile, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlalchemy.orm import selectinload
from pathlib import Path
from datetime import date, datetime
import uuid, json, io, csv, zipfile, requests

from .db import init_db, get_session
from .models import Horse, Ride
from .metrics import parse_gpx_points, compute_metrics, hav_m
import gpxpy

app = FastAPI(title="GPX Analyzer")
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = (Path("/data/gpx") if Path("/data").exists() else BASE_DIR / "data" / "gpx")
DATA_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str((Path(__file__).parent / "static").resolve())), name="static")
templates = Jinja2Templates(directory=str((Path(__file__).parent / "templates").resolve()))

@app.on_event("startup")
def startup():
    init_db()

def _to_float(val):
    if val is None: return None
    s = str(val).strip().replace(',', '.')
    return float(s) if s else None

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with get_session() as s:
        horses = s.exec(select(Horse).order_by(Horse.name)).all()
        rides = s.exec(select(Ride).options(selectinload(Ride.horse)).order_by(Ride.ride_date.desc(), Ride.id.desc())).all()
    return templates.TemplateResponse("index.html", {"request": request, "horses": horses, "rides": rides})

@app.post("/upload")
async def upload(file: UploadFile, horse_name: str = Form(default=""), ride_title: str = Form(default=""), ride_date: str = Form(default="")):
    text = (await file.read()).decode("utf-8", errors="ignore")
    uid = uuid.uuid4().hex; dest = DATA_DIR / f"{uid}.gpx"; dest.write_text(text, encoding="utf-8")
    pts = parse_gpx_points(text); m = compute_metrics(pts)
    with get_session() as s:
        horse = None
        if horse_name.strip():
            horse = s.exec(select(Horse).where(Horse.name==horse_name.strip())).first()
            if not horse: horse = Horse(name=horse_name.strip()); s.add(horse); s.commit(); s.refresh(horse)
        rd = date.fromisoformat(ride_date) if ride_date else (m.start_time or datetime.utcnow()).date()
        r = Ride(title=ride_title or file.filename, ride_date=rd, horse_id=horse.id if horse else None,
                 distance_km=round(m.distance_m/1000,3), total_time_s=m.total_time_s,
                 avg_speed_kmh=round(m.avg_speed_mps*3.6,2), max_speed_kmh=round(m.max_speed_mps*3.6,2),
                 ascent_m=round(m.ascent_m,1), descent_m=round(m.descent_m,1),
                 min_elev_m=m.min_elev_m, max_elev_m=m.max_elev_m, gpx_path=str(dest))
        s.add(r); s.commit(); s.refresh(r)
        return RedirectResponse(f"/ride/{r.id}", status_code=303)

@app.get("/ride/{ride_id}", response_class=HTMLResponse)
def ride_detail(request: Request, ride_id: int):
    with get_session() as s:
        ride = s.exec(select(Ride).options(selectinload(Ride.horse)).where(Ride.id==ride_id)).first()
        if not ride: raise HTTPException(404, "Jízda nenalezena")
    p = Path(ride.gpx_path); missing=False
    if str(ride.gpx_path).startswith("http"):
        try:
            resp = requests.get(ride.gpx_path, timeout=20); resp.raise_for_status()
            text = resp.text
        except Exception: missing=True; text=""
    else:
        if p.exists(): text = p.read_text(encoding="utf-8", errors="ignore")
        else: missing=True; text=""
    coords=[]; segs=[]; speed=[]; elev=[]
    gait={'walk':0.0,'trot':0.0,'canter':0.0}
    walk=(ride.horse.walk_trot_kmh if ride.horse else None) or 7.0
    trot=(ride.horse.trot_canter_kmh if ride.horse else None) or 13.0
    if not missing and text:
        pts = parse_gpx_points(text)
        from .metrics import compute_metrics as cm
        m = cm(pts)
        speed=[{'t': (t.isoformat() if t else None), 'v': v*3.6} for t,v in m.speed_series]
        elev=[{'d': d/1000.0, 'e': e} for d,e in m.elev_profile]
        g=gpxpy.parse(text)
        for trk in g.tracks:
            for seg in trk.segments:
                ps=seg.points
                if len(ps)<2: continue
                coords.extend([[q.latitude,q.longitude,q.elevation or 0.0] for q in ps])
                for i in range(1,len(ps)):
                    a,b=ps[i-1],ps[i]
                    dt=(b.time-a.time).total_seconds() if (a.time and b.time) else 1.0
                    if dt<=0: dt=1.0
                    d=hav_m(a.latitude,a.longitude,b.latitude,b.longitude)
                    v=(d/dt)*3.6
                    segs.append({'lat1':a.latitude,'lon1':a.longitude,'lat2':b.latitude,'lon2':b.longitude,'v':v})
                    if v<walk: gait['walk']+=d
                    elif v<trot: gait['trot']+=d
                    else: gait['canter']+=d
    total=sum(gait.values()) or 1.0
    gait_stats={k:{'km':round(v/1000,2),'pct':round(v/total*100,1)} for k,v in gait.items()}
    return templates.TemplateResponse("ride_detail.html", {
        "request": request, "ride": ride, "horse": ride.horse, "missing_gpx": missing,
        "coords_json": json.dumps(coords), "segments_json": json.dumps(segs),
        "speed_json": json.dumps(speed), "elev_json": json.dumps(elev),
        "gait": gait_stats, "walk_trot": walk, "trot_canter": trot
    })

# Horses mgmt
from fastapi import Form
@app.get("/horses", response_class=HTMLResponse)
def horses_page(request: Request):
    from .models import Horse
    with get_session() as s:
        horses = s.exec(select(Horse).options(selectinload(Horse.rides)).order_by(Horse.name)).all()
    return templates.TemplateResponse("horses.html", {"request": request, "horses": horses})

@app.post("/horses/new")
def horses_new(name: str = Form(...), notes: str = Form(default="")):
    with get_session() as s:
        h = Horse(name=name.strip(), notes=notes or None); s.add(h); s.commit()
    return RedirectResponse("/horses", status_code=303)

@app.post("/horse/{horse_id}/update")
def horse_update(horse_id: int, name: str = Form(...), notes: str = Form(default=""),
                 walk_trot_kmh: str = Form(default=""), trot_canter_kmh: str = Form(default="")):
    with get_session() as s:
        h = s.get(Horse, horse_id); h.name=name.strip(); h.notes=(notes or None)
        h.walk_trot_kmh=_to_float(walk_trot_kmh); h.trot_canter_kmh=_to_float(trot_canter_kmh); s.add(h); s.commit()
    return RedirectResponse("/horses", status_code=303)

@app.post("/horse/{horse_id}/delete")
def horse_delete(horse_id: int):
    with get_session() as s:
        from .models import Ride
        rds = s.exec(select(Ride).where(Ride.horse_id==horse_id)).all()
        for r in rds: r.horse_id=None; s.add(r)
        h=s.get(Horse, horse_id); s.delete(h); s.commit()
    return RedirectResponse("/horses", status_code=303)

# GPX & backup
@app.get("/gpx/{ride_id}")
def get_gpx(ride_id:int):
    with get_session() as s:
        r=s.get(Ride, ride_id)
        if not r: raise HTTPException(404,"Ride not found")
        p=Path(r.gpx_path)
        if not p.exists(): return HTMLResponse("GPX nenalezen", status_code=404)
        return FileResponse(str(p), filename=p.name, media_type="application/gpx+xml")

@app.get("/backup.zip")
def backup_zip():
    from .models import Horse, Ride
    with get_session() as s:
        horses = s.exec(select(Horse)).all()
        rides = s.exec(select(Ride)).all()
    mem=io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("horses.json", json.dumps([h.model_dump() for h in horses], ensure_ascii=False))
        z.writestr("rides.json", json.dumps([r.model_dump() for r in rides], ensure_ascii=False))
    mem.seek(0)
    return StreamingResponse(iter([mem.read()]), media_type="application/zip", headers={"Content-Disposition":'attachment; filename="backup.zip"'})
