#!/usr/bin/env python3
"""
Ultra Pomodoro backend (clean rebuild)

Features:
- Serves index.html / manifest.json / sw.js
- Flask-SocketIO companion rooms
- macOS notifications (osascript)
- Hosts-based website blocking (requires sudo)
- Local "cloud" export/import JSON

Run:
  python3 server.py
Hosts blocking (needs root to write /etc/hosts):
  sudo python3 server.py

NOTE:
Hosts blocking only works if OS-level network extensions (AdGuard/LuLu/WireGuard/VPN)
are not intercepting DNS/hosts resolution.
"""

# ---------------------------------------------------------------------------
# Async mode selection: prefer eventlet (for Socket.IO/WebSockets); fall back to threading.
# ---------------------------------------------------------------------------
import os
import platform
ASYNC_MODE = "threading"
try:
    import eventlet  # noqa: WPS433

    eventlet.monkey_patch()
    ASYNC_MODE = "eventlet"
except Exception:
    # Eventlet may be unavailable (or incompatible with runtime); fallback to threading.
    ASYNC_MODE = "threading"

# ---------------------------------------------------------------------------
# Standard imports after monkey_patch
# ---------------------------------------------------------------------------
import json
import time
import socket
import subprocess
import pathlib
import re
import secrets
from typing import List, Tuple, Optional, Dict, Any

from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_socketio import SocketIO, emit, join_room, leave_room

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
BASE_DIR = pathlib.Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "index.html"
EXPORT_PATH = BASE_DIR / "ultra_pomodoro_cloud.json"

HOSTS_FILE = "/etc/hosts"
HOSTS_TAG_START = "# === ULTRA_POMODORO_BLOCK_START ==="
HOSTS_TAG_END = "# === ULTRA_POMODORO_BLOCK_END ==="

# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=None)
# Generous ping settings to survive background-tab throttling and hosted network jitter.
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode=ASYNC_MODE,
    ping_interval=25,
    ping_timeout=60,
)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def is_macos() -> bool:
    return platform.system().lower() == "darwin"

def require_root() -> bool:
    # On macOS/Linux this exists; if not, assume no root.
    return getattr(os, "geteuid", lambda: -1)() == 0

def read_hosts() -> str:
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""

