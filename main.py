#!/usr/bin/env python3
"""
FieldRaven Desktop — Local Web Dashboard
========================================
Starts the FastAPI server and opens the dashboard in the default browser.
Connects to Firebase for auth, job queue management, and machine registration.

Usage:
    python main.py                  # Start server on default port 8081
    python main.py --port 8080      # Custom port
    python main.py --no-browser     # Don't open browser automatically
"""
import sys
import os

# Auto-relaunch under Python 3.13 if running on a different version.
# This ensures cv2, torch, and other ML deps are available regardless of
# which python.exe the user invoked.
if sys.version_info[:2] != (3, 13):
    import subprocess
    print(f"[FieldRaven] Python {sys.version_info.major}.{sys.version_info.minor} detected — relaunching under Python 3.13...")
    result = subprocess.run(["py", "-3.13"] + sys.argv, close_fds=True)
    sys.exit(result.returncode)

import webbrowser
import argparse
import time
import threading

# Add the project root to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn


def open_browser(port: int, delay: float = 2.0):
    """Open the dashboard in the default browser after a short delay."""
    def _open():
        time.sleep(delay)
        url = f"http://localhost:{port}"
        print(f"🌐 Opening dashboard: {url}")
        webbrowser.open(url)
    threading.Thread(target=_open, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description="FieldRaven Desktop Dashboard")
    parser.add_argument("--port", type=int, default=8081, help="Port to serve on (default: 8081)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║         🦅  FieldRaven Desktop                  ║")
    print("║     Local 3D Processing Dashboard               ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    print(f"   Python:    {py_ver}  ✓")
    print(f"   Exec:      {sys.executable}")
    print(f"   Server:    http://{args.host}:{args.port}")
    print(f"   API docs:  http://{args.host}:{args.port}/docs")
    print(f"   Jobs dir:  C:\\FieldRaven\\Jobs")
    print()

    if not args.no_browser:
        open_browser(args.port)

    print("   Press Ctrl+C to stop the server")
    print()

    # Start the FastAPI server
    uvicorn.run(
        "backend.server:app",
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()