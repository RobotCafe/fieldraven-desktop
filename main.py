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
import logging
from datetime import datetime
from pathlib import Path


class _Tee:
    """Write to multiple streams simultaneously (e.g. console + log file)."""
    def __init__(self, *streams):
        self._streams = list(streams)

    def add_stream(self, stream):
        if stream not in self._streams:
            self._streams.append(stream)

    def remove_stream(self, stream):
        try:
            self._streams.remove(stream)
        except ValueError:
            pass

    def write(self, data):
        for s in self._streams:
            s.write(data)

    def flush(self):
        for s in self._streams:
            s.flush()

    def fileno(self):
        return self._streams[0].fileno()

    def isatty(self):
        return False


def _setup_file_logging() -> Path:
    log_dir = Path(__file__).parent / "server_logs"
    log_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = log_dir / f"{stamp}.log"

    log_file = open(log_path, "w", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.__stdout__, log_file)
    sys.stderr = _Tee(sys.__stderr__, log_file)

    # Capture uvicorn / FastAPI log records too
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s — %(message)s"))
    logging.getLogger().addHandler(fh)

    return log_path


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
    parser.add_argument("--host", type=str, default="localhost", help="Host to bind to (default: localhost)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    log_path = _setup_file_logging()

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
    print(f"   Log file:  {log_path}")
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