#!/usr/bin/env python3
"""
shorts_clipper.py - Turn a YouTube link into vertical (9:16) clips for Shorts.

Requirements:
    pip install yt-dlp
    ffmpeg installed and on PATH, built with libass for burned-in captions
    (https://ffmpeg.org/download.html)

Usage examples:
    # One clip, 1:10 to 2:05, blurred-background style
    python shorts_clipper.py "https://youtu.be/VIDEO_ID" --clip 1:10-2:05

    # Several clips, center-crop style
    python shorts_clipper.py "URL" --clip 0:30-1:00 --clip 5:00-5:45 --mode crop

    # Auto-split the whole video (or a range) into 58-second Shorts
    python shorts_clipper.py "URL" --auto-split 58
    python shorts_clipper.py "URL" --auto-split 58 --range 2:00-10:00

Only use this on videos you own or have permission to reuse.
Downloading other people's videos may violate YouTube's Terms of Service
and copyright law.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MAX_SHORT_SECONDS = 180  # YouTube Shorts allow up to 3 minutes


def _stream_format() -> str:
    h = int(os.getenv("MAX_HEIGHT", "1080"))
    return (
        f"bv*[height<={h}][protocol^=http]+ba[protocol^=http]/"
        f"b[height<={h}][protocol^=http]/"
        f"bv*[height<={h}]+ba/b[height<={h}]/b"
    )


def temp_workdir() -> Path:
    """Return a system-temp working directory (avoids saving videos in project folder)."""
    d = Path(tempfile.gettempdir()) / "shorts_clipper"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_temp(max_age_sec: int = 7200) -> None:
    """Remove stale temporary clips/subtitles older than max_age_sec."""
    root = Path(tempfile.gettempdir()) / "shorts_clipper"
    if not root.exists():
        return
    cutoff = time.time() - max_age_sec
    for p in root.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
        except OSError:
            pass


def parse_time(value: str) -> float:
    """Accepts SS, MM:SS or HH:MM:SS (decimals allowed). Returns seconds."""
    parts = [float(p) for p in value.strip().split(":")]
    if len(parts) > 3:
        raise ValueError(f"Bad timestamp: {value}")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def parse_range(value: str) -> tuple[float, float]:
    try:
        start, end = value.split("-", 1)
        s, e = parse_time(start), parse_time(end)
    except ValueError:
        sys.exit(f"Invalid range '{value}'. Use START-END, e.g. 1:10-2:05")
    if e <= s:
        sys.exit(f"End must be after start in range '{value}'")
    return s, e


def check_tools() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found. Install it and make sure it is on your PATH.")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        sys.exit("yt-dlp not found. Run: pip install yt-dlp")


def _normalize_cookies(raw: str) -> str:
    """Accept Netscape cookies.txt, base64, escaped \\n, or JSON cookie exports."""
    text = raw.strip()
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    if "\n" not in text and not text.startswith(("#", "[")):
        try:
            pad = "=" * (-len(text) % 4)
            decoded = base64.b64decode(text + pad).decode("utf-8")
            if "\t" in decoded or decoded.lstrip().startswith(("#", "[")):
                text = decoded
        except Exception:  # noqa: BLE001
            pass
    if text.lstrip().startswith("["):
        items = json.loads(text)
        lines = ["# Netscape HTTP Cookie File"]
        for c in items:
            dom = str(c.get("domain", ""))
            sub = "TRUE" if dom.startswith(".") else "FALSE"
            path = str(c.get("path", "/"))
            sec = "TRUE" if c.get("secure") else "FALSE"
            exp = str(int(float(c.get("expirationDate") or c.get("expiry") or 0)))
            name, val = str(c.get("name", "")), str(c.get("value", ""))
            if dom and name:
                lines.append(f"{dom}\t{sub}\t{path}\t{sec}\t{exp}\t{name}\t{val}")
        text = "\n".join(lines) + "\n"
    elif "\n" not in text and "\t" in text:
        # Recover lines if a single-line env var box flattened newlines into spaces
        text = re.sub(r"^#(?: Netscape HTTP Cookie File| HTTP Cookie File)\s*", "", text)
        text = re.sub(r"\s+(?=\.[a-z0-9.-]+\t|youtube\.com\t|www\.youtube\.com\t)", "\n", text)
        text = "# Netscape HTTP Cookie File\n" + text.strip() + "\n"
    elif not text.startswith("# Netscape") and not text.startswith("# HTTP Cookie"):
        text = "# Netscape HTTP Cookie File\n" + text
    if not any(ln and not ln.startswith("#") and ln.count("\t") >= 6 for ln in text.splitlines()):
        raise ValueError(
            "YT_COOKIES is not valid Netscape cookies.txt (or base64/JSON). "
            "Run: base64 -w0 cookies.txt"
        )
    return text if text.endswith("\n") else text + "\n"


def ydl_opts(**extra) -> dict:
    """Build yt-dlp options, attaching YT_COOKIES / YT_PO_TOKEN env vars and IPv4 binding."""
    opts = {"quiet": True, "noplaylist": True, "source_address": "0.0.0.0", **extra}
    cookies = os.getenv("YT_COOKIES", "").strip()
    if cookies:
        norm = _normalize_cookies(cookies)
        cpath = temp_workdir() / "cookies.txt"
        if not cpath.exists() or cpath.read_text(encoding="utf-8", errors="ignore") != norm:
            cpath.write_text(norm, encoding="utf-8")
        opts["cookiefile"] = str(cpath)
    po_token = os.getenv("YT_PO_TOKEN", "").strip().strip("\"'")
    if po_token:
        tok = po_token if "+" in po_token else f"web.gvs+{po_token}"
        ex = dict(opts.get("extractor_args") or {})
        yt_ex = dict(ex.get("youtube") or {})
        yt_ex["po_token"] = [tok]
        if "player_client" not in yt_ex:
            client = tok.split(".", 1)[0].split("+", 1)[0]
            if client:
                yt_ex["player_client"] = [client, "tv", "web_safari"]
        ex["youtube"] = yt_ex
        opts["extractor_args"] = ex
    return opts


def download(url: str, workdir: Path) -> tuple[Path, float, str]:
    import yt_dlp

    workdir.mkdir(parents=True, exist_ok=True)
    opts = ydl_opts(
        format="bv*[height<=1080]+ba/b[height<=1080]/b",
        merge_output_format="mp4",
        outtmpl=str(workdir / "source.%(ext)s"),
        quiet=False,
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    source = workdir / "source.mp4"
    if not source.exists():
        matches = list(workdir.glob("source.*"))
        if not matches:
            sys.exit("Download failed: no source file found.")
        source = matches[0]
    title = "".join(c if c.isalnum() or c in " -_" else "" for c in info.get("title", "video"))
    return source, float(info.get("duration") or 0), title.strip().replace(" ", "_")[:50] or "video"


def get_streams(
    url: str, player_clients: list[str] | None = None
) -> tuple[list[tuple[str, dict]], float, str, dict]:
    """Resolve remote video/audio stream URLs via yt-dlp without downloading locally."""
    import yt_dlp

    fmt_primary = _stream_format()
    extra = {"extractor_args": {"youtube": {"player_client": player_clients}}} if player_clients else {}
    info = None
    for fmt in (fmt_primary, "bestvideo+bestaudio/best"):
        try:
            with yt_dlp.YoutubeDL(ydl_opts(format=fmt, skip_download=True, **extra)) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except Exception as err:  # noqa: BLE001
            if "Requested format is not available" not in str(err) or fmt == "bestvideo+bestaudio/best":
                raise
    req_fmts = info.get("requested_formats") or []
    if req_fmts:
        streams = [(f["url"], f.get("http_headers") or {}) for f in req_fmts[:2] if f.get("url")]
    elif info.get("url"):
        streams = [(info["url"], info.get("http_headers") or {})]
    else:
        raise RuntimeError("No playable stream URL found for this video.")
    title = "".join(c if c.isalnum() or c in " -_" else "" for c in info.get("title", "video"))
    safe_title = title.strip().replace(" ", "_")[:50] or "video"
    return streams, float(info.get("duration") or 0), safe_title, info


def _ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _split_cue(cs: float, ce: float, text: str, max_words: int = 6) -> list[tuple[float, float, str]]:
    words = text.split()
    if len(words) <= max_words:
        return [(cs, ce, " ".join(words))]
    chunks = [words[i:i + max_words] for i in range(0, len(words), max_words)]
    total = len(words)
    dur = ce - cs
    out: list[tuple[float, float, str]] = []
    cur = cs
    for idx, chunk in enumerate(chunks):
        nxt = ce if idx == len(chunks) - 1 else cur + dur * (len(chunk) / total)
        out.append((cur, nxt, " ".join(chunk)))
        cur = nxt
    return out


def write_ass(transcript: list[dict], start: float, end: float, out_path: Path) -> Path | None:
    """Write a 1080x1920 .ass subtitle file for [start, end], shifted to clip start."""
    if not transcript or end <= start:
        return None
    dur = end - start
    cues: list[tuple[float, float, str]] = []
    for item in transcript:
        ts = float(item.get("start", 0.0))
        te = float(item.get("end", 0.0))
        text = " ".join(str(item.get("text", "")).split())
        if not text or te <= start or ts >= end:
            continue
        cs = max(0.0, ts - start)
        ce = min(dur, te - start)
        if ce - cs < 0.05:
            continue
        cues.extend(_split_cue(cs, ce, text))

    if not cues:
        return None

    cues.sort(key=lambda x: x[0])
    events: list[str] = []
    for i, (cs, ce, text) in enumerate(cues):
        if i + 1 < len(cues) and ce > cues[i + 1][0]:
            ce = cues[i + 1][0]
        if ce - cs < 0.05:
            continue
        safe = text.replace("{", r"\{").replace("}", r"\}")
        events.append(f"Dialogue: 0,{_ass_time(cs)},{_ass_time(ce)},Default,,0,0,0,,{safe}")

    if not events:
        return None

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,64,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,5,2,2,90,90,420,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return out_path


def _escape_filter_path(path: Path) -> str:
    s = str(path.resolve()).replace("\\", "/")
    for ch in (":", "'", "[", "]", ",", ";"):
        s = s.replace(ch, f"\\{ch}")
    return s


def build_filter(mode: str, subs_path: Path | None = None, audio_idx: int = 0) -> list[str]:
    ass = f",ass={_escape_filter_path(subs_path)}" if subs_path else ""
    if mode == "crop":
        # Center-crop to 9:16, fills the whole frame (loses left/right sides)
        vf = f"crop=ih*9/16:ih,scale=1080:1920,setsar=1{ass}"
        if audio_idx > 0:
            return ["-vf", vf, "-map", "0:v", "-map", f"{audio_idx}:a?"]
        return ["-vf", vf]
    # Blur mode: downscale background to 270x480 before blurring to cut RAM/CPU by ~16x
    graph = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=270:480:force_original_aspect_ratio=increase,"
        "crop=270:480,boxblur=6:2,scale=1080:1920:flags=bilinear[bgb];"
        "[fg]scale=1080:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1{ass}[v]"
    )
    return ["-filter_complex", graph, "-map", "[v]", "-map", f"{audio_idx}:a?"]


def _run_ffmpeg_cut(
    inputs: list[tuple[str, dict]],
    start: float,
    duration: float,
    out: Path,
    mode: str,
    subs_path: Path | None,
    preset: str,
) -> None:
    cmd = ["ffmpeg", "-y"]
    for src, hdrs in inputs:
        src_str = str(src)
        if src_str.startswith(("http://", "https://")):
            cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"])
            if hdrs:
                ua = next((v for k, v in hdrs.items() if k.lower() == "user-agent"), None)
                if ua:
                    cmd.extend(["-user_agent", ua])
                hdr_lines = "".join(
                    f"{k}: {v}\r\n"
                    for k, v in hdrs.items()
                    if k.lower() not in ("host", "content-length", "user-agent")
                )
                if hdr_lines:
                    cmd.extend(["-headers", hdr_lines])
        cmd.extend(["-ss", f"{start:.3f}", "-i", src_str])
    threads = os.getenv("FFMPEG_THREADS", "0")
    cmd.extend([
        "-t", f"{duration:.3f}",
        *build_filter(mode, subs_path, audio_idx=1 if len(inputs) > 1 else 0),
        "-c:v", "libx264", "-preset", preset, "-crf", "22", "-pix_fmt", "yuv420p", "-threads", threads,
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        str(out),
    ])
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


_FALLBACK_LOCK = threading.Lock()


def _download_source(url: str, workdir: Path) -> Path:
    """Download via yt-dlp's Python HTTP stack (avoids ffmpeg HTTP 403 / exit code 8)."""
    import yt_dlp

    with _FALLBACK_LOCK:
        existing = [p for p in workdir.glob("fallback.*") if p.suffix in (".mp4", ".mkv", ".webm")]
        if existing:
            return existing[0]
        h = int(os.getenv("MAX_HEIGHT", "1080"))
        opts = ydl_opts(
            format=f"bv*[height<={h}]+ba/b[height<={h}]/bestvideo+bestaudio/best",
            merge_output_format="mp4",
            outtmpl=str(workdir / "fallback.%(ext)s"),
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        return next(p for p in workdir.glob("fallback.*") if p.suffix in (".mp4", ".mkv", ".webm"))


def cut_clip(
    source: Path | str | list[tuple[str, dict]],
    start: float,
    end: float,
    out: Path,
    mode: str,
    subs_path: Path | None = None,
    preset: str = "veryfast",
    fallback_url: str | None = None,
    fallback_dir: Path | None = None,
) -> None:
    duration = end - start
    if duration > MAX_SHORT_SECONDS:
        print(f"  ! Warning: clip is {duration:.0f}s; Shorts max is {MAX_SHORT_SECONDS}s.")
    inputs = source if isinstance(source, list) else [(str(source), {})]
    try:
        _run_ffmpeg_cut(inputs, start, duration, out, mode, subs_path, preset)
    except subprocess.CalledProcessError:
        if not (isinstance(source, list) and fallback_url):
            raise
        try:
            alt_streams, _, _, _ = get_streams(fallback_url, player_clients=["tv", "web_safari"])
            _run_ffmpeg_cut(alt_streams, start, duration, out, mode, subs_path, preset)
        except Exception:  # noqa: BLE001
            if fallback_dir is not None:
                sec = _download_source(fallback_url, fallback_dir)
                _run_ffmpeg_cut([(str(sec), {})], start, duration, out, mode, subs_path, preset)
            else:
                with tempfile.TemporaryDirectory() as td:
                    sec = _download_source(fallback_url, Path(td))
                    _run_ffmpeg_cut([(str(sec), {})], start, duration, out, mode, subs_path, preset)


def main() -> None:
    p = argparse.ArgumentParser(description="Clip a YouTube video into vertical Shorts.")
    p.add_argument("url", help="YouTube video link")
    p.add_argument("--clip", action="append", default=[], metavar="START-END",
                   help="Time range to clip, e.g. 1:10-2:05. Repeat for multiple clips.")
    p.add_argument("--auto-split", type=int, metavar="SECONDS",
                   help="Split into equal chunks of this length (e.g. 58)")
    p.add_argument("--range", dest="split_range", metavar="START-END",
                   help="Limit --auto-split to this part of the video")
    p.add_argument("--mode", choices=["blur", "crop"], default="blur",
                   help="blur = full video + blurred background (default); crop = center crop")
    p.add_argument("--out", default="shorts_output", help="Output folder")
    p.add_argument("--keep-source", action="store_true", help="Keep the downloaded source video")
    args = p.parse_args()

    if not args.clip and not args.auto_split:
        sys.exit("Give at least one --clip START-END or use --auto-split SECONDS.")

    check_tools()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_work"

    if args.keep_source:
        print("Downloading video...")
        source, total, title = download(args.url, work)
    else:
        print("Resolving remote streams...")
        source, total, title, _ = get_streams(args.url)

    ranges: list[tuple[float, float]] = [parse_range(c) for c in args.clip]

    if args.auto_split:
        lo, hi = (parse_range(args.split_range) if args.split_range else (0.0, total))
        hi = min(hi, total) if total else hi
        t = lo
        while t < hi:
            end = min(t + args.auto_split, hi)
            if end - t >= 5:  # skip tiny leftovers
                ranges.append((t, end))
            t += args.auto_split

    print(f"Creating {len(ranges)} clip(s)...")
    for i, (s, e) in enumerate(ranges, 1):
        out = out_dir / f"{title}_short_{i:02d}.mp4"
        print(f"  [{i}/{len(ranges)}] {s:.0f}s -> {e:.0f}s  =>  {out.name}")
        try:
            cut_clip(source, s, e, out, args.mode)
        except subprocess.CalledProcessError as err:
            print("  ffmpeg failed:", err.stderr.decode(errors="ignore")[-500:])

    if not args.keep_source:
        shutil.rmtree(work, ignore_errors=True)

    print(f"\nDone. Clips saved in: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
