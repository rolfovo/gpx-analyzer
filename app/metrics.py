from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Tuple, Optional
import math, gpxpy

def hav_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat = math.radians(lat2-lat1); dlon = math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return 2*R*math.asin(math.sqrt(a))

@dataclass
class Metrics:
    distance_m: float
    total_time_s: int
    avg_speed_mps: float
    max_speed_mps: float
    ascent_m: float
    descent_m: float
    min_elev_m: Optional[float]
    max_elev_m: Optional[float]
    start_time: Optional[datetime]
    speed_series: List[Tuple[Optional[datetime], float]]
    elev_profile: List[Tuple[float, float]]

def parse_gpx_points(text: str):
    g = gpxpy.parse(text)
    pts = []
    for trk in g.tracks:
        for seg in trk.segments:
            for p in seg.points:
                t = p.time
                if t and t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                pts.append((t, p.latitude, p.longitude, p.elevation or 0.0))
    return pts

def compute_metrics(pts) -> Metrics:
    if not pts:
        return Metrics(0,0,0,0,0,0,None,None,None,[],[])
    total_d=0; ascent=0; descent=0; max_v=0
    min_e=pts[0][3]; max_e=pts[0][3]
    start=pts[0][0]; end=pts[-1][0] if pts[-1][0] else start
    speed_series=[]; elev_profile=[]; acc_d=0
    for i in range(1,len(pts)):
        t1,lat1,lon1,e1 = pts[i-1]
        t2,lat2,lon2,e2 = pts[i]
        dt = (t2-t1).total_seconds() if (t1 and t2) else 1.0
        if dt<=0: dt=1.0
        d = hav_m(lat1,lon1,lat2,lon2)
        total_d += d; acc_d += d
        v = d/dt; max_v=max(max_v,v)
        speed_series.append((t2,v))
        elev_profile.append((acc_d, e2))
        de = e2-e1
        if de>0: ascent+=de
        else: descent+=-de
        min_e=min(min_e,e2); max_e=max(max_e,e2)
    total_t = int((end-start).total_seconds()) if (start and end) else 0
    avg = (total_d/total_t) if total_t>0 else 0.0
    return Metrics(total_d,total_t,avg,max_v,ascent,descent,min_e,max_e,start,speed_series,elev_profile)
