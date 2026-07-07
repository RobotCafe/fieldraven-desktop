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
    """Return True if Garmin is usable: config has email AND either
    a cached token exists or a password is present for initial auth."""
    try:
        cfg = _load_config()
        email = cfg.get("email")
        if not email:
            return False
        # Token cached → ready without password
        if _token_file_for(email).exists():
            return True
        # No token yet → need password for first-time login
        return bool(cfg.get("password"))
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

def _token_file_for(email: str) -> Path:
    safe = email.replace("@", "_at_").replace(".", "_")
    return _TOKEN_DIR / f"{safe}.json"


def _get_client(prompt_mfa=None):
    """
    Return an authenticated Garmin client (garminconnect 0.3.x API).

    Token storage uses api.client.dumps() / login(tokenstore=json_string).
    prompt_mfa is a constructor arg: Garmin(..., prompt_mfa=callback).
    Pipeline runs with prompt_mfa=None — if no cached token, Garmin fetch
    is skipped gracefully. Run scripts/setup_garmin.py once to cache tokens.
    """
    from garminconnect import Garmin

    cfg        = _load_config()
    email      = cfg["email"]
    password   = cfg.get("password", "")   # empty once scrubbed after first login
    token_file = _token_file_for(email)

    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    # Try cached token — login() handles expiry/refresh automatically
    if token_file.exists():
        try:
            client = Garmin(email=email, password=password)
            result = client.login(tokenstore=token_file.read_text(encoding="utf-8"))
            if result[0] is None:  # (None, None) = clean login, no MFA needed
                print(f"  [garmin] Token valid — {client.full_name or email}")
                # Refresh serialized token in case it was silently rotated
                token_file.write_text(client.client.dumps(), encoding="utf-8")
                return client
        except Exception as e:
            print(f"  [garmin] Cached token invalid ({e}) — re-authenticating")

    # Fresh login (only works interactively if MFA is required)
    if prompt_mfa is None:
        raise RuntimeError(
            "No cached Garmin token. Run scripts/setup_garmin.py once to authenticate."
        )

    client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
    client.login()
    token_file.write_text(client.client.dumps(), encoding="utf-8")

    # Remove the password from config — it's no longer needed now that
    # the OAuth token is cached. The token refreshes automatically.
    _scrub_password_from_config(email)

    print(f"  [garmin] Authenticated as {client.full_name or email}, token cached")
    return client


def _scrub_password_from_config(email: str) -> None:
    """Overwrite garmin_config.json removing the password field.
    The OAuth token is sufficient for all subsequent runs."""
    try:
        cfg = _load_config()
        if "password" not in cfg:
            return
        safe_cfg = {k: v for k, v in cfg.items() if k != "password"}
        _CONFIG_PATH.write_text(
            json.dumps(safe_cfg, indent=2), encoding="utf-8"
        )
        print(f"  [garmin] Password removed from {_CONFIG_PATH.name} "
              "(token is now the only credential on disk)")
    except Exception as e:
        print(f"  [garmin] Note: could not scrub password from config: {e}")


# ── Activity search ───────────────────────────────────────────────────────────

