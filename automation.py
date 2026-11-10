#!/usr/bin/env python3
"""
Shorts automation: pick N clips -> cut 9:16 -> AI title/description/hashtags
-> upload to YouTube (immediately or scheduled).

Setup:
    pip install flask yt-dlp google-api-python-client google-auth-oauthlib anthropic "google-genai>=2.30.1"
    ffmpeg must be installed and on PATH (with libass for burned-in captions)
    1. Google Cloud Console: create project, enable "YouTube Data API v3" and
       "Google Drive API", create OAuth client ID (type: Desktop app),
       download as client_secret.json into this folder.
    2. python automation.py --login      (one-time browser sign-in, saves token.json)
    3. set ANTHROPIC_API_KEY for transcript-based clip selection & AI metadata
       (optional; falls back to evenly spaced clips and templates)
    4. set GEMINI_API_KEY for prompt-based 9:16 video generation via Gemini Omni 1.2 Flash
       (optional GEMINI_VIDEO_MODEL env var, defaults to gemini-omni-1.2-flash)

Notes:
    - Transcripts are fetched via yt-dlp captions (falls back cleanly if none exist).
    - Each upload costs 1600 API quota units; default quota is 10,000/day (~6 uploads).
    - New/unaudited API projects may have uploads locked to private by YouTube.
    - Only automate videos you own or have rights to reuse.
"""

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

import shorts_clipper as sc

YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
SCOPES = [YT_SCOPE, DRIVE_SCOPE]
OUT = sc.temp_workdir()
SETTINGS_FILE = Path("settings.json")
bp = Blueprint("auto", __name__)
jobs: dict[str, dict] = {}


def _hydrate_env_files() -> None:
    """Restore token/client_secret/settings JSON files from env vars on ephemeral cloud hosts."""
    for env_name, target in (
        ("GOOGLE_TOKEN_JSON", Path("token.json")),
        ("GOOGLE_CLIENT_SECRET_JSON", Path("client_secret.json")),
        ("SETTINGS_JSON", SETTINGS_FILE),
    ):
        raw = os.getenv(env_name, "").strip()
        if raw and not target.exists():
            target.write_text(raw, encoding="utf-8")


_hydrate_env_files()


def _load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_settings(data: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _get_key(field: str, env_var: str) -> str:
    return (_load_settings().get(field) or os.getenv(env_var) or "").strip()


def _mask_key(val: str) -> str:
    return f"...{val[-4:]}" if len(val) >= 4 else ("set" if val else "")


def register(app):
    app.register_blueprint(bp)


# ---------- Google (YouTube & Drive) ----------
def _token_scopes() -> set[str]:
    if not Path("token.json").exists():
        return set()
    try:
        data = json.loads(Path("token.json").read_text(encoding="utf-8"))
        raw = data.get("scopes") or data.get("scope") or []
        return set(raw.split() if isinstance(raw, str) else raw)
    except Exception:  # noqa: BLE001
        return set()


def _google_creds(required_scopes: list[str] | None = None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    req = required_scopes or SCOPES
    creds = None
    if Path("token.json").exists():
        creds = Credentials.from_authorized_user_file("token.json", req)
        if creds and not creds.has_scopes(req):
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path("client_secret.json").exists():
                raise FileNotFoundError("client_secret.json not found in this folder.")
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
            creds = flow.run_local_server(port=0)
        Path("token.json").write_text(creds.to_json())
    return creds


def youtube_service():
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=_google_creds([YT_SCOPE]))


def drive_service():
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_google_creds([DRIVE_SCOPE]))


def parse_drive_folder(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", v) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", v)
    return m.group(1) if m else v.split("?")[0].strip("/")


def upload_drive(drive, path: Path, name: str, folder_id: str = "") -> str:
    from googleapiclient.http import MediaFileUpload

    meta: dict = {"name": name}
    if folder_id:
        meta["parents"] = [folder_id]
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True)
    created = drive.files().create(
        body=meta,
        media_body=media,
        fields="id,webViewLink",
        supportsAllDrives=True,
    ).execute()
    return created.get("webViewLink") or f"https://drive.google.com/file/d/{created['id']}/view"


def upload(yt, path: Path, title: str, desc: str, tags: list, privacy: str, publish_at: str | None) -> str:
    from googleapiclient.http import MediaFileUpload

    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}
    if publish_at:
        status.update(privacyStatus="private", publishAt=publish_at)
    body = {
        "snippet": {"title": title[:100], "description": desc[:4900], "tags": tags, "categoryId": "22"},
        "status": status,
    }
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True, chunksize=-1)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


# ---------- Transcript ----------
def _pick_sub_track(info: dict) -> tuple[str, str] | None:
    vid_lang = info.get("language") or ""
    for source in (info.get("subtitles") or {}, info.get("automatic_captions") or {}):
        if not source:
            continue
        langs = [k for k in source if k != "live_chat"]
        if not langs:
            continue
        pref = ["en", "en-orig"] + [k for k in langs if k.startswith(("en-", "en_"))]
        if vid_lang and vid_lang in langs:
            pref.append(vid_lang)
        pref.extend(langs)
        lang = next((k for k in pref if k in source and source[k]), None)
        if not lang:
            continue
        formats = source[lang]
        for ext in ("json3", "vtt", "srt"):
            match = next((f for f in formats if f.get("ext") == ext and f.get("url")), None)
            if match:
                return ext, match["url"]
    return None


def _clamp_overlaps(cues: list[dict]) -> list[dict]:
    for i in range(len(cues) - 1):
        if cues[i]["end"] > cues[i + 1]["start"] > cues[i]["start"]:
            cues[i]["end"] = cues[i + 1]["start"]
    return [c for c in cues if c["end"] > c["start"] and c["text"]]


def _parse_json3(raw: str) -> list[dict]:
    data = json.loads(raw)
    cues: list[dict] = []
    for ev in data.get("events", []):
        t0 = ev.get("tStartMs")
        segs = ev.get("segs")
        if t0 is None or not segs:
            continue
        text = " ".join("".join(s.get("utf8", "") for s in segs).split())
        if not text:
            continue
        start = t0 / 1000.0
        dur = ev.get("dDurationMs") or 0
        end = (t0 + dur) / 1000.0 if dur > 0 else start + 2.5
        if cues and cues[-1]["text"] == text and start - cues[-1]["end"] < 0.5:
            cues[-1]["end"] = max(cues[-1]["end"], end)
        else:
            cues.append({"start": start, "end": end, "text": text})
    return _clamp_overlaps(cues)


