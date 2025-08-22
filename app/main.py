from __future__ import annotations
from fastapi import FastAPI, UploadFile, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlalchemy.orm import selectinload
from pathlib import Path
from datetime import date, datetime
from typing import Optional
import uuid, json, io, zipfile, os

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

def _to_float(val: Optional[str]) -> Optional[float]:
    if val is None: 
        return None
    s = str(val).strip().replace(',', '.')
    return float(s) if s else None

# ---------- HOME (přehled) ----------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    with get_session() as s:
        horses = s.exec(select(Horse).order_by(Horse.name)).all()
        rides = s.exec(select(Ride)
                       .options(selectinload(Ride.horse))
                       .order_by(Ride.ride_date.desc(), Ride.id.desc())).all()
    return templates.TemplateResponse("index.html", {"request": request, "horses": horses, "rides": rides})

# ---------- UPLOAD ----------
@app.post("/upload")
async def upload(file: UploadFile, horse_name: str = Form(default=""), ride_title: str = Form(default=""), ride_date: str = Form(default="")):
    text = (await file.read()).decode("utf-8", errors="ignore")
    uid = uuid.uuid4().hex; dest = DATA_DIR / f"{uid}.gpx"; dest.write_text(text, encoding="utf-8")
    pts = parse_gpx_points(text); m = compute_metrics(pts)
    with get_session() as s:
        horse = None
        if horse_name.strip():
            horse = s.exec(select(Horse).where(Horse.name==horse_name.strip())).first()
            if not horse:
                horse = Horse(name=horse_name.strip()); s.add(horse); s.commit(); s.refresh(horse)
        rd = date.fromisoformat(ride_date) if ride_date else (m.start_time or datetime.utcnow()).date()

        distance_km = round(m.distance_m/1000, 3)
        total_time_s = int(m.total_time_s or 0)
        moving_time_s = int(getattr(m, "moving_time_s", 0) or total_time_s)
        avg_speed_kmh = round(m.avg_speed_mps * 3.6, 2)
        max_speed_kmh = round(m.max_speed_mps * 3.6, 2)
        ascent_m = round(m.ascent_m, 1)
        descent_m = round(m.descent_m, 1)
        min_elev_m = m.min_elev_m
        max_elev_m = m.max_elev_m

        if moving_time_s > 0:
            avg_moving_speed_kmh = round(distance_km / (moving_time_s / 3600.0), 2)
        else:
            avg_moving_speed_kmh = 0.0

        r = Ride(
            title=ride_title or file.filename,
            ride_date=rd,
            horse_id=horse.id if horse else None,
            distance_km=distance_km,
            total_time_s=total_time_s,
            moving_time_s=moving_time_s,
            avg_speed_kmh=avg_speed_kmh,
            avg_moving_speed_kmh=avg_moving_speed_kmh,
            max_speed_kmh=max_speed_kmh,
            ascent_m=ascent_m,
            descent_m=descent_m,
            min_elev_m=min_elev_m,
            max_elev_m=max_elev_m,
            gpx_path=str(dest)
        )
        s.add(r); s.commit(); s.refresh(r)
        return RedirectResponse(f"/ride/{r.id}", status_code=303)

# ---------- GPX download ----------
@app.get("/gpx/{ride_id}")
def download_gpx(ride_id: int):
    with get_session() as s:
        r = s.get(Ride, ride_id)
        if not r: raise HTTPException(404, "Jízda nenalezena")
        p = Path(r.gpx_path)
        if not p.exists(): raise HTTPException(404, "GPX soubor chybí")
        return FileResponse(str(p), media_type="application/gpx+xml", filename=p.name)

# ---------- Ride detail ----------
@app.get("/ride/{ride_id}", response_class=HTMLResponse)
def ride_detail(request: Request, ride_id: int):
    with get_session() as s:
        r = s.exec(select(Ride).where(Ride.id==ride_id).options(selectinload(Ride.horse))).first()
        if not r: raise HTTPException(404, "Jízda nenalezena")
        horse = r.horse
    p = Path(r.gpx_path)
    missing_gpx = not p.exists()
    coords_json = "[]"; segments_json="[]"; speed_json="[]"; elev_json="[]"
    walk_trot = horse.walk_trot_kmh if horse and horse.walk_trot_kmh is not None else 7.0
    trot_canter = horse.trot_canter_kmh if horse and horse.trot_canter_kmh is not None else 13.0
    gait = {"walk":{"km":0,"pct":0},"trot":{"km":0,"pct":0},"canter":{"km":0,"pct":0}}

    if not missing_gpx:
        import gpxpy
        text = p.read_text(encoding="utf-8", errors="ignore")
        g = gpxpy.parse(text)
        from .metrics import hav_m, parse_gpx_points, compute_metrics

        coords = []
        segs = []
        walk_km = trot_km = canter_km = 0.0
        for trk in g.tracks:
            for seg in trk.segments:
                ps = seg.points
                if len(ps) < 2: 
                    continue
                coords.extend([[q.latitude, q.longitude, q.elevation or 0.0] for q in ps])
                for i in range(1, len(ps)):
                    a, b = ps[i-1], ps[i]
                    dt = (b.time - a.time).total_seconds() if (a.time and b.time) else 1.0
                    if dt <= 0: dt = 1.0
                    d = hav_m(a.latitude, a.longitude, b.latitude, b.longitude)
                    v = (d / dt) * 3.6
                    segs.append({"lat1": a.latitude, "lon1": a.longitude, "lat2": b.latitude, "lon2": b.longitude, "v": v})
                    if v < walk_trot: walk_km += d/1000.0
                    elif v < trot_canter: trot_km += d/1000.0
                    else: canter_km += d/1000.0

        total_km = (walk_km + trot_km + canter_km) or 1e-9
        gait = {
            "walk": {"km": round(walk_km,2), "pct": round(100*walk_km/total_km, 2)},
            "trot": {"km": round(trot_km,2), "pct": round(100*trot_km/total_km, 2)},
            "canter": {"km": round(canter_km,2), "pct": round(100*canter_km/total_km, 2)},
        }
        coords_json = json.dumps([[c[0], c[1]] for c in coords])
        segments_json = json.dumps(segs)

        pts = parse_gpx_points(text)
        m = compute_metrics(pts)
        sp = [{"t": (t.isoformat() if t else None), "v": v*3.6} for t,v in m.speed_series]
        ev = [{"d": round(d/1000.0,3), "e": e} for d,e in m.elev_profile]
        speed_json = json.dumps(sp)
        elev_json = json.dumps(ev)

    return templates.TemplateResponse("ride_detail.html", {
        "request": request, "ride": r, "horse": horse,
        "coords_json": coords_json, "segments_json": segments_json,
        "speed_json": speed_json, "elev_json": elev_json,
        "walk_trot": walk_trot, "trot_canter": trot_canter,
        "gait": gait, "missing_gpx": missing_gpx
    })

