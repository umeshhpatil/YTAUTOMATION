#!/usr/bin/env python3
"""
Shorts Clipper - local web UI (dark theme)

Setup:
    pip install flask yt-dlp
    ffmpeg must be installed and on PATH
    Keep this file in the same folder as shorts_clipper.py

Run:
    python app.py
    then open http://127.0.0.1:5000

Only use this on videos you own or have permission to reuse.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hmac
import os
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

import automation
import shorts_clipper as sc

app = Flask(__name__)
automation.register(app)
OUT = sc.temp_workdir()
jobs: dict[str, dict] = {}


@app.before_request
def _require_password():
    pw = os.getenv("APP_PASSWORD")
    if not pw:
        return None
    auth = request.authorization
    if auth and hmac.compare_digest(auth.password or "", pw):
        return None
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Shorts Studio"'})


def get_info(url: str) -> dict:
    import yt_dlp

    with yt_dlp.YoutubeDL(sc.ydl_opts(skip_download=True)) as ydl:
        return ydl.extract_info(url, download=False)


def run_job(jid: str, url: str, clips: list, mode: str) -> None:
    job = jobs[jid]
    try:
        sc.cleanup_temp()
        job["status"] = "Connecting to remote stream..."
        streams, _, _, info = sc.get_streams(url)
        vid = info["id"]
        total = len(clips)
        job["status"] = f"Clipping {total} clip(s) remotely..."
        results: dict[int, dict] = {}
        done_count = 0
        lock = threading.Lock()

        def _cut(idx: int, s: float, e: float) -> tuple[int, dict]:
            name = f"{vid}_{jid}_{int(s)}-{int(e)}_{mode}.mp4"
            sc.cut_clip(streams, float(s), float(e), OUT / name, mode)
            return idx, {"name": f"{vid}_{int(s)}-{int(e)}_{mode}.mp4", "url": f"/clips/{name}"}

        workers = min(max(1, int(os.getenv("MAX_WORKERS", "3"))), total)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(_cut, i, s, e) for i, (s, e) in enumerate(clips, 1)]
            for fut in as_completed(futs):
                idx, entry = fut.result()
                with lock:
                    results[idx] = entry
                    done_count += 1
                    job["progress"] = round(done_count / total * 100)
                    job["status"] = f"Clipped {done_count} of {total}..."

        job["files"] = [results[i] for i in range(1, total + 1)]
        job["progress"] = 100
        job["status"] = "Done"
        job["done"] = True
    except Exception as err:  # noqa: BLE001
        job["error"] = str(err)[-400:]
        job["done"] = True


@app.get("/")
def index():
    return PAGE


@app.post("/api/info")
def api_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Paste a YouTube link first."), 400
    try:
        i = get_info(url)
    except Exception as err:  # noqa: BLE001
        return jsonify(error=f"Could not load video: {str(err)[-200:]}"), 400
    return jsonify(
        id=i["id"],
        title=i.get("title", ""),
        duration=i.get("duration") or 0,
        thumbnail=f"https://i.ytimg.com/vi/{i['id']}/hqdefault.jpg",
    )


@app.post("/api/process")
def api_process():
    d = request.json or {}
    clips = d.get("clips", [])
    mode = d.get("mode", "blur")
    if not clips or mode not in ("blur", "crop"):
        return jsonify(error="Add at least one clip."), 400
    for s, e in clips:
        if e <= s or e - s > sc.MAX_SHORT_SECONDS:
            return jsonify(error=f"Clip {s}-{e}s is invalid (max {sc.MAX_SHORT_SECONDS}s)."), 400
    jid = uuid.uuid4().hex[:8]
    jobs[jid] = {"status": "Starting...", "progress": 0, "files": [], "done": False, "error": None}
    threading.Thread(target=run_job, args=(jid, d["url"], clips, mode), daemon=True).start()
    return jsonify(job=jid)


@app.get("/api/job/<jid>")
def api_job(jid):
    return jsonify(jobs.get(jid, {"error": "Unknown job", "done": True}))


@app.get("/clips/<path:name>")
def clips(name):
    return send_from_directory(OUT.resolve(), name, as_attachment=request.args.get("dl") == "1")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shorts Clipper</title>
<style>
:root{--bg:#0b0d12;--panel:#141821;--panel2:#1b2030;--line:#262d3f;--text:#e8ebf2;--mute:#8a93a8;--accent:#ff3b5c;--ok:#34d399}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:18px 28px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px}
header h1{font-size:18px;margin:0}
.logo{width:30px;height:30px;border-radius:8px;background:var(--accent);display:grid;place-items:center;font-weight:700}
main{max-width:1200px;margin:24px auto;padding:0 20px;display:grid;gap:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=text],input[type=number]{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:10px;padding:10px 12px;font-size:15px;outline:none}
input[type=text]{flex:1;min-width:240px}
input:focus{border-color:var(--accent)}
button{background:var(--panel2);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:9px 14px;font-size:14px;cursor:pointer}
button:hover{border-color:#3a4460}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
button.primary:disabled{opacity:.5;cursor:not-allowed}
button.seg.on{background:var(--accent);border-color:var(--accent)}
.grid{display:grid;grid-template-columns:1fr 280px;gap:20px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.player{aspect-ratio:16/9;background:#000;border-radius:12px;overflow:hidden}
.player iframe{width:100%;height:100%}
.hidden{display:none!important}
label{color:var(--mute);font-size:13px}
input[type=range]{width:100%;accent-color:var(--accent)}
.time{font-variant-numeric:tabular-nums;font-weight:600}
.mute{color:var(--mute);font-size:13px}
.err{color:#ff8a8a;margin-top:8px}
h3{margin:0 0 10px;font-size:15px}
.phone{aspect-ratio:9/16;width:200px;margin:0 auto;border-radius:24px;overflow:hidden;position:relative;background:#000;border:2px solid var(--line)}
.phone .bg{position:absolute;inset:-20px;background-size:cover;background-position:center;filter:blur(18px) brightness(.7)}
.phone img{position:absolute;top:50%;left:0;width:100%;transform:translateY(-50%)}
.phone.crop .bg{display:none}
.phone.crop img{height:100%;width:auto;left:50%;top:0;transform:translateX(-50%)}
.clip{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;background:var(--panel2);border-radius:10px;margin-bottom:8px}
.bar{height:8px;background:var(--panel2);border-radius:6px;overflow:hidden;margin:10px 0}
.bar div{height:100%;width:0;background:var(--accent);transition:width .3s}
.results{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}
.results video{width:100%;aspect-ratio:9/16;background:#000;border-radius:12px}
.results a.dlbtn{display:inline-block;margin-top:6px;background:var(--accent);color:#fff;padding:6px 12px;border-radius:8px;font-size:13px;font-weight:600;text-decoration:none}
</style>
</head>
<body>
<header><div class="logo">S</div><h1>Shorts Clipper</h1><span class="mute">paste a link, pick a range, export 9:16</span><a href="/automation" style="margin-left:auto;color:var(--accent);text-decoration:none">Automation &rarr;</a></header>
<main>
  <section class="card">
    <div class="row">
      <input type="text" id="url" placeholder="Paste YouTube link (only videos you own or have permission to reuse)">
      <button class="primary" id="loadBtn">Load video</button>
    </div>
    <div class="err" id="err"></div>
  </section>

  <section class="grid hidden" id="editor">
    <div class="card">
      <h3 id="title"></h3>
      <div class="player"><div id="player"></div></div>
      <div style="margin-top:16px">
        <div class="row" style="justify-content:space-between">
          <label>Start <span class="time" id="sLbl">0:00</span></label>
          <label>Now <span class="time" id="nowLbl">0:00</span></label>
          <label>End <span class="time" id="eLbl">0:00</span></label>
        </div>
        <input type="range" id="sRange" min="0" step="1" value="0">
        <input type="range" id="eRange" min="0" step="1" value="0">
        <div class="row" style="margin-top:8px">
          <button id="setS">Start = now</button>
          <button id="setE">End = now</button>
          <button id="prev">&#9654; Preview range</button>
          <span class="mute" id="lenLbl"></span>
        </div>
        <div class="row" style="margin-top:14px">
          <button class="primary" id="addClip">+ Add clip</button>
          <span class="mute">or split selected range into</span>
          <input type="number" id="splitLen" value="58" min="5" max="180" style="width:80px">
          <span class="mute">sec</span>
          <button id="split">Auto-split</button>
        </div>
      </div>
    </div>

    <div class="card">
      <h3>Style</h3>
      <div class="row" style="margin-bottom:12px">
        <button class="seg on" data-mode="blur">Blur fill</button>
        <button class="seg" data-mode="crop">Center crop</button>
      </div>
      <div class="phone" id="phone"><div class="bg" id="phoneBg"></div><img id="phoneImg" alt=""></div>
      <p class="mute" style="text-align:center;margin:8px 0 0">9:16 preview</p>
    </div>
  </section>

  <section class="card hidden" id="queue">
    <h3>Clips <span class="mute" id="count"></span></h3>
    <div id="clipList"></div>
    <button class="primary" id="go">Create Shorts</button>
    <div id="status" class="mute" style="margin-top:10px"></div>
    <div class="bar hidden" id="barWrap"><div id="bar"></div></div>
  </section>

  <section class="card hidden" id="resultCard">
    <h3>Your Shorts</h3>
    <div class="results" id="results"></div>
  </section>
</main>

<script src="https://www.youtube.com/iframe_api"></script>
<script>
const $ = id => document.getElementById(id);
let info = null, player = null, ytReady = false, clips = [], mode = 'blur', previewEnd = null;

window.onYouTubeIframeAPIReady = () => { ytReady = true; };

const pad = n => String(n).padStart(2, '0');
function fmt(s){
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? `${h}:${pad(m)}:${pad(x)}` : `${m}:${pad(x)}`;
}
const getS = () => +$('sRange').value;
const getE = () => +$('eRange').value;

function refresh(){
  if (getE() <= getS()) {
    if ($('sRange').dataset.last === 'e') $('sRange').value = Math.max(0, getE() - 1);
    else $('eRange').value = Math.min(+$('eRange').max, getS() + 1);
  }
  $('sLbl').textContent = fmt(getS());
  $('eLbl').textContent = fmt(getE());
  const len = getE() - getS();
  $('lenLbl').textContent = `Length: ${len}s` + (len > 180 ? ' (over 3 min Shorts limit)' : '');
  $('lenLbl').style.color = len > 180 ? '#ff8a8a' : '';
}

$('sRange').oninput = () => { $('sRange').dataset.last = 's'; refresh(); };
$('eRange').oninput = () => { $('sRange').dataset.last = 'e'; refresh(); };

async function loadVideo(){
  $('err').textContent = '';
  $('loadBtn').disabled = true; $('loadBtn').textContent = 'Loading...';
  try {
    const r = await fetch('/api/info', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url: $('url').value})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error);
    info = d; clips = [];
    $('title').textContent = d.title;
    ['sRange','eRange'].forEach(id => $(id).max = d.duration);
    $('sRange').value = 0; $('eRange').value = Math.min(d.duration, 58);
    $('phoneImg').src = d.thumbnail;
    $('phoneBg').style.backgroundImage = `url(${d.thumbnail})`;
    $('editor').classList.remove('hidden'); $('queue').classList.remove('hidden');
    $('resultCard').classList.add('hidden');
    refresh(); renderClips(); makePlayer(d.id);
  } catch (e) { $('err').textContent = e.message; }
  $('loadBtn').disabled = false; $('loadBtn').textContent = 'Load video';
}

function makePlayer(id){
  if (player && player.loadVideoById) { player.cueVideoById(id); return; }
  const start = () => { player = new YT.Player('player', {videoId: id, playerVars: {rel: 0}}); };
  if (ytReady) start(); else window.onYouTubeIframeAPIReady = () => { ytReady = true; start(); };
}

setInterval(() => {
  if (!player || !player.getCurrentTime) return;
  const t = player.getCurrentTime();
  $('nowLbl').textContent = fmt(t);
  if (previewEnd !== null && t >= previewEnd) { player.pauseVideo(); previewEnd = null; }
}, 200);

$('setS').onclick = () => { $('sRange').value = Math.floor(player.getCurrentTime()); $('sRange').dataset.last = 's'; refresh(); };
$('setE').onclick = () => { $('eRange').value = Math.ceil(player.getCurrentTime()); $('sRange').dataset.last = 'e'; refresh(); };
$('prev').onclick = () => { previewEnd = getE(); player.seekTo(getS(), true); player.playVideo(); };

function renderClips(){
  $('count').textContent = clips.length ? `(${clips.length})` : '';
  $('go').disabled = !clips.length;
  $('clipList').innerHTML = clips.length ? '' : '<p class="mute">No clips yet. Pick a range and click "Add clip".</p>';
  clips.forEach((c, i) => {
    const el = document.createElement('div'); el.className = 'clip';
    el.innerHTML = `<span><b>#${i+1}</b> ${fmt(c[0])} - ${fmt(c[1])} <span class="mute">(${c[1]-c[0]}s)</span></span><span></span>`;
    const p = document.createElement('button'); p.textContent = 'Preview';
    p.onclick = () => { previewEnd = c[1]; player.seekTo(c[0], true); player.playVideo(); };
    const d = document.createElement('button'); d.textContent = 'Remove';
    d.onclick = () => { clips.splice(i, 1); renderClips(); };
    el.lastChild.append(p, ' ', d); $('clipList').append(el);
  });
}

$('addClip').onclick = () => {
  if (getE() - getS() > 180) return alert('Shorts can be at most 3 minutes.');
  clips.push([getS(), getE()]); renderClips();
};
$('split').onclick = () => {
  const n = Math.max(5, Math.min(180, +$('splitLen').value || 58));
  for (let t = getS(); t < getE(); t += n) {
    const e = Math.min(t + n, getE());
    if (e - t >= 5) clips.push([t, e]);
  }
  renderClips();
};

document.querySelectorAll('.seg').forEach(b => b.onclick = () => {
  mode = b.dataset.mode;
  document.querySelectorAll('.seg').forEach(x => x.classList.toggle('on', x === b));
  $('phone').classList.toggle('crop', mode === 'crop');
});

$('go').onclick = async () => {
  $('go').disabled = true; $('err').textContent = '';
  $('barWrap').classList.remove('hidden'); $('bar').style.width = '3%';
  $('status').textContent = 'Starting...';
  const r = await fetch('/api/process', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url: $('url').value, clips, mode})});
  const d = await r.json();
  if (!r.ok) { $('status').textContent = d.error; $('go').disabled = false; return; }
  const timer = setInterval(async () => {
    const j = await (await fetch('/api/job/' + d.job)).json();
    $('status').textContent = j.error ? 'Error: ' + j.error : j.status;
    $('bar').style.width = Math.max(3, j.progress || 0) + '%';
    if (j.done) {
      clearInterval(timer); $('go').disabled = false;
      if (!j.error) showResults(j.files);
    }
  }, 1000);
};

function showResults(files){
  $('results').innerHTML = '';
  files.forEach(f => {
    const w = document.createElement('div');
    w.innerHTML = `<video src="${f.url}" controls preload="metadata"></video><a class="dlbtn" href="${f.url}?dl=1" download="${f.name}">Download ${f.name}</a>`;
    $('results').append(w);
  });
  $('resultCard').classList.remove('hidden');
  $('resultCard').scrollIntoView({behavior: 'smooth'});
}

$('loadBtn').onclick = loadVideo;
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') loadVideo(); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")), debug=False)