_TS_RE = re.compile(
    r"((?:\d+:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*((?:\d+:)?\d{2}:\d{2}[.,]\d{3})"
)


def _parse_vtt_srt(raw: str) -> list[dict]:
    cues: list[dict] = []
    prev_lines: list[str] = []
    for block in re.split(r"\r?\n\s*\r?\n", raw.strip()):
        lines = block.splitlines()
        ts_idx = next((i for i, ln in enumerate(lines) if _TS_RE.search(ln)), None)
        if ts_idx is None:
            continue
        m = _TS_RE.search(lines[ts_idx])
        start = sc.parse_time(m.group(1).replace(",", "."))
        end = sc.parse_time(m.group(2).replace(",", "."))
        body = []
        for ln in lines[ts_idx + 1:]:
            cleaned = html.unescape(re.sub(r"<[^>]+>", "", ln)).strip()
            if cleaned:
                body.append(cleaned)
        if not body:
            continue
        # YouTube auto-VTT repeats the previous line as the top line of the next cue
        if prev_lines and body[0] == prev_lines[-1]:
            fresh = body[1:]
        else:
            fresh = body
        prev_lines = body
        text = " ".join(fresh).strip()
        if text and end > start:
            cues.append({"start": start, "end": end, "text": text})
    return _clamp_overlaps(cues)


def fetch_transcript(url: str, work: Path | None = None, info: dict | None = None) -> list[dict]:
    """Fetch timestamped captions via yt-dlp in memory; return [] if none exist or on failure."""
    try:
        import yt_dlp

        with yt_dlp.YoutubeDL(sc.ydl_opts(skip_download=True)) as ydl:
            if info is None:
                info = ydl.extract_info(url, download=False)
            track = _pick_sub_track(info or {})
            if not track:
                return []
            ext, sub_url = track
            raw = ydl.urlopen(sub_url).read().decode("utf-8", errors="replace")
        return _parse_json3(raw) if ext == "json3" else _parse_vtt_srt(raw)
    except Exception:  # noqa: BLE001
        return []


def clip_transcript_text(transcript: list[dict], start: float, end: float) -> str:
    return " ".join(
        c["text"] for c in transcript if c["end"] > start and c["start"] < end
    ).strip()


def _extract_json(text: str):
    start_obj = text.find("{")
    start_arr = text.find("[")
    if start_arr != -1 and (start_obj == -1 or start_arr < start_obj):
        return json.loads(text[start_arr: text.rindex("]") + 1])
    return json.loads(text[start_obj: text.rindex("}") + 1])