def _parse_garmin_time(s: str) -> Optional[datetime]:
    """Parse a Garmin startTimeLocal string to a naive datetime."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _find_activity_by_overlap(
    client,
    session_start: datetime,
    session_end: datetime,
    search_days: int,
) -> Optional[dict]:
    """
    Find the Garmin activity whose recording window overlaps most with the
    field session [session_start, session_end].

    Falls back to elevation-gain scoring when no temporal overlap found.
    """
    search_from = (session_start.date() - timedelta(days=search_days)).isoformat()
    search_to   = (session_end.date()   + timedelta(days=search_days)).isoformat()

    print(f"  [garmin] Searching activities {search_from} → {search_to} "
          f"(session {session_start.strftime('%H:%M')}–{session_end.strftime('%H:%M')})")

    activities = client.get_activities_by_date(search_from, search_to, activitytype=None)
    if not activities:
        print("  [garmin] No activities found")
        return None

    session_dur = (session_end - session_start).total_seconds()
    best: Optional[dict] = None
    best_overlap = 0.0

    for act in activities:
        act_start = _parse_garmin_time(act.get("startTimeLocal", ""))
        if act_start is None:
            continue
        act_dur = float(act.get("duration") or act.get("elapsedDuration") or 0)
        act_end = act_start + timedelta(seconds=act_dur)

        overlap_start = max(session_start, act_start)
        overlap_end   = min(session_end,   act_end)
        overlap_sec   = max(0.0, (overlap_end - overlap_start).total_seconds())

        if overlap_sec > best_overlap:
            best_overlap = overlap_sec
            best = act

    if best and best_overlap > 0:
        pct = best_overlap / session_dur * 100 if session_dur > 0 else 0
        print(
            f"  [garmin] Matched '{best.get('activityName')}' — "
            f"{pct:.0f}% overlap ({best_overlap/60:.0f} min)"
        )
        return best

    # No temporal overlap found — fall back to elevation-gain heuristic
    print("  [garmin] No temporal overlap — falling back to elevation-gain score")
    return _find_best_activity_by_date(client, session_start.date(), search_days)


def _find_best_activity_by_date(client, target_date: _date, search_days: int) -> Optional[dict]:
    """
    Fallback: search ±search_days and pick activity with most elevation gain.
    """
    start = (target_date - timedelta(days=search_days)).isoformat()
    end   = (target_date + timedelta(days=search_days)).isoformat()

    activities = client.get_activities_by_date(start, end, activitytype=None)
    if not activities:
        return None

    def score(a: dict) -> float:
        return (a.get("elevationGain") or 0) * 10 + (a.get("duration") or 0)

    best = max(activities, key=score)
    print(
        f"  [garmin] Fallback match: '{best.get('activityName')}' "
        f"on {best.get('startTimeLocal', '?')[:10]} "
        f"— {(best.get('distance') or 0)/1000:.1f} km"
    )
    return best


# ── GPX parsing ───────────────────────────────────────────────────────────────

def _find_hr_in_extensions(pt: ET.Element) -> Optional[int]:
    """Extract heart rate from a GPX track point's extensions (any namespace)."""
    for ext in pt.iter():
        local = ext.tag.split("}")[-1] if "}" in ext.tag else ext.tag
        if local.lower() == "hr" and ext.text:
            try:
                return int(float(ext.text))
            except ValueError:
                pass
    return None


def _parse_gpx(gpx_bytes: bytes, max_points: int = 500) -> list[dict]:
    """
    Parse GPX XML → list of {lat, lon, alt, hr?} dicts, subsampled to max_points.
    Handles both GPX 1.0 and 1.1 namespaces. Extracts HR from extensions when present.
    """
    try:
        root = ET.fromstring(gpx_bytes)
    except ET.ParseError as e:
        print(f"  [garmin] GPX parse error: {e}")
        return []

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
            entry: dict = {"lat": round(lat, 6), "lon": round(lon, 6), "alt": round(alt, 1)}
            hr = _find_hr_in_extensions(pt)
            if hr is not None:
                entry["hr"] = hr
            route.append(entry)
        except (KeyError, ValueError, AttributeError):
            continue

    has_hr = sum(1 for p in route if "hr" in p)
    print(f"  [garmin] GPX: {len(trkpts)} points → {len(route)} sampled ({has_hr} with HR)")
    return route


def _fmt_pace(sec_per_km: float) -> str:
    """Convert seconds-per-km to MM:SS string."""
    m = int(sec_per_km) // 60
    s = int(sec_per_km) % 60
    return f"{m}:{s:02d}"


def _parse_laps(activity_details: dict) -> list[dict]:
    """Extract lap data from full activity details dict."""
    raw_laps = activity_details.get("laps") or []
    laps = []
    for i, lap in enumerate(raw_laps, 1):
        dist_m   = lap.get("distance") or 0
        dist_km  = dist_m / 1000
        dur_sec  = int(lap.get("elapsedDuration") or lap.get("duration") or 0)
        asc      = lap.get("totalAscent")
        desc     = lap.get("totalDescent")
        avg_hr   = lap.get("averageHR")
        max_hr   = lap.get("maxHR")
        cad      = lap.get("averageCadence")
        cal      = lap.get("calories")

        entry: dict = {
            "index":       i,
            "durationSec": dur_sec,
            "distanceKm":  round(dist_km, 2),
        }
        if avg_hr is not None:   entry["avgHR"]        = int(avg_hr)
        if max_hr is not None:   entry["maxHR"]        = int(max_hr)
        if asc    is not None:   entry["totalAscentM"]  = round(asc, 0)
        if desc   is not None:   entry["totalDescentM"] = round(desc, 0)
        if cad    is not None:   entry["avgCadence"]    = int(cad)
        if cal    is not None:   entry["calories"]      = int(cal)
        if dist_km > 0 and dur_sec > 0:
            entry["avgPace"] = _fmt_pace(dur_sec / dist_km)

        laps.append(entry)
    return laps


