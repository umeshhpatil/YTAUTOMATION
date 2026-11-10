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
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_SHORT_SECONDS = 180  # YouTube Shorts allow up to 3 minutes
STREAM_FORMAT = (
    "bv*[height<=1080][protocol^=http]+ba[protocol^=http]/"
    "b[height<=1080][protocol^=http]/"
    "bv*[height<=1080]+ba/b[height<=1080]/b"
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


def ydl_opts(**extra) -> dict:
    """Build yt-dlp options, attaching YT_COOKIES env var if set (helpful on cloud IPs)."""
    opts = {"quiet": True, "noplaylist": True, **extra}
    cookies = os.getenv("YT_COOKIES", "").strip()
    if cookies:
        cpath = temp_workdir() / "cookies.txt"
        cpath.write_text(cookies, encoding="utf-8")
        opts["cookiefile"] = str(cpath)
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


def get_streams(url: str) -> tuple[list[tuple[str, dict]], float, str, dict]:
    """Resolve remote video/audio stream URLs via yt-dlp without downloading locally."""
    import yt_dlp

    opts = ydl_opts(format=STREAM_FORMAT, skip_download=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
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
    # Blur mode: full video centered, blurred copy fills the background
    graph = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=25:5[bgb];"
        "[fg]scale=1080:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1{ass}[v]"
    )
    return ["-filter_complex", graph, "-map", "[v]", "-map", f"{audio_idx}:a?"]


def cut_clip(
    source: Path | str | list[tuple[str, dict]],
    start: float,
    end: float,
    out: Path,
    mode: str,
    subs_path: Path | None = None,
    preset: str = "veryfast",
) -> None:
    duration = end - start
    if duration > MAX_SHORT_SECONDS:
        print(f"  ! Warning: clip is {duration:.0f}s; Shorts max is {MAX_SHORT_SECONDS}s.")
    inputs = source if isinstance(source, list) else [(str(source), {})]
    cmd = ["ffmpeg", "-y"]
    for src, hdrs in inputs:
        src_str = str(src)
        if src_str.startswith(("http://", "https://")):
            cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"])
            if hdrs:
                hdr_lines = "".join(
                    f"{k}: {v}\r\n" for k, v in hdrs.items() if k.lower() not in ("host", "content-length")
                )
                if hdr_lines:
                    cmd.extend(["-headers", hdr_lines])
        cmd.extend(["-ss", f"{start:.3f}", "-i", src_str])
    cmd.extend([
        "-t", f"{duration:.3f}",
        *build_filter(mode, subs_path, audio_idx=1 if len(inputs) > 1 else 0),
        "-c:v", "libx264", "-preset", preset, "-crf", "22", "-pix_fmt", "yuv420p", "-threads", "0",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        str(out),
    ])
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


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