# ---------- Metadata ----------
def make_metadata(
    info: dict,
    url: str,
    niche: str,
    i: int,
    n: int,
    transcript: str = "",
) -> dict:
    src_title = info.get("title", "Video")
    data = None
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        try:
            import anthropic

            clip_ctx = f"Clip transcript:\n{transcript[:3000]}\n" if transcript else ""
            origin = (
                f"cut from a longer video.\nSource title: {src_title}\n"
                f"Source description: {(info.get('description') or '')[:600]}\n"
                if url
                else f"generated from this prompt: {src_title}\n"
            )
            prompt = (
                "Write metadata for YouTube Short "
                f"{i} of {n} {origin}"
                f"Channel niche/keywords: {niche or 'general'}\n"
                f"{clip_ctx}"
                "Base the title, description, and hashtags on the content of this Short.\n"
                'Reply with JSON only: {"title": "<=70 chars, hooky, no clickbait lies", '
                '"description": "2-3 short engaging sentences with a call to subscribe", '
                '"hashtags": ["#tag", ... 5 to 7 relevant hashtags]}'
            )
            msg = anthropic.Anthropic(api_key=api_key).messages.create(
                model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text
            data = _extract_json(text)
        except Exception:  # noqa: BLE001
            data = None
    if not data:
        words = [w for w in (niche or "").replace(",", " ").split() if w]
        excerpt = f'"{transcript[:140].strip()}..." ' if transcript else ""
        base_desc = (
            f'{excerpt}Part {i} of {n} from "{src_title}". Subscribe for more!'
            if url
            else f"{excerpt}Subscribe for more!"
        )
        data = {
            "title": f"{src_title[:60]} (Part {i})" if n > 1 else src_title[:70],
            "description": base_desc,
            "hashtags": ["#" + w.strip("#") for w in words[:5]],
        }
    tags = []
    for h in data.get("hashtags", []) + ["#Shorts"]:
        h = "#" + h.strip().lstrip("#").replace(" ", "")
        if len(h) > 1 and h.lower() not in [t.lower() for t in tags]:
            tags.append(h)
    title = data["title"].strip()
    if "#shorts" not in title.lower() and len(title) <= 90:
        title += " #Shorts"
    link_line = f"\n\nWatch the full video: {url}" if url else ""
    desc = f"{data['description'].strip()}{link_line}\n\n{' '.join(tags[:8])}"
    return {"title": title, "description": desc, "tags": [t.lstrip("#") for t in tags[:8]]}


# ---------- Pipeline ----------
def pick_segments(duration: float, count: int, length: int, start_offset: float = 0.0):
    avail = max(0.0, duration - start_offset)
    length = max(5, min(length, int(avail)))
    count = max(1, min(count, max(1, int(avail // length))))
    if count == 1:
        s = round(start_offset)
        return [(s, s + length)]
    step = (avail - length) / (count - 1)
    return [(round(start_offset + i * step), round(start_offset + i * step) + length) for i in range(count)]


def pick_transcript_segments(
    transcript: list[dict], duration: float, count: int, length: int, start_offset: float = 0.0
) -> list[tuple[float, float]]:
    """Ask Anthropic API to pick N engaging, non-overlapping clips; fall back to pick_segments."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    scoped = [c for c in transcript if c["end"] > start_offset]
    if not scoped or not api_key or duration - start_offset < 5:
        return pick_segments(duration, count, length, start_offset)
    try:
        import anthropic

        lines = []
        total_chars = 0
        for c in scoped:
            line = f"[{int(max(start_offset, c['start']))}s] {c['text']}"
            if total_chars + len(line) > 100_000:
                break
            lines.append(line)
            total_chars += len(line) + 1
        min_len = max(5, length - 15)
        max_len = min(sc.MAX_SHORT_SECONDS, length + 15)
        prompt = (
            f"Pick the {count} most engaging, self-contained moments from this timestamped video transcript "
            f"(between {int(start_offset)}s and {int(duration)}s) for YouTube Shorts.\n"
            f"Each clip should be close to {length}s long (roughly {min_len}s to {max_len}s), "
            f"start and end cleanly on complete thoughts, and MUST NOT overlap any other clip.\n"
            'Reply with JSON only: {"clips": [{"start": 12.0, "end": 57.0}, ...]}\n\n'
            "Transcript:\n" + "\n".join(lines)
        )
        msg = anthropic.Anthropic(api_key=api_key).messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        raw = _extract_json(text)
        items = raw.get("clips", raw) if isinstance(raw, dict) else raw
        parsed: list[tuple[float, float]] = []
        for item in items:
            s = max(start_offset, sc.parse_time(str(item["start"])))
            e = sc.parse_time(str(item["end"]))
            if duration > 0:
                e = min(duration, e)
            if 5.0 <= e - s <= sc.MAX_SHORT_SECONDS:
                parsed.append((round(s, 2), round(e, 2)))
        parsed.sort(key=lambda x: x[0])
        chosen: list[tuple[float, float]] = []
        for s, e in parsed:
            if not chosen or s >= chosen[-1][1]:
                chosen.append((s, e))
            if len(chosen) >= count:
                break
        if chosen:
            return chosen
    except Exception:  # noqa: BLE001
        pass
    return pick_segments(duration, count, length, start_offset)


def generate_omni_clip(prompt: str, duration: int, out_path: Path) -> Path:
    """Generate a 9:16 1080p MP4 clip (3-15s) from a text prompt using Gemini Omni 1.2 Flash."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set in the environment.")
    try:
        from google import genai
        from google.genai import errors, types
    except ImportError as err:
        raise RuntimeError("google-genai>=2.30.1 not found. Run: pip install -U google-genai") from err

    dur = max(3, min(15, int(duration)))
    model = os.getenv("GEMINI_VIDEO_MODEL", "gemini-omni-1.2-flash")
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=600_000))
    try:
        interaction = client.interactions.create(
            model=model,
            input=[{"type": "text", "text": prompt.strip()}],
            response_format={
                "type": "video",
                "delivery": "uri",
                "aspect_ratio": "9:16",
                "duration": f"{dur}s",
                "resolution": "1080p",
            },
        )
    except errors.APIError as err:
        msg = f"{getattr(err, 'code', '')} {getattr(err, 'status', '')}: {getattr(err, 'message', err)}"
        raise RuntimeError(msg.replace(api_key, "[REDACTED]")) from err

    out_vid = getattr(interaction, "output_video", None)
    if not out_vid:
        raise RuntimeError("Gemini Omni returned no video (prompt may have been filtered).")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if getattr(out_vid, "uri", None):
        video_bytes = client.files.download(file=out_vid.uri)
        out_path.write_bytes(video_bytes)
    elif getattr(out_vid, "data", None):
        out_path.write_bytes(base64.b64decode(out_vid.data))
    else:
        raise RuntimeError("Gemini Omni returned an empty video payload.")
    return out_path


def _upload_or_mark(yt, drive, out_path: Path, md: dict, p: dict, start, i: int, item: dict) -> None:
    statuses: list[str] = []
    if yt:
        when = None
        if start:
            when = (start + timedelta(hours=p["interval"] * (i - 1))).astimezone(timezone.utc)
            when = when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            item["when"] = when
        item["status"] = "Uploading to YouTube..."
        vid = upload(yt, out_path, md["title"], md["description"], md["tags"], p["privacy"], when)
        item["link"] = f"https://youtube.com/shorts/{vid}"
        statuses.append("Scheduled" if when else "Published")

    if drive:
        item["status"] = "Uploading to Google Drive..."
        clean_title = re.sub(r'[\\/:*?"<>|]+', "", md.get("title", "")).strip()[:60]
        drive_name = f"{clean_title or out_path.stem}.mp4"
        item["drive_link"] = upload_drive(drive, out_path, drive_name, p.get("drive_folder", ""))
        statuses.append("Drive")

    item["status"] = " + ".join(statuses) if statuses else "Ready (not uploaded)"


def run(jid: str, p: dict) -> None:
    job = jobs[jid]
    try:
        sc.cleanup_temp()
        yt = youtube_service() if p["upload"] else None
        drive = drive_service() if p["drive_upload"] else None
        start = None
        if p["publish"] == "schedule":
            start = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))

        if p["source"] == "prompt":
            info = {"title": p["prompt"][:80], "description": p["prompt"]}
            for i in range(1, p["count"] + 1):
                item = {"n": i, "title": "", "status": "Generating video...", "link": "", "drive_link": "", "file": "", "name": ""}
                job["items"].append(item)
                job["status"] = f"Generating Short {i} of {p['count']}..."
                try:
                    name = f"omni_{jid}_{i:02d}.mp4"
                    clip_prompt = p["prompt"] if p["count"] == 1 else f"{p['prompt']} (Variation {i})"
                    generate_omni_clip(clip_prompt, p["length"], OUT / name)
                    item.update(file=f"/clips/{name}", name=name, status="Writing metadata...")
                    md = make_metadata(info, "", p["niche"], i, p["count"], transcript=p["prompt"])
                    item["title"] = md["title"]
                    _upload_or_mark(yt, drive, OUT / name, md, p, start, i, item)
                except Exception as err:  # noqa: BLE001
                    item["status"] = "Failed: " + str(err)[-160:]
                job["progress"] = round(i / p["count"] * 100)
            job["status"] = "Done"
            job["done"] = True
            return

        job["status"] = "Connecting to remote stream..."
        streams, duration, _, info = sc.get_streams(p["url"])
        offset = float(p.get("from_sec", 0.0))
        if duration > 0 and duration - offset < 5:
            raise ValueError(f"Start time ({int(offset)}s) leaves less than 5s in {int(duration)}s video.")
        job["status"] = "Fetching transcript..."
        transcript = fetch_transcript(p["url"], info=info)
        job["status"] = "Selecting clips..."
        segs = pick_transcript_segments(transcript, duration, p["count"], p["length"], offset)
        total = len(segs)
        job["items"] = [
            {"n": i, "title": "", "status": "Queued...", "link": "", "drive_link": "", "file": "", "name": ""}
            for i in range(1, total + 1)
        ]
        job["status"] = f"Clipping {total} Short(s) remotely..."
        ready: dict[int, tuple[Path, dict]] = {}
        done_count = 0
        lock = threading.Lock()
        has_uploads = bool(yt or drive)

        def _process_clip(i: int, s: float, e: float) -> None:
            nonlocal done_count
            item = job["items"][i - 1]
            name = f"{info['id']}_{jid}_{i:02d}.mp4"
            out_path = OUT / name
            try:
                item["status"] = "Clipping remotely..."
                with tempfile.TemporaryDirectory() as td:
                    subs_path = (
                        sc.write_ass(transcript, s, e, Path(td) / f"auto_{i:02d}.ass")
                        if p.get("captions") and transcript
                        else None
                    )
                    sc.cut_clip(streams, s, e, out_path, p["mode"], subs_path=subs_path)
                item.update(file=f"/clips/{name}", name=name, status="Writing metadata...")
                clip_text = clip_transcript_text(transcript, s, e)
                md = make_metadata(info, p["url"], p["niche"], i, total, transcript=clip_text)
                item["title"] = md["title"]
                if has_uploads:
                    item["status"] = "Ready to upload"
                    with lock:
                        ready[i] = (out_path, md)
                else:
                    item["status"] = "Ready"
            except subprocess.CalledProcessError as err:
                msg = err.stderr.decode(errors="ignore").strip().splitlines()
                item["status"] = "Failed: " + (msg[-1][-160:] if msg else str(err)[-160:])
            except Exception as err:  # noqa: BLE001
                item["status"] = "Failed: " + str(err)[-160:]
            with lock:
                done_count += 1
                job["progress"] = round(done_count / total * (70 if has_uploads else 100))

        workers = min(max(1, int(os.getenv("MAX_WORKERS", "3"))), total)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_process_clip, i, s, e) for i, (s, e) in enumerate(segs, 1)]
            for fut in as_completed(futs):
                fut.result()

        if has_uploads:
            for i in range(1, total + 1):
                if i not in ready:
                    continue
                out_path, md = ready[i]
                item = job["items"][i - 1]
                job["status"] = f"Uploading Short {i} of {total}..."
                try:
                    _upload_or_mark(yt, drive, out_path, md, p, start, i, item)
                except Exception as err:  # noqa: BLE001
                    item["status"] = "Failed: " + str(err)[-160:]
                job["progress"] = 70 + round(i / total * 30)
        job["progress"] = 100
        job["status"] = "Done"
    except Exception as err:  # noqa: BLE001
        job["error"] = str(err)[-300:]
    job["done"] = True


