#!/usr/bin/env python3
"""Collect satellite evidence and assess visually confirmed roof recovery (UTC)."""
import argparse
import csv
import io
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from shapely.geometry import Point, mapping, shape

UTC = timezone.utc
STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
OBJECTS = {"object_1": (57.0543, 53.9876), "object_2": (55.8242, 52.0514)}  # lat, lon
SOURCES = ["VIIRS_SNPP_NRT", "VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT", "MODIS_NRT"]
RULES = [
    "Compare the same building contour with a dated pre-event baseline and a damaged post-event scene.",
    "Check local clouds, shadows, snow, image registration and sufficient spatial resolution.",
    "New materials alone indicate partial repair; recovery requires complete visible roof coverage.",
    "Confirm on a second usable acquisition at least 5 days later, with no intervening negative or unreviewed usable date.",
    "Recovery is interval-censored: (last incomplete observation, first complete observation].",
    "This measures visible roof recovery, not structural certification or production restart.",
]


def parse_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        if len(value) != 10:
            raise ValueError("Specify a timezone for a timestamp; date-only values mean 00:00 UTC")
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt):
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def distance_km(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def radius_bbox(lat, lon, radius=3):
    d = radius / 6371.0088
    dy = math.degrees(d)
    dx = math.degrees(math.asin(math.sin(d) / math.cos(math.radians(lat))))
    return [lon-dx, lat-dy, lon+dx, lat+dy]


def session():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])))
    return s


def firms_events(s, key, source, lat, lon, start):
    end = start + timedelta(hours=48)
    # The API accepts calendar days: a non-midnight 48h window spans THREE dates.
    days = (end.date() - start.date()).days + 1
    bbox = ",".join(f"{v:.7f}" for v in radius_bbox(lat, lon))
    url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{source}/{bbox}/{days}/{start.date()}"
    try:
        response = s.get(url, timeout=(15, 90))
    except requests.RequestException:
        raise RuntimeError(f"FIRMS {source}: transport error (URL hidden to protect MAP_KEY)") from None
    if response.status_code != 200:
        raise RuntimeError(f"FIRMS {source}: HTTP {response.status_code}")
    if response.text.strip().lower() == "no data":
        return []
    reader = csv.DictReader(io.StringIO(response.text))
    required = {"latitude", "longitude", "acq_date", "acq_time"}
    if not required.issubset(reader.fieldnames or []):
        raise RuntimeError(f"FIRMS {source}: invalid CSV or unavailable date range; check data_availability")
    events = []
    for row in reader:
        hhmm = row["acq_time"].strip().zfill(4)
        dt = parse_time(f'{row["acq_date"]}T{hhmm[:2]}:{hhmm[2:]}:00Z')
        y, x = float(row["latitude"]), float(row["longitude"])
        km = distance_km(lat, lon, y, x)
        if start <= dt < end and km <= 3:
            events.append({"source": source, "observed_at": iso(dt), "latitude": y,
                "longitude": x, "distance_km": round(km, 4), "raw": row})
    return sorted(events, key=lambda e: e["observed_at"])


