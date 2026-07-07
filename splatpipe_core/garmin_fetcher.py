"""
Garmin Connect auto-fetch — downloads GPS track + activity stats when a
new gallery item publishes.

Uses the garminconnect library (unofficial Garmin Connect API via garth OAuth).
Credentials stored once in config/garmin_config.json; tokens cached in
config/garmin_tokens/ and refreshed automatically.

Matching logic: searches Garmin activities within ±search_days of the job
creation date. If multiple activities found, picks the one with the most
elevation gain (most likely the field hike / survey session).
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date as _date
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "garmin_config.json"
_TOKEN_DIR   = Path(__file__).parent.parent / "config" / "garmin_tokens"

_GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1"}


# ── Config helpers ────────────────────────────────────────────────────────────

def is_configured() -> bool:
    """Return True if garmin_config.json exists with credentials."""
    try:
        cfg = _load_config()
        return bool(cfg.get("email")) and bool(cfg.get("password"))
    except Exception:
        return False


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Garmin config not found at {_CONFIG_PATH}. "
            "Create it with: {{\"email\": \"you@example.com\", \"password\": \"...\", \"search_days\": 1}}"
        )
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


# ── Authentication ────────────────────────────────────────────────────────────

def _get_client():
    """Return an authenticated Garmin client, using cached tokens when possible."""
    from garminconnect import Garmin

    cfg        = _load_config()
    email      = cfg["email"]
    password   = cfg["password"]
    token_file = _TOKEN_DIR / f"{email.replace('@', '_').replace('.', '_')}.json"

    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    client = Garmin(email=email, password=password)

    if token_file.exists():
        try:
            client.garth.loads(token_file.read_text(encoding="utf-8"))
            client.display_name  # lightweight API call to validate token
            print("  [garmin] Using cached token")
            return client
        except Exception:
            print("  [garmin] Cached token invalid — re-authenticating")

    client.login()
    token_file.write_text(client.garth.dumps(), encoding="utf-8")
    print(f"  [garmin] Authenticated as {email}, token cached")
    return client


# ── Activity search ───────────────────────────────────────────────────────────

def _find_best_activity(client, target_date: _date, search_days: int) -> Optional[dict]:
    """
    Search activities within ±search_days of target_date.
    Returns the activity with the highest elevation gain (most likely the
    field survey hike). Returns None if nothing found.
    """
    start = (target_date - timedelta(days=search_days)).isoformat()
    end   = (target_date + timedelta(days=search_days)).isoformat()

    print(f"  [garmin] Searching activities {start} → {end}")
    activities = client.get_activities_by_date(start, end, activitytype=None)

    if not activities:
        print("  [garmin] No activities found in date range")
        return None

    # Prefer activities with most elevation gain (field surveys tend to involve
    # significant ascent/descent). Fall back to longest duration.
    def score(a: dict) -> float:
        gain = a.get("elevationGain") or 0
        dur  = a.get("duration") or 0
        return gain * 10 + dur

    best = max(activities, key=score)
    print(
        f"  [garmin] Best match: {best.get('activityName')} "
        f"({best.get('activityType', {}).get('typeKey', '?')}) "
        f"on {best.get('startTimeLocal', '?')[:10]} "
        f"— {(best.get('distance') or 0)/1000:.1f} km, "
        f"↑{best.get('elevationGain', 0):.0f} m"
    )
    return best


# ── GPX parsing ───────────────────────────────────────────────────────────────

def _parse_gpx(gpx_bytes: bytes, max_points: int = 500) -> list[dict]:
    """
    Parse GPX XML → list of {lat, lon, alt} dicts, subsampled to max_points.
    Handles both GPX 1.0 and 1.1 namespaces.
    """
    try:
        root = ET.fromstring(gpx_bytes)
    except ET.ParseError as e:
        print(f"  [garmin] GPX parse error: {e}")
        return []

    # Try both namespace variants
    trkpts: list[ET.Element] = []
    for ns in (_GPX_NS["gpx"], ""):
        prefix = f"{{{ns}}}" if ns else ""
        pts = root.findall(f".//{prefix}trkpt")
        if pts:
            trkpts = pts
            break

    if not trkpts:
        print("  [garmin] No track points found in GPX")
        return []

    step = max(1, len(trkpts) // max_points)
    route = []
    for pt in trkpts[::step]:
        try:
            lat = float(pt.attrib["lat"])
            lon = float(pt.attrib["lon"])
            ele_el = pt.find(f"{{{_GPX_NS['gpx']}}}ele") or pt.find("ele")
            alt = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
            route.append({"lat": round(lat, 6), "lon": round(lon, 6), "alt": round(alt, 1)})
        except (KeyError, ValueError, AttributeError):
            continue

    print(f"  [garmin] GPX: {len(trkpts)} points → {len(route)} sampled")
    return route


# ── Stats builder ─────────────────────────────────────────────────────────────

def _build_activity_stats(activity: dict, route: list[dict]) -> dict:
    """Convert a Garmin activity dict + route into the ActivityStats schema."""

    alts = [p["alt"] for p in route if p["alt"] != 0]

    # Elevation gain/loss — prefer Garmin's computed values, fall back to
    # computing from track deltas if Garmin returns None
    gain = activity.get("elevationGain")
    loss = activity.get("elevationLoss")

    if gain is None and alts:
        deltas = [alts[i+1] - alts[i] for i in range(len(alts)-1)]
        gain = sum(d for d in deltas if d > 0)
        loss = abs(sum(d for d in deltas if d < 0))

    activity_type_map = {
        "hiking":        "Hike",
        "trail_running": "Trail Run",
        "running":       "Run",
        "cycling":       "Ride",
        "mountain_biking": "MTB",
        "swimming":      "Swim",
        "kayaking":      "Kayak",
        "walking":       "Walk",
    }
    type_key    = (activity.get("activityType") or {}).get("typeKey", "")
    pretty_type = activity_type_map.get(type_key, type_key.replace("_", " ").title() or "Activity")

    start_raw = activity.get("startTimeLocal", "")
    try:
        start_iso = datetime.strptime(start_raw, "%Y-%m-%d %H:%M:%S").isoformat()
    except ValueError:
        start_iso = start_raw

    return {
        "activityName":    activity.get("activityName", "Untitled"),
        "activityType":    pretty_type,
        "startDate":       start_iso,
        "distanceKm":      round((activity.get("distance") or 0) / 1000, 2),
        "elevationGainM":  round(gain or 0, 1),
        "elevationLossM":  round(loss or 0, 1),
        "durationSec":     int(activity.get("elapsedDuration") or activity.get("duration") or 0),
        "movingTimeSec":   int(activity.get("movingDuration") or activity.get("duration") or 0),
        "maxElevationM":   round(max(alts), 1) if alts else 0,
        "minElevationM":   round(min(alts), 1) if alts else 0,
        "route":           route,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_and_store(job_id: str, job_date: _date, db) -> bool:
    """
    Top-level function called by pipeline_runner after gallery publish.
    Finds the best matching Garmin activity, downloads GPX, builds
    activityStats, and writes to Firestore gallery/{job_id}.

    Returns True if activity data was stored, False otherwise.
    """
    if not is_configured():
        print("  [garmin] garmin_config.json not found — skipping auto-fetch")
        return False

    try:
        cfg         = _load_config()
        search_days = int(cfg.get("search_days", 1))
        client      = _get_client()

        activity = _find_best_activity(client, job_date, search_days)
        if not activity:
            return False

        activity_id = activity["activityId"]

        # Download GPX
        print(f"  [garmin] Downloading GPX for activity {activity_id}…")
        from garminconnect import Garmin
        gpx_bytes = client.download_activity(
            activity_id, dl_fmt=Garmin.ActivityDownloadFormat.GPX
        )
        route = _parse_gpx(gpx_bytes)

        stats = _build_activity_stats(activity, route)

        # Write to Firestore
        db.collection("gallery").document(job_id).update({
            "stravaActivityId": activity_id,  # reuse same field as Strava plan
            "activityStats":    stats,
        })
        print(
            f"  [garmin] ✅ Activity stored: {stats['activityName']} "
            f"({stats['distanceKm']} km, ↑{stats['elevationGainM']} m, "
            f"↓{stats['elevationLossM']} m, {len(route)} route points)"
        )
        return True

    except Exception as e:
        print(f"  [garmin] Non-fatal error: {e}")
        return False