@bp.get("/api/auto/auth")
def auth():
    scopes = _token_scopes()
    ready = Path("token.json").exists()
    return jsonify(ready=ready, youtube_ready=ready and (not scopes or YT_SCOPE in scopes), drive_ready=ready and DRIVE_SCOPE in scopes)


@bp.route("/api/auto/settings", methods=["GET", "POST"])
def api_settings():
    cur = _load_settings()
    if request.method == "POST":
        d = request.json or {}
        if "drive_folder" in d:
            cur["drive_folder"] = (d.get("drive_folder") or "").strip()
        val = (d.get("drive_api_key") or "").strip()
        if val:
            cur["drive_api_key"] = val
        elif d.get("clear_drive_api_key"):
            cur.pop("drive_api_key", None)
        _save_settings(cur)
    dk = _get_key("drive_api_key", "GOOGLE_DRIVE_API_KEY")
    return jsonify(
        drive_folder=cur.get("drive_folder", ""),
        has_drive_key=bool(dk),
        drive_key_mask=_mask_key(dk),
        has_anthropic=bool(os.getenv("ANTHROPIC_API_KEY")),
        has_gemini=bool(os.getenv("GEMINI_API_KEY")),
    )


@bp.post("/api/auto/test")
def test_apis():
    from googleapiclient.discovery import build

    d = request.json or {}
    dk = (d.get("drive_api_key") or "").strip() or _get_key("drive_api_key", "GOOGLE_DRIVE_API_KEY")
    folder_raw = (d.get("drive_folder") or "").strip() or _load_settings().get("drive_folder", "")
    folder_id = parse_drive_folder(folder_raw)
    results: dict[str, dict] = {}

    if Path("token.json").exists() and DRIVE_SCOPE in _token_scopes():
        try:
            drv = drive_service()
            who = drv.about().get(fields="user(emailAddress)").execute().get("user", {}).get("emailAddress", "ok")
            if folder_id:
                fld = drv.files().get(fileId=folder_id, fields="id,name", supportsAllDrives=True).execute()
                results["drive"] = {"ok": True, "msg": f"Drive OAuth OK ({who}) · Folder: {fld.get('name', folder_id)}"}
            else:
                results["drive"] = {"ok": True, "msg": f"Drive OAuth OK ({who})"}
        except Exception as err:  # noqa: BLE001
            results["drive"] = {"ok": False, "msg": str(err)[-160:]}
    elif dk:
        try:
            drv = build("drive", "v3", developerKey=dk)
            if folder_id:
                fld = drv.files().get(fileId=folder_id, fields="id,name", supportsAllDrives=True).execute()
                results["drive"] = {
                    "ok": True,
                    "msg": f"Drive API key OK · Folder: {fld.get('name', folder_id)} (note: uploads also need OAuth --login)",
                }
            else:
                drv.files().list(pageSize=1, fields="files(id)").execute()
                results["drive"] = {"ok": True, "msg": "Drive API key OK (note: uploads also need OAuth --login)"}
        except Exception as err:  # noqa: BLE001
            raw = str(err).replace(dk, "[REDACTED]")
            if "notFound" in raw or "404" in raw:
                msg = (
                    f"Drive API key is active, but folder '{folder_id}' is private or not found. "
                    "To access private folders and upload files, add client_secret.json and run: python3 automation.py --login"
                )
            else:
                msg = raw[-160:]
            results["drive"] = {"ok": False, "msg": msg}
    else:
        results["drive"] = {"ok": False, "msg": "Enter a Google Drive API key or run python automation.py --login"}

    return jsonify(results=results)