# ── Stats builder ─────────────────────────────────────────────────────────────

def _build_activity_stats(activity: dict, route: list[dict], laps: list[dict]) -> dict:
    """Convert a Garmin activity dict + route + laps into the ActivityStats schema."""

    alts = [p["alt"] for p in route if p["alt"] != 0]

    gain = activity.get("elevationGain")
    loss = activity.get("elevationLoss")
    if gain is None and alts:
        deltas = [alts[i+1] - alts[i] for i in range(len(alts)-1)]
        gain = sum(d for d in deltas if d > 0)
        loss = abs(sum(d for d in deltas if d < 0))

    activity_type_map = {
        "hiking":          "Hike",
        "trail_running":   "Trail Run",
        "running":         "Run",
        "cycling":         "Ride",
        "mountain_biking": "MTB",
        "swimming":        "Swim",
        "kayaking":        "Kayak",
        "walking":         "Walk",
    }
    type_key    = (activity.get("activityType") or {}).get("typeKey", "")
    pretty_type = activity_type_map.get(type_key, type_key.replace("_", " ").title() or "Activity")

    start_raw = activity.get("startTimeLocal", "")
    try:
        start_iso = datetime.strptime(start_raw, "%Y-%m-%d %H:%M:%S").isoformat()
    except ValueError:
        start_iso = start_raw

    dist_km  = round((activity.get("distance") or 0) / 1000, 2)
    dur_sec  = int(activity.get("elapsedDuration") or activity.get("duration") or 0)
    move_sec = int(activity.get("movingDuration") or activity.get("duration") or 0)

    stats: dict = {
        "activityName":   activity.get("activityName", "Untitled"),
        "activityType":   pretty_type,
        "startDate":      start_iso,
        "distanceKm":     dist_km,
        "elevationGainM": round(gain or 0, 1),
        "elevationLossM": round(loss or 0, 1),
        "durationSec":    dur_sec,
        "movingTimeSec":  move_sec,
        "maxElevationM":  round(max(alts), 1) if alts else 0,
        "minElevationM":  round(min(alts), 1) if alts else 0,
        "route":          route,
    }

    # Optional extended fields
    avg_hr = activity.get("averageHR")
    max_hr = activity.get("maxHR")
    cal    = activity.get("calories")
    if avg_hr is not None:  stats["avgHR"]    = int(avg_hr)
    if max_hr is not None:  stats["maxHR"]    = int(max_hr)
    if cal    is not None:  stats["calories"] = int(cal)
    if dist_km > 0 and move_sec > 0:
        stats["avgPace"] = _fmt_pace(move_sec / dist_km)
    if laps:
        stats["laps"] = laps

    return stats


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_and_store(
    job_id: str,
    job_date: _date,
    db,
    session_start: Optional[datetime] = None,
    session_end:   Optional[datetime] = None,
) -> bool:
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

        if session_start and session_end:
            activity = _find_activity_by_overlap(client, session_start, session_end, search_days)
        else:
            activity = _find_best_activity_by_date(client, job_date, search_days)
        if not activity:
            return False

        activity_id = activity["activityId"]

        # Download GPX (track + HR)
        print(f"  [garmin] Downloading GPX for activity {activity_id}…")
        from garminconnect import Garmin
        gpx_bytes = client.download_activity(
            activity_id, dl_fmt=Garmin.ActivityDownloadFormat.GPX
        )
        route = _parse_gpx(gpx_bytes)

        # Fetch full activity details for laps + HR summary
        print(f"  [garmin] Fetching activity details for laps…")
        try:
            details = client.get_activity(activity_id)
            laps = _parse_laps(details)
            print(f"  [garmin] {len(laps)} laps parsed")
        except Exception as e:
            print(f"  [garmin] Could not fetch laps: {e}")
            laps = []

        stats = _build_activity_stats(activity, route, laps)

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
