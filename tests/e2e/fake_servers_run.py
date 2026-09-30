"""End-to-end run of the patched R:soul against fake slskd and Chaptarr HTTP servers.

Uses the real slskd-api client and R:soul's own Readarr client over real HTTP, with
response shapes taken from the slskd v0.26 and Chaptarr v0.9.965 source.
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
real_sleep = time.sleep
time.sleep = lambda s: real_sleep(0.002)  # speed up every wait inside R:soul

DL = tempfile.mkdtemp(prefix="slskd-dl-")
CFG = tempfile.mkdtemp(prefix="rsoul-cfg-")
LOG = {"chaptarr": [], "slskd": []}

BOOK1_DIR = "@@alice\\Audiobooks\\Brandon Sanderson\\Mistborn - The Final Empire (2006) [Michael Kramer]"
BOOK2_DIR = "@@alice\\Audiobooks\\Brandon Sanderson\\The Way of Kings\\CD1"
DECOY_DIR = "@@bob\\Audiobooks\\Someone Else\\The Final Empire"
CH = {BOOK1_DIR: [(f"{i:02d} - Chapter {i}.mp3", 1000 + i) for i in range(1, 13)],
      BOOK2_DIR: [(f"{i:02d}.mp3", 2000) for i in range(1, 6)],
      DECOY_DIR: [(f"{i:02d}.mp3", 3000) for i in range(1, 4)]}

# ------------------------------------------------------------------ fake slskd
# A finished download that belongs to another tool sharing slskd (e.g. Soularr for music);
# R:soul must leave it alone
FOREIGN = {"id": "soularr-1", "username": "musicpeer", "filename": "@@m\\Music\\Album\\01.flac", "size": 10, "state": "Completed, Succeeded", "bytesTransferred": 10}
transfers = {"soularr-1": dict(FOREIGN)}  # id -> record
CLEARS = {"n": 0}
lock = threading.Lock()

def user_dirs(username):
    dirs = {}
    for t in transfers.values():
        if t["username"] == username:
            d = t["filename"].rsplit("\\", 1)[0]
            dirs.setdefault(d, []).append(t)
    return [{"directory": d, "fileCount": len(fs), "files": fs} for d, fs in dirs.items()]

STALL = {"on": os.environ.get("SCENARIO") == "stall"}

def advance():
    """Each status request moves every transfer one step: Requested -> Queued -> InProgress -> done."""
    order = ["Requested", "Queued, Remotely", "InProgress", "Completed, Succeeded"]
    for t in transfers.values():
        chapter = int(t["filename"].rsplit("\\", 1)[1][:2]) if t["filename"].rsplit("\\", 1)[1][:2].isdigit() else 0
        if STALL["on"] and chapter > 6 and t["state"] == "InProgress":
            continue  # the peer stops sending after chapter 6
        if t["state"] in order[:-1]:
            t["state"] = order[order.index(t["state"]) + 1]
            if t["state"] == "InProgress":
                t["bytesTransferred"] = t["size"] // 2
            if t["state"] == "Completed, Succeeded":
                t["bytesTransferred"] = t["size"]
                leaf = t["filename"].rsplit("\\", 1)[0].split("\\")[-1]  # ${SOURCE_DIRECTORY}
                os.makedirs(os.path.join(DL, leaf), exist_ok=True)
                with open(os.path.join(DL, leaf, t["filename"].rsplit("\\", 1)[1]), "wb") as f:
                    f.write(b"\0" * t["size"])

class Slskd(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def body(self):
        n = int(self.headers.get("Content-Length", 0)); return json.loads(self.rfile.read(n) or b"null")
    def do_GET(self):
        p = unquote(urlparse(self.path).path); LOG["slskd"].append(("GET", p))
        with lock:
            if p == "/api/v0/options": return self.send(200, {})
            m = re.fullmatch(r"/api/v0/searches/([^/]+)", p)
            if m: return self.send(200, {"id": m.group(1), "state": "Completed, Succeeded"})
            m = re.fullmatch(r"/api/v0/searches/([^/]+)/responses", p)
            if m:
                files = [{"filename": f"{d}\\{n}", "size": s} for d in (BOOK1_DIR, BOOK2_DIR, DECOY_DIR) for n, s in CH[d][:5]]
                return self.send(200, [{"username": "alice", "files": [f for f in files if f["filename"].startswith("@@alice")]},
                                       {"username": "bob", "files": [f for f in files if f["filename"].startswith("@@bob")]}])
            if p == "/api/v0/transfers/downloads/":
                users = sorted({t["username"] for t in transfers.values()})
                return self.send(200, [{"username": u, "directories": user_dirs(u)} for u in users])
            m = re.fullmatch(r"/api/v0/transfers/downloads/([^/]+)", p)
            if m:
                advance()
                if os.environ.get("SCENARIO") == "soularr_clears":
                    # Soularr runs against the same slskd and clears every finished transfer
                    # (its own and R:soul's) at the end of each of its runs
                    CLEARS["n"] += 1
                    if CLEARS["n"] % 3 == 0:
                        for k in [k for k, t in transfers.items() if t["state"].startswith("Completed")]:
                            del transfers[k]
                return self.send(200, {"username": m.group(1), "directories": user_dirs(m.group(1))})
            m = re.fullmatch(r"/api/v0/transfers/downloads/([^/]+)/([^/]+)", p)
            if m:
                t = transfers.get(m.group(2)); return self.send(200, t) if t else self.send(404, "not found")
        self.send(404, "unknown")
    def do_POST(self):
        p = unquote(urlparse(self.path).path); data = self.body(); LOG["slskd"].append(("POST", p))
        with lock:
            if p == "/api/v0/searches": return self.send(200, {"id": "s1", "state": "InProgress"})
            m = re.fullmatch(r"/api/v0/users/([^/]+)/directory", p)
            if m:
                d = data["directory"]
                return self.send(200, [{"name": d, "fileCount": len(CH[d]), "files": [{"filename": n, "size": s, "extension": "mp3"} for n, s in CH[d]]}])
            m = re.fullmatch(r"/api/v0/transfers/downloads/([^/]+)", p)
            if m:
                for f in data:
                    tid = f"t{len(transfers)+1}"
                    transfers[tid] = {"id": tid, "username": m.group(1), "filename": f["filename"], "size": f["size"], "state": "Requested", "bytesTransferred": 0}
                return self.send(201, {"enqueued": data, "failed": []})
        self.send(404, "unknown")
    def do_DELETE(self):
        p = unquote(urlparse(self.path).path); LOG["slskd"].append(("DELETE", p))
        with lock:
            if p == "/api/v0/transfers/downloads/all/completed":
                for k in [k for k, t in transfers.items() if t["state"].startswith("Completed")]: del transfers[k]
                return self.send(204)
            if p.startswith("/api/v0/searches/"): return self.send(204)
            m = re.fullmatch(r"/api/v0/transfers/downloads/([^/]+)/([^/]+)", p)
            if m and m.group(2) in transfers:
                if not transfers[m.group(2)]["state"].startswith("Completed"):
                    transfers[m.group(2)]["state"] = "Completed, Cancelled"
                if parse_qs(urlparse(self.path).query).get("remove") == ["True"] or parse_qs(urlparse(self.path).query).get("remove") == ["true"]:
                    del transfers[m.group(2)]
                return self.send(204)
        self.send(204)

# ------------------------------------------------------------------ fake Chaptarr (Readarr-style facade)
BOOKS = {1: {"id": 1, "title": "Mistborn: The Final Empire", "authorId": 10, "mediaType": "audiobook", "monitored": True},
         2: {"id": 2, "title": "The Way of Kings", "authorId": 10, "mediaType": "audiobook", "monitored": True}}
commands = {}

class Chaptarr(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query); LOG["chaptarr"].append(("GET", u.path, q))
        assert u.path.startswith("/audiobook/api/v1/"), u.path
        p = u.path[len("/audiobook/api/v1"):]
        if p == "/wanted/missing":
            recs = [b for b in BOOKS.values() if q.get("mediaType", ["audiobook"])[0] == b["mediaType"]]
            return self.send(200, {"page": 1, "pageSize": 10, "totalRecords": len(recs), "records": recs})
        if p == "/author/10": return self.send(200, {"id": 10, "authorName": "Brandon Sanderson"})
        if p == "/edition": return self.send(200, [])
        m = re.fullmatch(r"/command/(\d+)", p)
        if m:
            c = commands[int(m.group(1))]
            return self.send(200, c)
        self.send(404, {})
    def do_POST(self):
        u = urlparse(self.path); n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n))
        LOG["chaptarr"].append(("POST", u.path, data))
        cid = len(commands) + 1
        path = data.get("path", "")
        audio = [f for f in os.listdir(path) if f.endswith(".mp3")] if os.path.isdir(path) else []
        ok = len(audio) > 0  # Chaptarr: audio extension -> audiobook import
        commands[cid] = {"id": cid, "name": data["name"], "status": "completed", "result": "successful" if ok else "unsuccessful",
                         "message": f"Imported {len(audio)} files" if ok else "Failed to import", "body": {"path": path}}
        self.send(201, dict(commands[cid], status="queued"))

def serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler); threading.Thread(target=s.serve_forever, daemon=True).start(); return s

slskd_srv, chaptarr_srv = serve(Slskd), serve(Chaptarr)

# Configure R:soul entirely through environment variables: no config.ini in the data folder
os.environ.update({
    "RSOUL__READARR__HOST_URL": f"http://127.0.0.1:{chaptarr_srv.server_port}/audiobook",
    "RSOUL__READARR__API_KEY": "chaptarr-key",
    "RSOUL__SLSKD__HOST_URL": f"http://127.0.0.1:{slskd_srv.server_port}",
    "RSOUL__SLSKD__API_KEY": "slskd-key",
    "RSOUL__SLSKD__DOWNLOAD_DIR": DL,
    "RSOUL__SLSKD__READARR_DOWNLOAD_DIR": DL,
    "RSOUL__SEARCH_SETTINGS__MEDIA_MODE": "audiobook",
    "RSOUL__SEARCH_SETTINGS__AUDIOBOOK_MIN_SIZE_MB": "0",
    "RSOUL__SEARCH_SETTINGS__SEARCH_TYPE": "first_page",
    "RSOUL__SEARCH_SETTINGS__IGNORED_USERS": "",
    "RSOUL__DOWNLOAD_SETTINGS__MONITOR_WINDOW": "0",
    "RSOUL__DOWNLOAD_SETTINGS__STALL_TIMEOUT": "1" if os.environ.get("SCENARIO") == "stall" else "1800",
    "RSOUL__GENERAL__BATCH_DELAY": "0",
})

import importlib.util
spec = importlib.util.spec_from_file_location("rsoul_main", os.path.join(REPO, "rsoul.py")); main_mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(main_mod)
from rsoul import orchestrator
_orig = orchestrator.DownloadOrchestrator.__init__
def fast_init(self, *a, **k):
    _orig(self, *a, **k); self.poll_interval = 0.01
orchestrator.DownloadOrchestrator.__init__ = fast_init

sys.argv = ["rsoul.py", "-c", CFG]
t0 = time.time()
main_mod.main()
elapsed = time.time() - t0

if STALL["on"]:
    print("--- run 1 (peer stalls after chapter 6, stall_timeout = 1 s)")
    fd = os.path.join(DL, "failed_downloads")
    print("  import commands:", len([c for c in LOG["chaptarr"] if c[0] == "POST"]))
    print("  cancelled transfers:", sum(t["state"] == "Completed, Cancelled" for t in transfers.values()))
    print("  leftovers moved to failed_downloads:", {d: len(os.listdir(os.path.join(fd, d))) for d in os.listdir(fd)} if os.path.isdir(fd) else "none")
    print("  local download folder still has files:", [d for d in os.listdir(DL) if d not in ("failed_downloads",)])
    STALL["on"] = False
    main_mod.main()
    print("--- run 2 (peer healthy again)")

# ------------------------------------------------------------------ checks
wanted = [c for c in LOG["chaptarr"] if c[0] == "GET" and c[1].endswith("/wanted/missing")]
posts = [c for c in LOG["chaptarr"] if c[0] == "POST"]
browses = [c for c in LOG["slskd"] if c[0] == "POST" and "/directory" in c[1]]
staged = os.path.join(DL, "rsoul_audiobooks", "Brandon Sanderson", "Mistborn The Final Empire")
print(f"run took {elapsed:.1f}s")
print("wanted list filtered to audiobooks:", all(c[2].get("mediaType") == ["audiobook"] for c in wanted))
print("folders browsed:", [c[1] for c in browses])
print("import commands:", [(c[2]["name"], c[2]["path"].replace(DL, "<dl>")) for c in posts])
print("book 1 staged files:", len(os.listdir(staged)) if os.path.isdir(staged) else "MISSING", "of 12")
print("decoy (other author) enqueued:", any(t["username"] == "bob" for t in transfers.values()))
print("disc folder enqueued:", any("CD1" in t["filename"] for t in transfers.values()))
print("state file left behind:", os.path.exists(os.path.join(CFG, "grab_list_state.json")))
print("leftover download folders:", sorted(d for d in os.listdir(DL)))
print("config.ini in data folder:", os.path.exists(os.path.join(CFG, "config.ini")))
print("foreign slskd transfer kept:", "soularr-1" in transfers)
print("cleared all finished transfers:", any(c == ("DELETE", "/api/v0/transfers/downloads/all/completed") for c in LOG["slskd"]))
print("R:soul transfer records left in slskd:", sum(1 for t in transfers.values() if t["username"] != "musicpeer"))