@bp.post("/api/auto/run")
def start_run():
    d = request.json or {}
    source = d.get("source", "url") if d.get("source") in ("url", "prompt") else "url"
    try:
        p = {
            "source": source,
            "url": (d.get("url") or "").strip(),
            "from_sec": max(0.0, float(d.get("from_sec", 0))),
            "prompt": (d.get("prompt") or "").strip(),
            "count": int(d.get("count", 3)),
            "length": int(d.get("length", 10 if source == "prompt" else 45)),
            "mode": d.get("mode", "blur") if d.get("mode") in ("blur", "crop") else "blur",
            "captions": bool(d.get("captions", True)),
            "niche": d.get("niche", ""),
            "upload": bool(d.get("upload")),
            "drive_upload": bool(d.get("drive_upload")),
            "drive_folder": parse_drive_folder(d.get("drive_folder") or _load_settings().get("drive_folder", "")),
            "publish": d.get("publish", "now"),
            "privacy": d.get("privacy", "public") if d.get("privacy") in ("public", "unlisted", "private") else "public",
            "interval": max(1.0, float(d.get("interval", 24))),
            "start": d.get("start") or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        }
        if p["upload"] and p["publish"] == "schedule":
            t = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
            if t <= datetime.now(timezone.utc):
                return jsonify(error="Schedule start must be in the future."), 400
    except (TypeError, ValueError):
        return jsonify(error="Invalid input values."), 400
    if p["source"] == "url" and not p["url"]:
        return jsonify(error="Paste a YouTube link."), 400
    if p["source"] == "prompt":
        if not p["prompt"]:
            return jsonify(error="Enter a video prompt."), 400
        if not os.getenv("GEMINI_API_KEY"):
            return jsonify(error="GEMINI_API_KEY is not set in the environment."), 400
    min_len = 3 if p["source"] == "prompt" else 5
    max_len = 15 if p["source"] == "prompt" else sc.MAX_SHORT_SECONDS
    if p["length"] < min_len or p["length"] > max_len or p["count"] < 1:
        return jsonify(error="Invalid count or length."), 400
    if (p["upload"] or p["drive_upload"]) and not Path("token.json").exists():
        return jsonify(error="Not signed in to Google. Run: python automation.py --login"), 400
    if p["drive_upload"] and DRIVE_SCOPE not in _token_scopes():
        return jsonify(error="Google Drive permission missing in token.json. Delete token.json and run: python automation.py --login"), 400
    jid = uuid.uuid4().hex[:8]
    drive_folder_url = f"https://drive.google.com/drive/folders/{p['drive_folder']}" if p["drive_folder"] else ""
    label = p["url"] if p["source"] == "url" else p["prompt"][:80]
    jobs[jid] = {
        "id": jid,
        "source": p["source"],
        "label": label,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "status": "Starting...",
        "progress": 0,
        "items": [],
        "drive_folder_url": drive_folder_url,
        "done": False,
        "error": None,
    }
    threading.Thread(target=run, args=(jid, p), daemon=True).start()
    return jsonify(job=jid)


@bp.get("/api/auto/job/<jid>")
def job_status(jid):
    return jsonify(jobs.get(jid, {"error": "Unknown job", "done": True}))


@bp.get("/api/auto/jobs")
def list_jobs():
    return jsonify(jobs=list(reversed(list(jobs.values())))[:20])