def write_hosts(text: str) -> bool:
    try:
        with open(HOSTS_FILE, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except PermissionError:
        return False
    except Exception:
        return False

def strip_ultra_block_section(hosts_text: str) -> str:
    pattern = re.compile(
        re.escape(HOSTS_TAG_START) + r".*?" + re.escape(HOSTS_TAG_END),
        re.DOTALL
    )
    cleaned = re.sub(pattern, "", hosts_text)
    return cleaned.rstrip() + "\n"

def expand_domains(domains: List[str]) -> List[str]:
    out: List[str] = []
    for d in domains:
        d = (d or "").strip().lower()
        if not d:
            continue
        if d.startswith("http://") or d.startswith("https://"):
            d = re.sub(r"^https?://", "", d)
        d = d.split("/")[0]
        out.append(d)
        if not d.startswith("www."):
            out.append("www." + d)

    seen = set()
    uniq: List[str] = []
    for d in out:
        if d not in seen:
            uniq.append(d)
            seen.add(d)
    return uniq

def flush_dns() -> None:
    if not is_macos():
        return
    cmds = [
        ["dscacheutil", "-flushcache"],
        ["killall", "-HUP", "mDNSResponder"]
    ]
    for c in cmds:
        try:
            subprocess.run(c, check=False,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            pass

def apply_hosts_block(domains: List[str]) -> Tuple[bool, Optional[str]]:
    if not require_root():
        return False, "permission"

    domains = expand_domains(domains)
    hosts = strip_ultra_block_section(read_hosts())

    lines = [HOSTS_TAG_START]
    for d in domains:
        lines.append(f"127.0.0.1 {d}")
        lines.append(f"::1 {d}")
    lines.append(HOSTS_TAG_END)

    new_hosts = hosts + "\n" + "\n".join(lines) + "\n"
    if write_hosts(new_hosts):
        flush_dns()
        return True, None
    return False, "write_failed"

def clear_hosts_block() -> Tuple[bool, Optional[str]]:
    if not require_root():
        return False, "permission"
    hosts = strip_ultra_block_section(read_hosts())
    if write_hosts(hosts):
        flush_dns()
        return True, None
    return False, "write_failed"

def resolve_all(domain: str) -> List[str]:
    ips = set()
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(domain, None):
            ips.add(sockaddr[0])
    except Exception:
        pass
    return sorted(ips)

def mac_notify(title: str, body: str) -> bool:
    if not is_macos():
        return False
    try:
        script = f'display notification "{body}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], check=False)
        return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    if INDEX_PATH.exists():
        return send_from_directory(BASE_DIR, "index.html")
    return "<h1>index.html not found</h1>", 404

@app.route("/manifest.json")
def manifest():
    data = {
        "name": "Ultra Pomodoro",
        "short_name": "Pomodoro",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b0b10",
        "theme_color": "#0b0b10",
        "icons": [
            {
                "src": "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' rx='22' fill='%230b0b10'/><text x='50' y='62' font-size='52' text-anchor='middle'>⏳</text></svg>",
                "sizes": "192x192",
                "type": "image/svg+xml"
            }
        ]
    }
    return jsonify(data)

@app.route("/sw.js")
def sw():
    js = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', e => {});
"""
    resp = make_response(js)
    resp.headers["Content-Type"] = "application/javascript"
    return resp

@app.route("/api/notify", methods=["POST"])
def api_notify():
    data = request.get_json(force=True) or {}
    title = data.get("title", "Ultra Pomodoro")
    body = data.get("body", "Session complete.")
    ok = mac_notify(title, body)
    return jsonify({"ok": ok})

@app.route("/api/export")
def api_export():
    try:
        if EXPORT_PATH.exists():
            with open(EXPORT_PATH, "r", encoding="utf-8") as f:
                return jsonify({"ok": True, "data": json.load(f)})
    except Exception:
        pass
    return jsonify({"ok": True, "data": None})

@app.route("/api/import", methods=["POST"])
def api_import():
    data = request.get_json(force=True) or {}
    try:
        with open(EXPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/block/apply", methods=["POST"])
def api_block_apply():
    data = request.get_json(force=True) or {}
    domains = data.get("domains", []) or []
    ok, err = apply_hosts_block(domains)
    if not ok:
        return jsonify({"ok": False, "error": err}), 403 if err == "permission" else 500
    return jsonify({"ok": True, "domains": expand_domains(domains)})

@app.route("/api/block/clear", methods=["POST"])
def api_block_clear():
    ok, err = clear_hosts_block()
    if not ok:
        return jsonify({"ok": False, "error": err}), 403 if err == "permission" else 500
    return jsonify({"ok": True})

@app.route("/api/block/flush", methods=["POST"])
def api_block_flush():
    flush_dns()
    return jsonify({"ok": True})

@app.route("/api/block/test", methods=["POST"])
def api_block_test():
    data = request.get_json(force=True) or {}
    domains = expand_domains(data.get("domains", []) or [])
    resolutions = {d: resolve_all(d) for d in domains}
    return jsonify({"ok": True, "resolutions": resolutions})

# ---------------------------------------------------------------------------
# Socket Rooms
# ---------------------------------------------------------------------------
def members_in(room_id: str) -> int:
    rooms = socketio.server.manager.rooms.get("/", {})
    return len(rooms.get(room_id, set()))

def new_room_id() -> str:
    return "room-" + secrets.token_hex(3)

@socketio.on("room:join")
def on_room_join(data):
    room_id = (data or {}).get("roomId", "").strip()
    if not room_id:
        return {"ok": False, "error": "missing roomId"}
    join_room(room_id)
    emit("room:joined",
         {"roomId": room_id, "members": members_in(room_id)},
         room=request.sid)
    emit("room:members",
         {"roomId": room_id, "members": members_in(room_id)},
         room=room_id)
    return {"ok": True, "roomId": room_id, "members": members_in(room_id)}

@socketio.on("room:create")
def on_room_create(data):
    room_id = (data or {}).get("roomId", "").strip() or new_room_id()
    join_room(room_id)
    emit("room:created",
         {"roomId": room_id, "members": members_in(room_id)},
         room=request.sid)
    emit("room:members",
         {"roomId": room_id, "members": members_in(room_id)},
         room=room_id)
    return {"ok": True, "roomId": room_id, "members": members_in(room_id)}

@socketio.on("room:leave")
def on_room_leave(data):
    room_id = (data or {}).get("roomId", "").strip()
    if not room_id:
        return {"ok": False, "error": "missing roomId"}
    leave_room(room_id)
    emit("room:members",
         {"roomId": room_id, "members": members_in(room_id)},
         room=room_id)
    emit("room:left",
         {"roomId": room_id, "members": members_in(room_id)},
         room=request.sid)
    return {"ok": True, "roomId": room_id, "members": members_in(room_id)}

@socketio.on("timer:sync")
def on_timer_sync(data):
    room_id = (data or {}).get("roomId", "").strip()
    if not room_id:
        return
    emit("timer:state", data, room=room_id, include_self=False)

@socketio.on("timer:penalty")
def on_timer_penalty(data):
    room_id = (data or {}).get("roomId", "").strip()
    if not room_id:
        return
    emit("timer:penalty", data, room=room_id, include_self=False)

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"Ultra Pomodoro running on http://localhost:{port}")
    if require_root():
        print("[BLOCK] Hosts blocking ENABLED (running as root).")
    else:
        print("[BLOCK] Hosts blocking DISABLED (run with sudo to enable).")

    run_kwargs = dict(host="0.0.0.0", port=port, debug=False)
    if ASYNC_MODE == "threading":
        # Werkzeug is only acceptable for threading fallback / non-prod.
        run_kwargs["allow_unsafe_werkzeug"] = True
    socketio.run(app, **run_kwargs)