def load_aois(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("type") != "FeatureCollection":
        raise ValueError("AOI must be a WGS84 GeoJSON FeatureCollection")
    result = {}
    for f in doc["features"]:
        oid = f.get("properties", {}).get("object_id")
        if oid not in OBJECTS or oid in result:
            raise ValueError("Use one feature per object_1/object_2, without duplicates")
        g = shape(f["geometry"])
        if g.geom_type not in {"Polygon", "MultiPolygon"} or g.is_empty or not g.is_valid:
            raise ValueError(f"{oid}: a valid building Polygon/MultiPolygon is required")
        west, south, east, north = g.bounds
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError("GeoJSON coordinates must be WGS84 [longitude, latitude]")
        lat, lon = OBJECTS[oid]
        if g.distance(Point(lon, lat)) > 0.1:
            raise ValueError(f"{oid}: polygon is far from the supplied centre; check coordinate order")
        result[oid] = g
    if not result:
        raise ValueError("No AOI features supplied")
    return result


def sentinel_scenes(client, aoi, start, end):
    search = client.search(collections=["sentinel-2-l2a"], intersects=mapping(aoi),
        datetime=f"{iso(start)}/{iso(end)}", query={"eo:cloud_cover": {"lt": 15}},
        method="POST", limit=100)  # no max_items: follow all pagination links
    scenes = {}
    for item in search.items():
        dt = item.datetime
        cloud = item.properties.get("eo:cloud_cover")
        if not dt or not item.geometry or cloud is None:
            continue
        footprint = shape(item.geometry)
        if not footprint.is_valid or not footprint.covers(aoi):
            continue
        if not (start <= dt <= end and float(cloud) < 15):
            continue
        scenes[item.id] = {"scene_id": item.id, "acquired_at": iso(dt),
            "cloud_cover": float(cloud), "stac_href": item.get_self_href(),
            "assets": {k: {"href": a.href, "roles": a.roles, "type": a.media_type}
                for k, a in item.assets.items()}}
    return sorted(scenes.values(), key=lambda x: (x["acquired_at"], x["cloud_cover"], x["scene_id"]))


def sampling_bins(scenes, start, end, step):
    bins = []
    left = start
    while left < end:
        right = min(end, left + timedelta(days=step))
        candidates = [s for s in scenes if left <= parse_time(s["acquired_at"]) < right
                      or (right == end and parse_time(s["acquired_at"]) == end)]
        best = min(candidates, key=lambda s: (s["cloud_cover"], s["acquired_at"]), default=None)
        bins.append({"start": iso(left), "end": iso(right),
            "scene_id": best["scene_id"] if best else None, "available_count": len(candidates)})
        left = right
    return bins


def collect(args):
    event, end = parse_time(args.t_event), parse_time(args.end)
    if end <= event:
        raise ValueError("--end must be after --t-event")
    key = os.environ.get("FIRMS_MAP_KEY")
    if not key:
        raise ValueError("Set FIRMS_MAP_KEY in the environment")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "manifest.json").exists() or (out / "reviews.template.json").exists():
        raise ValueError("Output already contains a collection; choose a new directory")
    aois = load_aois(args.aoi)
    client = Client.open(args.stac_url, stac_io=StacApiIO(timeout=60, max_retries=3))
    manifest = {"t_event": iso(event), "end": iso(end), "calendar_timezone": "UTC",
        "stac_url": args.stac_url, "rules": RULES, "objects": {}}
    reviews = {"objects": {}}
    with session() as s:
        for oid, aoi in aois.items():
            lat, lon = OBJECTS[oid]
            obj = {"centre_lat_lon": [lat, lon], "aoi": mapping(aoi), "thermal": [],
                "firms_sources": [], "scenes": []}
            for source in args.sources:
                try:
                    hits = firms_events(s, key, source, lat, lon, event)
                    obj["thermal"].extend(hits)
                    obj["firms_sources"].append({"source": source, "status": "ok", "count": len(hits)})
                except (RuntimeError, ValueError) as e:
                    obj["firms_sources"].append({"source": source, "status": "error", "error": str(e)})
            obj["thermal"].sort(key=lambda x: x["observed_at"])
            obj["scenes"] = sentinel_scenes(client, aoi, event, end)
            obj["sampling_bins"] = sampling_bins(obj["scenes"], event, end, args.step_days)
            manifest["objects"][oid] = obj
            reviews["objects"][oid] = {"baseline_reference": "", "damage_scene_id": "",
                "damage_verified": False, "reviewer": "", "scenes": [
                    {"scene_id": scene["scene_id"], "local_clear": None,
                     "resolution_sufficient": None, "same_contour_verified": None,
                     "new_roof_material": None, "roof_complete": None, "evidence": ""}
                    for scene in obj["scenes"]]}
    dump(out / "manifest.json", manifest)
    dump(out / "reviews.template.json", reviews)
    print(f"Collection saved: {out.resolve()}")