@bp.get("/automation")
def page():
    return PAGE


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shorts Studio &middot; Automation Dashboard</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--p2:#21262d;--line:#30363d;--text:#e6edf3;--mute:#8b949e;--blue:#58a6ff;--green:#238636;--greenh:#2ea043;--ok:#3fb950;--err:#f85149;--warn:#d29922}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Noto Sans,Helvetica,Arial,sans-serif}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
.topbar{background:#010409;border-bottom:1px solid var(--line);padding:14px 24px 0}
.toprow{max-width:1200px;margin:0 auto;display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding-bottom:12px}
.repo{display:flex;align-items:center;gap:8px;font-size:16px;font-weight:600}
.badge{font-size:12px;font-weight:500;padding:2px 8px;border:1px solid var(--line);border-radius:999px;color:var(--mute)}
.badge.ok{color:var(--ok);border-color:rgba(63,185,80,.4);background:rgba(63,185,80,.1)}
.badge.warn{color:var(--warn);border-color:rgba(210,153,34,.4);background:rgba(210,153,34,.1)}
.hright{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.navtabs{max-width:1200px;margin:0 auto;display:flex;gap:4px;overflow-x:auto}
.navtab{background:transparent;border:0;border-bottom:2px solid transparent;border-radius:6px 6px 0 0;color:var(--mute);padding:8px 14px 10px;font-size:14px;font-weight:500;cursor:pointer;display:flex;align-items:center;gap:6px}
.navtab:hover{color:var(--text);background:rgba(177,186,196,.08)}
.navtab.active{color:var(--text);font-weight:600;border-bottom-color:#f78166}
.cnt{background:rgba(110,118,129,.4);color:var(--text);border-radius:999px;padding:1px 7px;font-size:12px}
main{max-width:1200px;margin:20px auto;padding:0 20px 40px;display:grid;gap:16px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px 16px}
.stat .k{font-size:12px;color:var(--mute);margin-bottom:4px}.stat .v{font-size:20px;font-weight:600}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;align-items:start}
@media(max-width:920px){.layout{grid-template-columns:1fr}}
.box{background:var(--panel);border:1px solid var(--line);border-radius:6px;overflow:hidden;margin-bottom:16px}
.box-hd{background:var(--panel);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;align-items:center;justify-content:space-between;gap:10px;font-weight:600;font-size:14px}
.box-bd{padding:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
label{display:block;color:var(--text);font-size:13px;font-weight:500;margin-bottom:6px}
.sub{color:var(--mute);font-size:12px;font-weight:400}
input,select,textarea{width:100%;background:#0d1117;border:1px solid var(--line);color:var(--text);border-radius:6px;padding:8px 12px;font-size:14px;outline:none;color-scheme:dark;font-family:inherit}
input:focus,select:focus,textarea:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(88,166,255,.15)}
.tabs{display:inline-flex;background:#0d1117;border:1px solid var(--line);border-radius:6px;padding:3px;gap:4px}
.tabs button{background:transparent;border:0;color:var(--mute);padding:5px 12px;font-size:13px;border-radius:4px;font-weight:500}
.tabs button.on{background:var(--p2);color:var(--text);font-weight:600}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
.chk{display:flex;gap:8px;align-items:center;color:var(--text);font-weight:500;margin:0;cursor:pointer}.chk input{width:auto}
button{background:var(--p2);border:1px solid var(--line);color:var(--text);font-weight:500;border-radius:6px;padding:8px 14px;font-size:14px;cursor:pointer}
button:hover{background:#292e36;border-color:#8b949e}
button.primary{background:var(--green);border-color:rgba(240,246,252,.1);color:#fff;font-weight:600;padding:10px 16px}
button.primary:hover{background:var(--greenh)}
button:disabled{opacity:.5;cursor:not-allowed}
.mute{color:var(--mute);font-size:12px}.err{color:var(--err);font-size:13px}.ok{color:var(--ok);font-size:13px}
.bar{height:8px;background:#0d1117;border:1px solid var(--line);border-radius:999px;overflow:hidden;margin:10px 0}.bar div{height:100%;width:0;background:var(--ok);transition:width .3s}
.list-row{display:grid;grid-template-columns:32px minmax(0,1fr) auto;gap:12px;padding:12px 16px;border-top:1px solid var(--line);align-items:center}
.list-row:first-child{border-top:0}
.acts{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.btn-sm{display:inline-block;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:500;border:1px solid var(--line);background:var(--p2);color:var(--text);text-decoration:none}
.btn-sm:hover{border-color:#8b949e;text-decoration:none}
.btn-sm.dl{background:var(--green);border-color:rgba(240,246,252,.1);color:#fff}
.btn-sm.drv{color:var(--ok)}
.btn-sm.yt{color:var(--blue)}
.run-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;padding:12px 16px;border-top:1px solid var(--line);align-items:center;cursor:pointer}
.run-row:first-child{border-top:0}.run-row:hover{background:rgba(177,186,196,.04)}
.hidden{display:none!important}
</style></head><body>
<div class="topbar">
 <div class="toprow">
  <div class="repo">
   <span>&#9654;</span><span>shorts-studio</span><span class="mute">/</span><span>automation</span>
   <span class="badge">Local</span>
  </div>
  <div class="hright">
   <span class="badge" id="authBadge">Google: checking...</span>
   <span class="badge" id="aiBadge">AI: checking...</span>
   <a class="btn-sm" href="/">&larr; Manual editor</a>
  </div>
 </div>
 <div class="navtabs">
  <button type="button" class="navtab active" data-tab="dash">&#9655; Workflow</button>
  <button type="button" class="navtab" data-tab="runs">&#9889; Runs <span class="cnt" id="runsCnt">0</span></button>
  <button type="button" class="navtab" data-tab="settings">&#9881; Settings</button>
 </div>
</div>

<main>
 <section id="tab-dash">
  <div class="stats" style="margin-bottom:16px">
   <div class="stat"><div class="k">Google OAuth</div><div class="v" id="stGoogle" style="font-size:15px">Checking...</div></div>
   <div class="stat"><div class="k">AI Selection &amp; Video</div><div class="v" id="stAI" style="font-size:15px">Checking...</div></div>
   <div class="stat"><div class="k">Est. Daily Quota Used</div><div class="v" id="stQuota">4,800 / 10,000</div></div>
   <div class="stat"><div class="k">Recent Workflow Runs</div><div class="v" id="stRuns">0</div></div>
  </div>

  <div class="layout">
   <div>
    <div class="box">
     <div class="box-hd">
      <span>1. Source</span>
      <div class="tabs"><button type="button" class="seg on" data-src="url">From YouTube link</button><button type="button" class="seg" data-src="prompt">Generate from prompt</button></div>
     </div>
     <div class="box-bd">
      <div id="urlBox">
       <label>YouTube video link <span class="sub">(only videos you own or have rights to reuse)</span></label>
       <input id="url" placeholder="https://youtu.be/...">
       <div class="row"><span class="mute">Start clipping from</span><input id="fromMin" type="number" min="0" value="0" style="width:80px" aria-label="Start minutes"><span class="mute">min</span><input id="fromSec" type="number" min="0" max="59" value="0" style="width:80px" aria-label="Start seconds"><span class="mute">sec</span></div>
      </div>
      <div id="promptBox" class="hidden">
       <label>Video prompt <span class="sub">(9:16 1080p with audio via Gemini Omni 1.2 Flash)</span></label>
       <textarea id="prompt" rows="3" placeholder="e.g. Cinematic vertical drone shot gliding over a misty pine forest at golden hour, crisp morning light"></textarea>
      </div>
     </div>
    </div>

    <div class="box">
     <div class="box-hd"><span>2. Clip &amp; Caption Settings</span></div>
     <div class="box-bd">
      <div class="grid">
       <div><label>Number of Shorts</label><input id="count" type="number" min="1" max="20" value="3"></div>
       <div class="urlOnly"><label>Length of each (sec)</label><input id="length" type="number" min="5" max="180" value="45"></div>
       <div class="promptOnly hidden"><label>Clip duration (3–15 sec)</label><input id="omniLen" type="number" min="3" max="15" value="10"></div>
       <div class="urlOnly"><label>Style</label><select id="mode"><option value="blur">Blur fill</option><option value="crop">Center crop</option></select></div>
       <div><label>Niche / keywords</label><input id="niche" placeholder="e.g. fitness, workout tips"></div>
      </div>
      <div class="row" style="margin-top:14px">
       <label class="chk urlOnly"><input type="checkbox" id="captions" checked> Burned-in captions (lower-middle Shorts safe zone)</label>
      </div>
     </div>
    </div>

    <div class="box hidden" id="out">
     <div class="box-hd">
      <span id="status">Job output</span>
      <a id="driveFolderOut" class="btn-sm drv hidden" href="#" target="_blank">Open Drive folder &rarr;</a>
     </div>
     <div class="box-bd" style="padding-bottom:8px">
      <div class="bar"><div id="bar"></div></div>
      <div class="err" id="err"></div>
     </div>
     <div id="items"></div>
    </div>
   </div>

   <aside>
    <div class="box">
     <div class="box-hd"><span>3. Publish &amp; Destination</span></div>
     <div class="box-bd" style="display:grid;gap:14px">
      <div>
       <label class="chk"><input type="checkbox" id="upload" checked> Upload to YouTube</label>
       <div id="pub" style="display:grid;gap:10px;margin-top:10px">
        <div><label>Publish</label><select id="publish"><option value="now">Immediately</option><option value="schedule">Schedule (spread out)</option></select></div>
        <div id="privW"><label>Visibility</label><select id="privacy"><option>public</option><option>unlisted</option><option>private</option></select></div>
        <div class="sch hidden"><label>First post at</label><input id="start" type="datetime-local"></div>
        <div class="sch hidden"><label>Then every (hours)</label><input id="interval" type="number" min="1" value="24"></div>
       </div>
       <p class="mute" style="margin:8px 0 0">Each upload uses 1600 of the 10,000 daily API quota units (about 6 uploads/day).</p>
       <p class="err hidden" id="quotaWarn" style="margin:6px 0 0"></p>
      </div>
      <div style="border-top:1px solid var(--line);padding-top:12px">
       <label class="chk"><input type="checkbox" id="driveUpload"> Upload to Google Drive</label>
       <p id="driveHint" class="mute hidden" style="margin:6px 0 0"></p>
      </div>
      <button class="primary" id="go" style="width:100%">&#9654; Run automation</button>
     </div>
    </div>

    <div class="box">
     <div class="box-hd"><span>About</span></div>
     <div class="box-bd">
      <p class="mute" style="margin:0 0 8px">Only automate videos you own or have rights to reuse.</p>
      <p class="mute" style="margin:0">Clips stream remotely via <code>yt-dlp</code> + <code>ffmpeg</code> (9:16 1080&times;1920) without saving source videos locally.</p>
     </div>
    </div>
   </aside>
  </div>
 </section>

 <section id="tab-runs" class="hidden">
  <div class="box">
   <div class="box-hd"><span>Workflow Runs</span><button type="button" class="btn-sm" id="refreshRuns">Refresh</button></div>
   <div id="runsList"><div class="box-bd mute">No runs yet in this session.</div></div>
  </div>
 </section>

 <section id="tab-settings" class="hidden">
  <div class="box" style="max-width:760px">
   <div class="box-hd"><span>Google Drive Settings</span><span class="mute">Saved locally to settings.json</span></div>
   <div class="box-bd" style="display:grid;gap:14px">
    <div><label>Default Google Drive folder link / ID</label><input id="cfgDrive" placeholder="https://drive.google.com/drive/folders/..."></div>
    <div><label>Google Drive API Key</label><input id="cfgDriveKey" type="password" placeholder="AIza..."></div>
    <div class="row" style="margin-top:4px">
     <button type="button" class="primary" id="cfgSave">Save settings</button>
     <button type="button" id="cfgTest">Test API</button>
     <span class="mute" id="cfgMsg"></span>
    </div>
    <div id="cfgStatus" style="display:grid;gap:6px"></div>
   </div>
  </div>
 </section>
</main>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let source='url', pollTimer=null, driveConfigured=false;
const d=new Date(Date.now()+3600e3);d.setMinutes(0,0,0);
$('start').value=new Date(d-d.getTimezoneOffset()*6e4).toISOString().slice(0,16);

function showTab(name){
 document.querySelectorAll('.navtab').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));
 ['dash','runs','settings'].forEach(t=>$('tab-'+t).classList.toggle('hidden',t!==name));
 if(name==='runs')loadRuns();
}
document.querySelectorAll('.navtab').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));

function loadAuth(){
 fetch('/api/auto/auth').then(r=>r.json()).then(j=>{
  const b=$('authBadge');
  if(!j.ready){b.textContent='Google: signed out';b.className='badge warn';$('stGoogle').textContent='Not signed in';}
  else if(j.drive_ready){b.textContent='Google: YT + Drive';b.className='badge ok';$('stGoogle').textContent='YouTube + Drive';}
  else{b.textContent='Google: YT only';b.className='badge warn';$('stGoogle').textContent='YouTube only';}
 });
}
loadAuth();

function applyCfg(j){
 driveConfigured=Boolean(j.drive_folder);
 $('cfgDrive').value=j.drive_folder||'';
 $('cfgDriveKey').placeholder=j.has_drive_key?`Saved (${j.drive_key_mask}) — enter new key to replace`:'AIza...';
 const aiParts=[];if(j.has_anthropic)aiParts.push('Claude');if(j.has_gemini)aiParts.push('Omni');
 $('aiBadge').textContent=aiParts.length?`AI: ${aiParts.join(' + ')}`:'AI: fallback templates';
 $('aiBadge').className='badge'+(aiParts.length?' ok':'');
 $('stAI').textContent=aiParts.length?aiParts.join(' + '):'Templates only';
 sync();
}
fetch('/api/auto/settings').then(r=>r.json()).then(applyCfg);

$('cfgSave').onclick=async()=>{
 $('cfgMsg').textContent='Saving...';
 const r=await fetch('/api/auto/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
  drive_folder:$('cfgDrive').value,drive_api_key:$('cfgDriveKey').value
 })});
 applyCfg(await r.json());$('cfgDriveKey').value='';
 $('cfgMsg').textContent='Saved';setTimeout(()=>$('cfgMsg').textContent='',2000);
};

$('cfgTest').onclick=async()=>{
 $('cfgTest').disabled=true;$('cfgMsg').textContent='Testing...';$('cfgStatus').innerHTML='';
 try{
  const r=await fetch('/api/auto/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
   drive_folder:$('cfgDrive').value,drive_api_key:$('cfgDriveKey').value
  })});
  const res=(await r.json()).results||{};
  $('cfgStatus').innerHTML=Object.values(res).map(v=>`<div class="${v.ok?'ok':'err'}">${v.ok?'&#10003;':'&#10007;'} ${esc(v.msg)}</div>`).join('');
 }catch(e){$('cfgStatus').innerHTML=`<div class="err">${esc(e.message)}</div>`;}
 $('cfgMsg').textContent='';$('cfgTest').disabled=false;
};