# ---------- Delete ride ----------
@app.post("/ride/{ride_id}/delete")
def delete_ride(ride_id: int):
    with get_session() as s:
        r = s.get(Ride, ride_id)
        if not r: raise HTTPException(404, "Jízda nenalezena")
        try:
            p = Path(r.gpx_path)
            if p.exists(): p.unlink()
        except Exception:
            pass
        s.delete(r); s.commit()
    return RedirectResponse("/", status_code=303)

# ---------- Horses list & CRUD ----------
@app.get("/horses", response_class=HTMLResponse)
def horses_page(request: Request):
    with get_session() as s:
        horses = s.exec(
            select(Horse).options(selectinload(Horse.rides)).order_by(Horse.name)
        ).all()
    return templates.TemplateResponse("horses.html", {"request": request, "horses": horses})

@app.post("/horses/new")
def horse_new(name: str = Form(...), notes: str = Form(default="")):
    with get_session() as s:
        h = Horse(name=name.strip(), notes=notes.strip() or None)
        s.add(h); s.commit()
    return RedirectResponse("/horses", status_code=303)

@app.post("/horse/{horse_id}/update")
def horse_update(horse_id: int, name: str = Form(...), notes: str = Form(default=""),
                 walk_trot_kmh: str = Form(default=""), trot_canter_kmh: str = Form(default="")):
    with get_session() as s:
        h = s.get(Horse, horse_id)
        if not h: raise HTTPException(404, "Kůň nenalezen")
        h.name = name.strip()
        h.notes = notes.strip() or None
        h.walk_trot_kmh = _to_float(walk_trot_kmh)
        h.trot_canter_kmh = _to_float(trot_canter_kmh)
        s.add(h); s.commit()
    return RedirectResponse("/horses", status_code=303)

@app.post("/horse/{horse_id}/delete")
def horse_delete(horse_id: int):
    with get_session() as s:
        h = s.get(Horse, horse_id)
        if not h: raise HTTPException(404, "Kůň nenalezen")
        rides = s.exec(select(Ride).where(Ride.horse_id==horse_id)).all()
        for r in rides:
            r.horse_id = None; s.add(r)
        s.delete(h); s.commit()
    return RedirectResponse("/horses", status_code=303)

# ---------- Horse detail ----------
@app.get("/horse/{horse_id}", response_class=HTMLResponse)
def horse_detail(request: Request, horse_id: int):
    q_from = request.query_params.get("from")
    q_to = request.query_params.get("to")
    with get_session() as s:
        h = s.get(Horse, horse_id)
        if not h: raise HTTPException(404, "Kůň nenalezen")
        stmt = select(Ride).where(Ride.horse_id==horse_id)
        if q_from: stmt = stmt.where(Ride.ride_date >= date.fromisoformat(q_from))
        if q_to: stmt = stmt.where(Ride.ride_date <= date.fromisoformat(q_to))
        rides = s.exec(stmt.order_by(Ride.ride_date.desc())).all()
    km = sum(r.distance_km for r in rides) if rides else 0.0
    count = len(rides)
    avg = (sum(r.avg_speed_kmh for r in rides) / count) if count else 0.0
    mx = max((r.max_speed_kmh for r in rides), default=0.0)
    from collections import defaultdict
    months = defaultdict(float)
    for r in rides:
        key = f"{r.ride_date.year}-{r.ride_date.month:02d}"
        months[key] += float(r.distance_km)
    month_series = [{"label": k, "km": round(v,2)} for k,v in sorted(months.items())]
    return templates.TemplateResponse("horse_detail.html", {
        "request": request, "horse": h, "rides": rides,
        "stats": {"km": round(km,2), "count": count, "avg": round(avg,2), "max": round(mx,2)},
        "month_series": json.dumps(month_series), "q_from": q_from, "q_to": q_to
    })

# ---------- Backup ZIP ----------
@app.get("/backup.zip")
def backup_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        with get_session() as s:
            horses = s.exec(select(Horse)).all()
            rides = s.exec(select(Ride)).all()
            z.writestr("horses.json", json.dumps([h.dict() for h in horses], ensure_ascii=False))
            z.writestr("rides.json", json.dumps([r.dict() for r in rides], ensure_ascii=False))
        if DATA_DIR.exists():
            for p in DATA_DIR.glob("*.gpx"):
                z.write(p, f"gpx/{p.name}")
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={"Content-Disposition":"attachment; filename=backup.zip"})
