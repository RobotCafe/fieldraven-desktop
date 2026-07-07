"""
Garmin Connect first-time setup and connection test.
Uses garminconnect 0.3.x API (no garth dependency).

Run this ONCE from the FieldRaven_desktop directory:
    python scripts/setup_garmin.py

After this the pipeline auto-fetches Garmin activities with no interaction.
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT      = Path(__file__).parent.parent
_CONFIG    = _ROOT / "config" / "garmin_config.json"
_TEMPLATE  = _ROOT / "config" / "garmin_config.template.json"
_TOKEN_DIR = _ROOT / "config" / "garmin_tokens"


# ── Step 1: ensure config ─────────────────────────────────────────────────────

def ensure_config() -> dict:
    if _CONFIG.exists():
        cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
        if cfg.get("email") and cfg.get("password"):
            print(f"✓  Config: {cfg['email']}")
            return cfg

    print("─" * 60)
    print("Garmin Connect Setup — first-time credentials")
    print("─" * 60)
    print("Stored locally in config/garmin_config.json (gitignored).\n")
    print("NOTE: If you sign in to Garmin Connect with Google or Apple,")
    print("you need a separate Garmin password. Set one at:")
    print("  connect.garmin.com → Profile → Account → Security → Password\n")

    email    = input("Garmin Connect email:    ").strip()
    password = input("Garmin Connect password: ").strip()
    days_str = input("Search days (default 1): ").strip() or "1"

    cfg = {"email": email, "password": password, "search_days": int(days_str)}
    _CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"\n✓  Config saved → {_CONFIG.relative_to(_ROOT)}\n")
    return cfg


# ── Step 2: authenticate ──────────────────────────────────────────────────────

def authenticate(cfg: dict):
    try:
        from garminconnect import Garmin
    except ImportError:
        print("✗  garminconnect not installed. Run:")
        print("     pip install garminconnect")
        sys.exit(1)

    email    = cfg["email"]
    safe     = email.replace("@", "_at_").replace(".", "_")
    tok_file = _TOKEN_DIR / f"{safe}.json"
    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    print("─" * 60)

    # Try existing token first
    if tok_file.exists():
        print("Checking cached token…")
        try:
            client = Garmin(email=email, password=cfg["password"])
            result = client.login(tokenstore=tok_file.read_text(encoding="utf-8"))
            if result[0] is None:
                tok_file.write_text(client.client.dumps(), encoding="utf-8")
                print(f"✓  Token valid — logged in as: {client.full_name or email}")
                return client
            print("  Token valid but MFA flow triggered unexpectedly, re-logging in…")
        except Exception as e:
            print(f"  Cached token invalid ({e}), re-logging in…")
        tok_file.unlink(missing_ok=True)

    # Fresh login
    print("Logging in to Garmin Connect…")
    print("(If 2FA is enabled, you will be prompted for a code.)\n")

    def prompt_mfa() -> str:
        print("\n  Garmin requires 2-factor authentication.")
        print("  Check your email or authenticator app for a code.")
        code = input("  Enter MFA code: ").strip()
        return code

    try:
        client = Garmin(email=email, password=cfg["password"], prompt_mfa=prompt_mfa)
        client.login()
    except Exception as e:
        print(f"\n✗  Login failed: {e}")
        print("\nCommon fixes:")
        print("  • Check email and password in config/garmin_config.json")
        print("  • If you use Google/Apple SSO, create a Garmin-native password:")
        print("    connect.garmin.com → Profile → Account → Security → Password")
        print("  • Try logging in at connect.garmin.com to confirm credentials")
        sys.exit(1)

    tok_file.write_text(client.client.dumps(), encoding="utf-8")
    print(f"\n✓  Logged in as: {client.full_name or email}")
    print(f"✓  Token cached → {tok_file.relative_to(_ROOT)}")
    return client


# ── Step 3: test — list recent activities ─────────────────────────────────────

def test_connection(client, search_days: int = 14):
    print("─" * 60)
    print(f"Fetching activities from the last {search_days} days…\n")

    end   = date.today()
    start = end - timedelta(days=search_days)

    try:
        activities = client.get_activities_by_date(
            start.isoformat(), end.isoformat(), activitytype=None
        )
    except Exception as e:
        print(f"✗  Could not fetch activities: {e}")
        return

    if not activities:
        print(f"  No activities found in the last {search_days} days.")
        print(f"  Increase search_days in {_CONFIG.relative_to(_ROOT)} if needed.")
        return

    print(f"  Found {len(activities)} activities:\n")
    for a in activities[:10]:
        name     = a.get("activityName", "—")
        type_key = (a.get("activityType") or {}).get("typeKey", "?")
        dist_km  = (a.get("distance") or 0) / 1000
        gain_m   = a.get("elevationGain") or 0
        started  = (a.get("startTimeLocal") or "")[:16]
        print(f"  {started}  {name}")
        print(f"  {'':16}  {type_key:<18}  {dist_km:5.1f} km  ↑{gain_m:.0f} m\n")

    if len(activities) > 10:
        print(f"  … and {len(activities) - 10} more\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print()
    cfg    = ensure_config()
    client = authenticate(cfg)
    test_connection(client, search_days=14)

    print("─" * 60)
    print("Setup complete.\n")
    print("How auto-matching works:")
    print("  1. After a pipeline job publishes to the gallery, the pipeline")
    print("     reads startTime/endTime from your FieldRaven mobile job doc.")
    print("  2. It searches Garmin for activities that overlap that time window.")
    print("  3. Downloads the GPX track + lap data and writes activityStats")
    print("     to Firestore — the web gallery detail page shows it immediately.")
    print()
    print("Token management:")
    print(f"  Token stored at: config/garmin_tokens/")
    print("  It auto-refreshes. If it ever expires, re-run this script.")
    print()

if __name__ == "__main__":
    main()