function sync(){
 const s=$('publish').value==='schedule',u=$('upload').checked,du=$('driveUpload').checked,n=+$('count').value||0,pr=source==='prompt';
 document.querySelectorAll('.sch').forEach(e=>e.classList.toggle('hidden',!s));
 $('privW').classList.toggle('hidden',s);$('pub').classList.toggle('hidden',!u);
 $('driveHint').classList.toggle('hidden',!du);
 if(du)$('driveHint').innerHTML=driveConfigured
  ?'Using folder from <a href="#" onclick="showTab(\'settings\');return false">Settings</a>.'
  :'Uploading to My Drive root (<a href="#" onclick="showTab(\'settings\');return false">set folder in Settings</a>).';
 $('urlBox').classList.toggle('hidden',pr);$('promptBox').classList.toggle('hidden',!pr);
 document.querySelectorAll('.urlOnly').forEach(e=>e.classList.toggle('hidden',pr));
 document.querySelectorAll('.promptOnly').forEach(e=>e.classList.toggle('hidden',!pr));
 const units=u?n*1600:0;
 $('stQuota').textContent=`${units.toLocaleString()} / 10,000`;
 const over=u&&n>6;
 $('quotaWarn').classList.toggle('hidden',!over);
 if(over)$('quotaWarn').textContent=`Warning: ${n} uploads use ${units.toLocaleString()} quota units, exceeding the 10,000/day default (~6 uploads/day).`;
}
document.querySelectorAll('.seg').forEach(b=>b.onclick=()=>{
 source=b.dataset.src;
 document.querySelectorAll('.seg').forEach(x=>x.classList.toggle('on',x===b));
 if(source==='prompt'&&+$('count').value===3)$('count').value=1;
 sync();
});
$('publish').onchange=$('upload').onchange=$('driveUpload').onchange=$('count').oninput=sync;sync();

