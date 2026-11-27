"""Start the local Mira dashboard from the Windows shortcut."""

import json
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from mira.server import main as serve


URL = "http://127.0.0.1:6186"


def dashboard_ready():
    try:
        with urllib.request.urlopen(URL + "/api/session", timeout=1) as response:
            session = json.load(response)
        return isinstance(session, dict) and isinstance(session.get("setup_required"), bool)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def open_when_ready():
    for _ in range(120):
        if dashboard_ready():
            webbrowser.open(URL)
            return
        time.sleep(0.5)
    print(f"Open the dashboard manually at {URL}", flush=True)


def main():
    if dashboard_ready():
        webbrowser.open(URL)
        return
    print(f"Mira dashboard: {URL}   (Ctrl+C to stop)", flush=True)
    threading.Thread(target=open_when_ready, daemon=True).start()
    serve(["--port", "6186"])


if __name__ == "__main__":
    main()
