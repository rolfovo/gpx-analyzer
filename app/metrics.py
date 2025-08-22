from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple, Optional
import gpxpy
from math import radians, sin, cos, asin, sqrt
from datetime import datetime, timezone

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    return R * c

@dataclass
class Point:
    t: Optional[datetime]
    lat: float
    lon: float
    ele: Optional[float]

@dataclass
class Metrics:
    distance_m: float
    total_time_s: int
    moving_time_s: int
    avg_speed_mps: float
    avg_moving_speed_mps: float
    max_speed_mps: float
    ascent_m: float
    descent_m: float
    min_elev_m: Optional[float]
    max_elev_m: Optional[float]
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    speed_series: List[Tuple[datetime, float]]  # (timestamp, m/s)
    elev_profile: List[Tuple[float, float]]     # (distance_m, elevation_m)

def parse_gpx_points(gpx_text: str) -> List[Point]:
    gpx = gpxpy.parse(gpx_text)
    pts: List[Point] = []
    for trk in gpx.tracks:
        for seg in trk.segments:
            for p in seg.points:
                t = p.time.replace(tzinfo=timezone.utc) if p.time and p.time.tzinfo is None else p.time
                pts.append(Point(t=t, lat=p.latitude, lon=p.longitude, ele=p.elevation))
    return pts

def compute_metrics(pts: List[Point], moving_threshold_mps: float = 1.0) -> Metrics:
    if not pts:
        return Metrics(0.0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, None, None, None, None, [], [])
    distance_m = 0.0
    total_time_s = 0
    moving_time_s = 0
    max_speed_mps = 0.0
    ascent = 0.0
    descent = 0.0
    min_ele = pts[0].ele if pts[0].ele is not None else None
    max_ele = pts[0].ele if pts[0].ele is not None else None
    speed_series = []
    elev_series = []
    start_time = pts[0].t
    end_time = pts[-1].t

    cum_dist = 0.0
    prev = pts[0]
    for cur in pts[1:]:
        d = haversine(prev.lat, prev.lon, cur.lat, cur.lon)
        dt = 0.0
        if prev.t and cur.t:
            dt = (cur.t - prev.t).total_seconds()
            total_time_s += int(dt) if dt > 0 else 0
        distance_m += d
        cum_dist += d

        if prev.ele is not None and cur.ele is not None:
            de = cur.ele - prev.ele
            if de > 0: ascent += de
            else: descent += -de
            if min_ele is None or cur.ele < min_ele: min_ele = float(cur.ele)
            if max_ele is None or cur.ele > max_ele: max_ele = float(cur.ele)

        if dt > 0:
            v = d/dt
            speed_series.append((cur.t or prev.t, v))
            if v > max_speed_mps: max_speed_mps = v
            if v >= moving_threshold_mps: moving_time_s += int(dt)

        if cur.ele is not None: elev_series.append((cum_dist, float(cur.ele)))
        prev = cur

    avg_speed_mps = (distance_m / total_time_s) if total_time_s > 0 else 0.0
    avg_moving_speed_mps = (distance_m / moving_time_s) if moving_time_s > 0 else 0.0

    return Metrics(distance_m, total_time_s, moving_time_s, avg_speed_mps,
                   avg_moving_speed_mps, max_speed_mps, ascent, descent,
                   min_ele, max_ele, start_time, end_time, speed_series, elev_series)