function renderJob(s){
 $('out').classList.remove('hidden');
 $('status').textContent=s.status||'';
 $('bar').style.width=Math.max(3,s.progress||0)+'%';
 if(s.drive_folder_url){$('driveFolderOut').href=s.drive_folder_url;$('driveFolderOut').classList.remove('hidden');}
 else $('driveFolderOut').classList.add('hidden');
 $('err').textContent=s.error||'';
 $('items').innerHTML=(s.items||[]).map(i=>`<div class="list-row"><b>#${i.n}</b><div><div style="font-weight:500">${esc(i.title||'...')}</div><div class="mute">${esc(i.status)}${i.when?' &middot; '+esc(new Date(i.when).toLocaleString()):''}</div></div><div class="acts">${i.file?`<a class="btn-sm" href="${esc(i.file)}" target="_blank">Preview</a><a class="btn-sm dl" href="${esc(i.file)}?dl=1" download="${esc(i.name||'')}">Download</a>`:''}${i.drive_link?`<a class="btn-sm drv" href="${esc(i.drive_link)}" target="_blank">Drive</a>`:''}${i.link?`<a class="btn-sm yt" href="${esc(i.link)}" target="_blank">YouTube</a>`:''}</div></div>`).join('');
}

function pollJob(jid){
 if(pollTimer)clearInterval(pollTimer);
 const tick=async()=>{
  const s=await(await fetch('/api/auto/job/'+jid)).json();
  renderJob(s);
  if(s.done){clearInterval(pollTimer);pollTimer=null;$('go').disabled=false;loadRuns();}
 };
 tick();
 pollTimer=setInterval(tick,1500);
}

async function loadRuns(){
 const list=(await(await fetch('/api/auto/jobs')).json()).jobs||[];
 $('runsCnt').textContent=list.length;$('stRuns').textContent=list.length;
 if(!list.length){$('runsList').innerHTML='<div class="box-bd mute">No runs yet in this session.</div>';return;}
 $('runsList').innerHTML=list.map(j=>{
  const cls=j.error?'warn':j.done?'ok':'';
  return `<div class="run-row" data-jid="${esc(j.id)}"><div><div style="font-weight:600">${esc(j.label||j.id)}</div><div class="mute">#${esc(j.id)} &middot; ${esc(j.source)} &middot; ${esc(j.created_at||'')} &middot; ${(j.items||[]).length} clip(s)</div></div><span class="badge ${cls}">${esc(j.error?'Failed':j.status)}</span></div>`;
 }).join('');
 document.querySelectorAll('.run-row').forEach(r=>r.onclick=()=>{showTab('dash');pollJob(r.dataset.jid);});
}
$('refreshRuns').onclick=loadRuns;loadRuns();

$('go').onclick=async()=>{
 $('err').textContent='';$('out').classList.remove('hidden');$('items').innerHTML='';$('bar').style.width='3%';$('go').disabled=true;$('driveFolderOut').classList.add('hidden');
 const body={source,url:$('url').value,
  from_sec:Math.max(0,(+$('fromMin').value||0)*60+(+$('fromSec').value||0)),
  prompt:$('prompt').value,count:+$('count').value,
  length:source==='prompt'?+$('omniLen').value:+$('length').value,mode:$('mode').value,
  captions:$('captions').checked,niche:$('niche').value,upload:$('upload').checked,
  drive_upload:$('driveUpload').checked,drive_folder:'',
  publish:$('publish').value,privacy:$('privacy').value,interval:+$('interval').value,
  start:$('start').value?new Date($('start').value).toISOString():''};
 const r=await fetch('/api/auto/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json();if(!r.ok){$('err').textContent=j.error;$('go').disabled=false;return}
 loadRuns();pollJob(j.job);
};
</script></body></html>
"""

if __name__ == "__main__":
    if "--login" in sys.argv:
        try:
            _google_creds(SCOPES)
            print("Signed in (YouTube + Google Drive). token.json saved.")
        except FileNotFoundError as err:
            sys.exit(str(err))
    else:
        print("Run `python app.py` and open http://127.0.0.1:5000/automation")
