"""
Coros Training Hub auto-fetch — downloads activity stats when a new gallery
item publishes, using the same session-time matching approach as
garmin_fetcher.py (see that module for the Garmin equivalent; both are
wired into the same call site in pipeline_runner.py).

There is no official public Coros API. This talks directly to Coros's own
Training Hub web API, using the same endpoints/auth flow as the open-source
reference implementation at github.com/cygnusb/coros-mcp (unofficial,
not affiliated with COROS, MIT-licensed at time of writing). Confirmed via
that project's source:
  - Base URL:    https://team{region}api.coros.com  (region: "eu"/"us"/"cn")
  - Login:       POST /account/login
                 {"account": email, "accountType": 2, "pwd": md5(password)}
                 -> {"data": {"accessToken": ..., "userId": ...}}
  - Auth header: {"accessToken": <token>, "yfheader": '{"userId": <id>}'}
  - Activities:  GET /activity/query
                 ?startDay=YYYYMMDD&endDay=YYYYMMDD&pageNumber=1&size=30
  - Detail:      POST /activity/detail/query
                 form: {labelId, userId, sportType}

**Known gap, not yet solved:** no confirmed endpoint for GPS route/track
points. The reference project's activity-detail call explicitly strips a
field called `gpsLightDuration` before returning — and that name reads more
like a GPS-signal/lock-duration indicator than an actual lat/lon track, so
it's not even clear that's the right field to un-strip. Until a real GPS
source is found, Coros activities here contribute time-window matching +
stats only (name, distance, elevation, duration) — captureLocation still
falls back to the manual map-pick in the Job Details modal
(_write_capture_location in pipeline_runner.py) when there's no mobile-job
GPS. This module still sets captureLocation from a Coros activity's stats
IF a location ever does turn up in the response (defensive lookup below),
so it costs nothing to leave that path in.

Credentials: config/coros_config.json — {"email", "password", "region",
"search_days"}. No MFA step in the reference implementation (unlike Garmin),
so no interactive one-time setup script is needed — this logs in directly
and caches the token in config/coros_tokens/.

Confidence note: the request-side shapes above (URLs, login payload, auth
headers) come directly from reading cygnusb/coros-mcp's source and are
high-confidence. The exact *response* JSON field names for activity list
entries are less certain (that project maps them into its own dataclass
without me having seen its raw parsing code), so activity parsing below
tries several plausible key spellings defensively and logs the raw keys of
the first activity on every run — check that log line against reality on
the first real run against an actual Coros account and adjust the
candidate-key lists below if needed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, date as _date, timezone as _tz
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "coros_config.json"
_TOKEN_DIR   = Path(__file__).parent.parent / "config" / "coros_tokens"

_BASE_URLS = {
    "eu":   "https://teameuapi.coros.com",
    "us":   "https://teamapi.coros.com",
    "asia": "https://teamcnapi.coros.com",
    "cn":   "https://teamcnapi.coros.com",
}
_USER_AGENT = "FieldRaven/1.0 (+coros_fetcher.py)"


# ── Config helpers ────────────────────────────────────────────────────────────

def is_configured() -> bool:
    try:
        cfg = _load_config()
        return bool(cfg.get("email") and cfg.get("password"))
    except Exception:
        return False


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Coros config not found at {_CONFIG_PATH}. Create it with: "
            '{"email": "you@example.com", "password": "...", "region": "us", "search_days": 1}'
        )
    return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))


def _token_file_for(email: str) -> Path:
    safe = email.replace("@", "_at_").replace(".", "_")
    return _TOKEN_DIR / f"{safe}.json"


def _base_url(region: str) -> str:
    return _BASE_URLS.get(region, _BASE_URLS["us"])


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ── Authentication ────────────────────────────────────────────────────────────

def _login(email: str, password: str, region: str):
    """POST /account/login — returns (access_token, user_id). Raises on failure."""
    import httpx

    payload = {"account": email, "accountType": 2, "pwd": _md5(password)}
    headers = {"Content-Type": "application/json", "User-Agent": _USER_AGENT}
    with httpx.Client(timeout=30) as client:
        resp = client.post(_base_url(region) + "/account/login", json=payload, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or body
        access_token = data.get("accessToken")
        user_id = data.get("userId")
        if not access_token or not user_id:
            raise RuntimeError(f"Coros login response missing accessToken/userId: {body}")
        return access_token, user_id


def _get_token(cfg: dict) -> dict:
    """Return {"accessToken", "userId", "region"}, from cache or a fresh login."""
    email    = cfg["email"]
    password = cfg.get("password", "")
    region   = cfg.get("region", "us")
    token_file = _token_file_for(email)
    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    if token_file.exists():
        try:
            cached = json.loads(token_file.read_text(encoding="utf-8"))
            if cached.get("accessToken") and cached.get("userId"):
                return cached
        except Exception:
            pass

    print(f"  [coros] Logging in as {email} ({region})…")
    access_token, user_id = _login(email, password, region)
    token = {"accessToken": access_token, "userId": user_id, "region": region}
    token_file.write_text(json.dumps(token), encoding="utf-8")
    print(f"  [coros] Authenticated, token cached")
    return token


def _auth_headers(token: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "User-Agent":   _USER_AGENT,
        "accessToken":  token["accessToken"],
        "yfheader":     json.dumps({"userId": token["userId"]}),
    }


# ── Activity search ───────────────────────────────────────────────────────────

def _get_any(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _fetch_activities(token: dict, start_day: _date, end_day: _date) -> list[dict]:
    import httpx

    params = {
        "startDay":   start_day.strftime("%Y%m%d"),
        "endDay":     end_day.strftime("%Y%m%d"),
        "pageNumber": 1,
        "size":       30,
    }
    with httpx.Client(timeout=30) as client:
        resp = client.get(
            _base_url(token["region"]) + "/activity/query",
            params=params, headers=_auth_headers(token),
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data")
        if isinstance(data, dict):
            activities = data.get("dataList") or data.get("list") or []
        elif isinstance(data, list):
            activities = data
        else:
            activities = []
        return activities


def _parse_activity_start(act: dict) -> Optional[datetime]:
    """Defensive parse — exact raw field name for start time is unconfirmed."""
    raw = _get_any(act, "startTime", "start_time", "startDate", "date")
    if raw is None:
        return None
    # Epoch milliseconds
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000 if raw > 10_000_000_000 else raw, tz=_tz.utc)
    # String forms
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(str(raw), fmt).replace(tzinfo=_tz.utc)
        except ValueError:
            continue
    return None


def _parse_activity_duration_sec(act: dict) -> float:
    return float(_get_any(act, "duration", "durationSeconds", "totalTime", "elapsedDuration", default=0) or 0)


def _find_activity_by_overlap(activities: list[dict], session_start: datetime, session_end: datetime) -> Optional[dict]:
    """Same overlap-matching approach as garmin_fetcher._find_activity_by_overlap."""
    BUFFER = timedelta(minutes=10)
    window_start = session_start - BUFFER
    window_end   = session_end + BUFFER

    best: Optional[dict] = None
    best_overlap = 0.0
    for act in activities:
        act_start = _parse_activity_start(act)
        if act_start is None:
            continue
        act_end = act_start + timedelta(seconds=_parse_activity_duration_sec(act))
        overlap_start = max(window_start, act_start)
        overlap_end   = min(window_end, act_end)
        overlap_sec   = max(0.0, (overlap_end - overlap_start).total_seconds())
        if overlap_sec > best_overlap:
            best_overlap = overlap_sec
            best = act

    if best and best_overlap > 0:
        name = _get_any(best, "name", "activityName", "sportName", default="Untitled")
        print(f"  [coros] Matched '{name}' — {best_overlap/60:.0f} min overlap")
        return best
    print("  [coros] No activity overlaps the session window — skipping")
    return None


# ── Stats builder ─────────────────────────────────────────────────────────────

def _build_activity_stats(activity: dict) -> dict:
    """Shape must match fieldraven-web's ActivityStats interface (src/firebase.ts)
    exactly — every non-optional field there must be present here, even when
    the value is unknown, or ActivitySection.tsx's non-optional-chained
    `s.route.length` access throws at render time for a missing key (not just
    an empty array). route is always [] here: no confirmed Coros endpoint
    returns GPS points (see module docstring) — RouteMap/ElevationChart both
    already gate on `route.length > 1` so an empty route just hides those
    sections rather than breaking anything."""
    start = _parse_activity_start(activity)
    dist_m = float(_get_any(activity, "distance", "distanceMeters", "distance_meters", default=0) or 0)
    dur_sec = int(_parse_activity_duration_sec(activity))
    gain = _get_any(activity, "elevationGain", "elevation_gain", default=0) or 0
    loss = _get_any(activity, "elevationLoss", "elevation_loss", default=0) or 0

    return {
        "activityName":   _get_any(activity, "name", "activityName", "sportName", default="Untitled"),
        "activityType":   _get_any(activity, "sportName", "sport_name", default="Activity"),
        "startDate":      start.isoformat() if start else "",
        "distanceKm":     round(dist_m / 1000, 2),
        "elevationGainM": round(float(gain), 1),
        "elevationLossM": round(float(loss), 1),
        "durationSec":    dur_sec,
        "movingTimeSec":  dur_sec,
        "maxElevationM":  0,
        "minElevationM":  0,
        "route":          [],
        "source":         "coros",
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_and_store(
    job_id: str,
    job_date: _date,
    db,
    session_start: Optional[datetime] = None,
    session_end:   Optional[datetime] = None,
) -> bool:
    """
    Top-level function, same signature as garmin_fetcher.fetch_and_store().
    Finds the best matching Coros activity by session time window and writes
    activityStats (name/distance/elevation/duration only — see module
    docstring re: GPS not yet available) to Firestore gallery/{job_id}.

    Returns True if activity data was stored, False otherwise.
    """
    if not is_configured():
        print("  [coros] coros_config.json not found — skipping auto-fetch")
        return False
    if not (session_start and session_end):
        print("  [coros] Skipping — no session times available")
        return False

    try:
        cfg = _load_config()
        token = _get_token(cfg)

        local_date = session_start.astimezone().date()
        activities = _fetch_activities(token, local_date, local_date)
        if activities:
            print(f"  [coros] First activity keys (debug — verify field names): "
                  f"{sorted(activities[0].keys())}")
        if not activities:
            print(f"  [coros] No activities on {local_date}")
            return False

        activity = _find_activity_by_overlap(activities, session_start, session_end)
        if not activity:
            return False

        stats = _build_activity_stats(activity)
        # Same field the web app's ActivitySection.tsx reads (item.activityStats)
        # and garmin_fetcher.py writes — a prior version of this wrote to
        # "corosActivity", a field nothing on the frontend ever reads.
        update = {"activityStats": stats}

        # If nothing else has set a pin yet, and this response happens to carry
        # a location field, use it — cheap to check, costs nothing if absent.
        try:
            gallery_snap = db.collection("gallery").document(job_id).get()
            if gallery_snap.exists and not gallery_snap.to_dict().get("captureLocation"):
                lat = _get_any(activity, "startLatitude", "lat", "latitude")
                lon = _get_any(activity, "startLongitude", "lon", "lng", "longitude")
                if lat is not None and lon is not None:
                    update["captureLocation"] = {"lat": float(lat), "lon": float(lon)}
                    print(f"  [coros] captureLocation set from activity: {lat}, {lon}")
        except Exception:
            pass

        db.collection("gallery").document(job_id).update(update)
        print(f"  [coros] ✅ Activity stored: {stats['activityName']} "
              f"({stats['distanceKm']} km, ↑{stats['elevationGainM']} m)")
        return True

    except Exception as e:
        print(f"  [coros] Non-fatal error: {e}")
        return False
