"""
VidBee-engine downloader microservice for allindownload.com
Based on the yt-dlp extraction approach used by VidBee (github.com/nexmoe/VidBee).
Exposes:
  GET  /health          -> {"ok": true, "ytdlp": "<version>"}
  POST /info  {"url"}   -> {"title","duration","thumbnail","formats":[...]}
  GET  /fetch?token=..  -> streams media bytes (validates HMAC token)
Run:  python app.py   (or via docker-compose)
Env:  PORT (default 3100), SECRET (shared with ald-proxy.php), YTDLP_COOKIES (path to cookies.txt, optional)
"""
import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import time
from functools import lru_cache

from flask import Flask, jsonify, request, Response, stream_with_context

PORT = int(os.environ.get("PORT", "3100"))
SECRET = os.environ.get("SECRET", "change-me-in-production")
COOKIES = os.environ.get("YTDLP_COOKIES", "")
CACHE_TTL = 600  # seconds for /info cache

app = Flask(__name__)
_info_cache = {}


def sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
    return f"{raw}.{sign(raw)}"


def read_token(token: str):
    try:
        raw, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, sign(raw)):
            return None
        return json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
    except Exception:
        return None


def ytdlp_base_args(url: str = ""):
    args = ["yt-dlp", "--no-playlist", "--no-warnings",
            "--impersonate", "chrome",
            # Multiple player clients: yt-dlp falls back if one hits YouTube's
            # "Sign in to confirm you're not a bot" datacenter-IP block.
            # No user cookies needed.
            "--extractor-args", "youtube:player_client=android,ios,web"]
    # YouTube cookies ONLY for YouTube URLs — sending them to TikTok/FB/IG
    # breaks those extractors.
    if url and ("youtube.com" in url or "youtu.be" in url):
        if COOKIES and os.path.exists(COOKIES):
            args += ["--cookies", COOKIES]
    return args


def run_ytdlp_info(url: str, timeout=45):
    """Return parsed yt-dlp JSON for url, or raise."""
    args = ytdlp_base_args(url) + ["-J", "--skip-download", url]
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError((p.stderr or "yt-dlp failed")[-500:])
    return json.loads(p.stdout)


def pick_formats(info):
    """Pick web-friendly formats: progressive mp4s + best audio."""
    out = []
    for f in info.get("formats") or []:
        url = f.get("url")
        if not url or not url.startswith("http"):
            continue
        vcodec = (f.get("vcodec") or "none")
        acodec = (f.get("acodec") or "none")
        ext = (f.get("ext") or "").lower()
        height = f.get("height")
        entry = {
            "format_id": f.get("format_id"),
            "ext": ext,
            "vcodec": vcodec,
            "acodec": acodec,
            "height": height,
            "width": f.get("width"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "tbr": f.get("tbr"),
            "protocol": f.get("protocol"),
            "url": url,
        }
        out.append(entry)
    # video+audio progressive mp4 first, then others with both streams
    def rank(e):
        has_av = (e["vcodec"] != "none") and (e["acodec"] != "none")
        return (0 if has_av and e["ext"] == "mp4" else
                1 if has_av else 2, -(e["height"] or 0))
    return sorted(out, key=rank)


@app.get("/health")
def health():
    try:
        v = subprocess.run(["yt-dlp", "--version"], capture_output=True,
                           text=True, timeout=15).stdout.strip()
    except Exception:
        v = "unknown"
    return jsonify({"ok": True, "ytdlp": v, "time": int(time.time())})


@app.post("/info")
def info():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"status": "error", "text": "Invalid URL"}), 400
    now = time.time()
    hit = _info_cache.get(url)
    if hit and now - hit[0] < CACHE_TTL:
        return jsonify(hit[1])
    try:
        raw = run_ytdlp_info(url)
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error",
                        "text": "Usluga je trenutno preopterećena. Pokušaj ponovno za nekoliko minuta."}), 504
    except Exception as e:
        return jsonify({"status": "error",
                        "text": "Nije moguće dohvatiti video. Provjeri je li link javan."}), 404
    if raw.get("_type") == "playlist":
        return jsonify({"status": "error",
                        "text": "Playlist linkovi nisu podržani. Zalijepi link jednog videa."}), 400
    payload = {
        "status": "ok",
        "title": raw.get("title") or "Video",
        "duration": raw.get("duration"),
        "thumbnail": raw.get("thumbnail"),
        "uploader": raw.get("uploader"),
        "formats": pick_formats(raw),
    }
    _info_cache[url] = (now, payload)
    return jsonify(payload)


@app.get("/fetch")
def fetch():
    """Download+convert to a temp file, then stream it as a real MP4/MP3.

    (Piping yt-dlp's merged output to stdout yields MPEG-TS, which phones
    save as .mp4 but cannot play. A temp file lets ffmpeg write a proper
    MP4 with the moov atom at the front, plus a real Content-Length.)
    """
    data = read_token(request.args.get("token", ""))
    if not data or not data.get("url"):
        return jsonify({"status": "error", "text": "Neispravan zahtjev."}), 403
    url = data["url"]
    fmt = data.get("format_id") or "best"
    conv = data.get("conv")  # e.g. "mp3"
    tmpdir = tempfile.mkdtemp(prefix="dl")
    try:
        out_tmpl = os.path.join(tmpdir, "out.%(ext)s")
        if conv == "mp3":
            args = ytdlp_base_args(url) + ["-f", fmt, "-x", "--audio-format", "mp3",
                                           "--audio-quality", "0",
                                           "-o", out_tmpl, url]
            ctype, fname = "audio/mpeg", "audio.mp3"
        else:
            args = ytdlp_base_args(url) + ["-f", fmt, "--merge-output-format", "mp4",
                                           "--postprocessor-args",
                                           "ffmpeg:-movflags +faststart",
                                           "-o", out_tmpl, url]
            ctype, fname = "video/mp4", "video.mp4"
        p = subprocess.run(args, capture_output=True, text=True, timeout=900)
        outs = [f for f in os.listdir(tmpdir)
                if f.startswith("out.") and not f.endswith((".part", ".ytdl"))]
        if p.returncode != 0 or not outs:
            raise RuntimeError((p.stderr or "yt-dlp failed")[-300:])
        fpath = os.path.join(tmpdir, outs[0])
        fsize = os.path.getsize(fpath)

        def gen():
            try:
                with open(fpath, "rb") as fh:
                    while True:
                        chunk = fh.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        headers = {"Content-Type": ctype,
                   "Content-Disposition": f'attachment; filename="{fname}"',
                   "Content-Length": str(fsize),
                   "Accept-Ranges": "bytes"}
        return Response(stream_with_context(gen()), headers=headers)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"status": "error", "text": "Nije moguće dohvatiti video."}), 500


@app.post("/token")
def token():
    """Mint a signed download token (called by ald-proxy.php)."""
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("url"):
        return jsonify({"status": "error"}), 400
    return jsonify({"token": make_token({
        "url": data["url"],
        "format_id": data.get("format_id") or "best",
        "conv": data.get("conv"),
        "exp": int(time.time()) + 3600,
    })})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