def assess_object(obj, review, event):
    result = {"status": "unconfirmed", "t_material": None, "t_recovery": None,
        "scene_id": None, "confirmation_scene_id": None, "ttr_days": None, "interval": None}
    scenes = {s["scene_id"]: s for s in obj["scenes"]}
    observations = {}
    for r in review["scenes"]:
        sid = r["scene_id"]
        if sid not in scenes or sid in observations:
            raise ValueError(f"Unknown or duplicate reviewed scene: {sid}")
        for field in ("local_clear", "resolution_sufficient", "same_contour_verified",
                      "new_roof_material", "roof_complete"):
            if r.get(field) is not None and type(r[field]) is not bool:
                raise ValueError(f"{sid}: {field} must be true, false or null")
        observations[sid] = r
    if not (review.get("baseline_reference", "").strip() and review.get("reviewer", "").strip()
            and review.get("damage_verified") is True):
        return result
    damage = observations.get(review.get("damage_scene_id"))
    def usable(r):
        return r and all(r.get(k) is True for k in
            ("local_clear", "resolution_sufficient", "same_contour_verified")) and bool(r.get("evidence", "").strip())
    if not usable(damage) or damage.get("roof_complete") is not False:
        return result
    damage_time = parse_time(scenes[damage["scene_id"]]["acquired_at"])
    if damage_time < event:
        raise ValueError("Damage observation precedes the event")
    candidate = None
    last_incomplete = damage_time
    # Group reprocessed products/overlapping tiles by UTC acquisition date.
    groups = {}
    for scene in obj["scenes"]:
        dt = parse_time(scene["acquired_at"])
        if dt >= damage_time:
            groups.setdefault(dt.date(), []).append(scene)
    for date in sorted(groups):
        day = [(s, observations.get(s["scene_id"])) for s in groups[date]]
        valid = [(s, r) for s, r in day if usable(r)]
        negatives = [(s, r) for s, r in valid if r.get("roof_complete") is False]
        positives = [(s, r) for s, r in valid if r.get("roof_complete") is True
                     and r.get("new_roof_material") is True]
        materials = [(s, r) for s, r in valid if r.get("new_roof_material") is True]
        if materials and result["t_material"] is None:
            result["t_material"] = min(s["acquired_at"] for s, _ in materials)
        if negatives:
            candidate = None
            last_incomplete = max(parse_time(s["acquired_at"]) for s, _ in negatives)
        elif positives:
            scene = min((s for s, _ in positives), key=lambda s: s["acquired_at"])
            if candidate is None:
                candidate = scene
            elif parse_time(scene["acquired_at"]) - parse_time(candidate["acquired_at"]) >= timedelta(days=5):
                recovery = parse_time(candidate["acquired_at"])
                result.update(status="visually_confirmed", t_recovery=iso(recovery),
                    scene_id=candidate["scene_id"], confirmation_scene_id=scene["scene_id"],
                    ttr_days=(recovery.date()-event.date()).days,
                    interval={"after": iso(last_incomplete), "on_or_before": iso(recovery),
                        "ttr_calendar_days_min": (last_incomplete.date()-event.date()).days,
                        "ttr_calendar_days_max": (recovery.date()-event.date()).days})
                return result
        else:
            # Explicitly unusable dates may be skipped; pending usable dates break confirmation.
            if any(r is None or all(r.get(k) is not False for k in
                   ("local_clear", "resolution_sufficient", "same_contour_verified")) for _, r in day):
                candidate = None
    return result


def assess(args):
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    reviews = json.loads(Path(args.reviews).read_text(encoding="utf-8"))
    if set(reviews["objects"]) != set(manifest["objects"]):
        raise ValueError("Review object IDs must match the manifest")
    event = parse_time(manifest["t_event"])
    result = {"t_event": iso(event), "calendar_timezone": "UTC", "rules": RULES,
        "objects": {oid: assess_object(obj, reviews["objects"][oid], event)
                    for oid, obj in manifest["objects"].items()}}
    dump(args.out, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--t-event", required=True)
    c.add_argument("--end", required=True)
    c.add_argument("--aoi", required=True, help="WGS84 polygons with properties.object_id")
    c.add_argument("--step-days", type=int, choices=range(5, 11), default=7)
    c.add_argument("--sources", nargs="+", default=SOURCES,
                   choices=SOURCES + ["VIIRS_SNPP_SP", "VIIRS_NOAA20_SP", "MODIS_SP"])
    c.add_argument("--stac-url", default=STAC_URL)
    c.add_argument("--out", default="data/geospatial_recovery/run")
    c.set_defaults(func=collect)
    a = sub.add_parser("assess")
    a.add_argument("--manifest", required=True)
    a.add_argument("--reviews", required=True)
    a.add_argument("--out", default="ttr_result.json")
    a.set_defaults(func=assess)
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as e:
        # Never print request URLs: FIRMS places the API key in its URL path.
        message = str(e) if isinstance(e, (ValueError, RuntimeError)) else type(e).__name__
        parser.exit(1, f"Error: {message}\n")


if __name__ == "__main__":
    main()
