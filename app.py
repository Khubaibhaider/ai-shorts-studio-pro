import json
import os
import re
import subprocess
import sys
import threading
import uuid
import webbrowser
from pathlib import Path

import requests
from faster_whisper import WhisperModel
from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

# ---------------------------------------------------------------------------
# App, Bundled FFmpeg & PyInstaller Path Configuration
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    # Running inside PyInstaller .exe
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    # Running directly via python app.py
    BUNDLE_DIR = Path(__file__).resolve().parent
    APP_ROOT = BUNDLE_DIR

# Locate bundled bin\ffmpeg.exe (checks both APP_ROOT\bin and BUNDLE_DIR\bin)
BUNDLED_FFMPEG_APP = APP_ROOT / "bin" / "ffmpeg.exe"
BUNDLED_FFMPEG_MEIPASS = BUNDLE_DIR / "bin" / "ffmpeg.exe"

if BUNDLED_FFMPEG_APP.exists():
    FFMPEG_BIN = str(BUNDLED_FFMPEG_APP)
elif BUNDLED_FFMPEG_MEIPASS.exists():
    FFMPEG_BIN = str(BUNDLED_FFMPEG_MEIPASS)
else:
    FFMPEG_BIN = "ffmpeg"

# Writable working folders inside user's AppData or local app directory
WORK_DIR = Path(os.environ.get("LOCALAPPDATA", APP_ROOT)) / "AIShortsStudioPro"
UPLOAD_DIR = WORK_DIR / "uploads"
OUTPUT_DIR = WORK_DIR / "outputs"
TEMP_DIR = WORK_DIR / "temp"

for folder in (UPLOAD_DIR, OUTPUT_DIR, TEMP_DIR):
    folder.mkdir(parents=True, exist_ok=True)

TEMPLATE_DIR = BUNDLE_DIR / "templates"
STATIC_DIR = BUNDLE_DIR / "static"

app = Flask(
    __name__,
    template_folder=str(TEMPLATE_DIR),
    static_folder=str(STATIC_DIR) if STATIC_DIR.exists() else None,
)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024  # 10 GB max upload

# In-memory job state for live UI progress & console logs
JOBS = {}
WHISPER_CACHE = {}


# ---------------------------------------------------------------------------
# Hardware Detection & Helpers
# ---------------------------------------------------------------------------
def has_nvidia_gpu() -> bool:
    """Detect whether CUDA / NVENC is available on the user's Windows PC."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return True
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["nvidia-smi"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return res.returncode == 0
    except Exception:
        return False


def get_whisper_model(hardware_mode: str):
    """Load and cache faster-whisper on GPU or laptop CPU + RAM."""
    use_gpu = (hardware_mode == "gpu") and has_nvidia_gpu()
    device = "cuda" if use_gpu else "cpu"
    compute_type = "float16" if use_gpu else "int8"
    cache_key = f"small_{device}_{compute_type}"

    if cache_key not in WHISPER_CACHE:
        WHISPER_CACHE[cache_key] = WhisperModel(
            "small",
            device=device,
            compute_type=compute_type,
        )
    return WHISPER_CACHE[cache_key], device


def log_job(job_id: str, message: str, progress: int = None, status: str = None):
    """Append a timestamped log line and update progress percentage."""
    job = JOBS.get(job_id)
    if not job:
        return
    job["logs"].append(message)
    if progress is not None:
        job["progress"] = progress
    if status is not None:
        job["status"] = status


# ---------------------------------------------------------------------------
# Built-in 100% Local Viral Moment Engine (Zero API Keys / Zero LM Studio)
# ---------------------------------------------------------------------------
def find_viral_moments_local(segments, num_clips=3, min_sec=25, max_sec=58):
    """
    100% offline viral clip finder using Whisper timestamped segments.
    Scores speaking pace (WPM), hook triggers, emotional punctuation, and opening punch.
    """
    hook_words = {
        "why", "how", "what", "secret", "never", "insane", "crazy", "wait",
        "stop", "mistake", "truth", "finally", "bro", "clutch", "impossible",
        "watch", "look", "seriously", "literally", "best", "worst", "hack",
        "warning", "nobody", "everyone", "money", "cheat", "legend", "wtf",
        "no", "way", "listen", "remember", "imagine", "real", "actually"
    }

    candidates = []
    n = len(segments)

    for i in range(n):
        start_time = segments[i]["start"]
        combined_text = []
        end_time = start_time

        for j in range(i, n):
            seg_end = segments[j]["end"]
            duration = seg_end - start_time
            if duration > max_sec:
                break

            combined_text.append(segments[j]["text"].strip())
            end_time = seg_end

            if duration >= min_sec:
                full_text = " ".join(combined_text)
                words = re.findall(r"\w+", full_text.lower())
                if not words:
                    continue

                wpm = (len(words) / duration) * 60.0
                hook_hits = sum(1 for w in words if w in hook_words)
                punctuation_boost = (full_text.count("?") * 3.0) + (full_text.count("!") * 2.5)

                opening = combined_text[0].lower()
                opening_boost = (
                    5.0
                    if ("?" in opening or any(h in opening for h in hook_words))
                    else 0.0
                )

                score = (wpm * 0.15) + (hook_hits * 2.5) + punctuation_boost + opening_boost
                clean_title = re.sub(r"[^\w\s!?'-]", "", combined_text[0]).strip()[:55]
                candidates.append(
                    {
                        "start": round(start_time, 2),
                        "end": round(end_time, 2),
                        "score": score,
                        "title": clean_title or f"Viral Clip #{len(candidates) + 1}",
                    }
                )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected = []
    for cand in candidates:
        overlaps = any(
            not (cand["end"] <= s["start"] + 3.0 or cand["start"] >= s["end"] - 3.0)
            for s in selected
        )
        if not overlaps:
            selected.append(cand)
        if len(selected) >= num_clips:
            break

    # Fallback if video is shorter than min_sec or has sparse speech
    if not selected and segments:
        total_start = segments[0]["start"]
        total_end = min(segments[-1]["end"], total_start + max_sec)
        selected.append(
            {
                "start": round(total_start, 2),
                "end": round(max(total_end, total_start + 5.0), 2),
                "score": 10.0,
                "title": "Highlight Clip",
            }
        )

    return sorted(selected, key=lambda x: x["start"])


def find_viral_moments_llm(segments, engine, api_key, num_clips=3, min_sec=25, max_sec=58):
    """Optional LLM viral moment detection for LM Studio, OpenAI, Groq, or Gemini."""
    transcript_lines = [
        f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text'].strip()}"
        for s in segments
    ]
    transcript_block = "\n".join(transcript_lines[:350])

    prompt = (
        f"You are a viral short-form video editor. Pick the {num_clips} most engaging, "
        f"high-retention moments ({min_sec} to {max_sec} seconds each) from this transcript.\n"
        f"Return ONLY valid JSON as a list of objects with keys: "
        f'"start" (number), "end" (number), "title" (short hook title).\n\n'
        f"Transcript:\n{transcript_block}"
    )

    if engine == "lm_studio":
        url = "http://localhost:1234/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": "local-model",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=45)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    elif engine == "openai":
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=45)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    elif engine == "groq":
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=45)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    elif engine == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        headers = {"Content-Type": "application/json"}
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        resp = requests.post(url, headers=headers, json=payload, timeout=45)
        resp.raise_for_status()
        content = resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    else:
        return find_viral_moments_local(segments, num_clips, min_sec, max_sec)

    match = re.search(r"\[.*\]", content, re.DOTALL)
    if not match:
        raise ValueError("AI engine did not return valid JSON clip timestamps.")

    clips = json.loads(match.group(0))
    cleaned = []
    for c in clips[:num_clips]:
        cleaned.append(
            {
                "start": float(c["start"]),
                "end": float(c["end"]),
                "title": str(c.get("title", "Viral Short"))[:55],
            }
        )
    return cleaned


# ---------------------------------------------------------------------------
# Subtitle (.SRT) & FFmpeg Video Rendering Helpers
# ---------------------------------------------------------------------------
def format_srt_time(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    return f"{hrs:02d}:{mins:02d}:{secs:02d},{millis:03d}"


def build_clip_srt(segments, clip_start: float, clip_end: float, srt_path: Path):
    """Create a relative-timestamped SRT file for burned-in Hormozi-style captions."""
    entries = []
    idx = 1
    for seg in segments:
        if seg["end"] < clip_start or seg["start"] > clip_end:
            continue
        rel_start = max(0.0, seg["start"] - clip_start)
        rel_end = max(rel_start + 0.3, min(clip_end - clip_start, seg["end"] - clip_start))
        text = seg["text"].strip().upper()
        if not text:
            continue
        entries.append(
            f"{idx}\n{format_srt_time(rel_start)} --> {format_srt_time(rel_end)}\n{text}\n"
        )
        idx += 1

    srt_path.write_text("\n".join(entries), encoding="utf-8")


def render_vertical_short(
    input_video: Path,
    output_video: Path,
    start_sec: float,
    end_sec: float,
    srt_path: Path,
    font_name: str,
    subtitle_color: str,
    framing: str,
    use_gpu: bool,
):
    """Crop to 9:16 vertical (1080x1920), burn styled captions, and encode with GPU or CPU."""
    duration = max(1.0, end_sec - start_sec)

    color_map = {
        "yellow": "&H0000FFFF",  # ASS BGR format for bright yellow
        "white": "&H00FFFFFF",
        "cyan": "&H00FFFF00",
        "green": "&H0000FF66",
    }
    primary_color = color_map.get(subtitle_color.lower(), "&H0000FFFF")

    escaped_srt = str(srt_path).replace("\\", "/").replace(":", "\\:")

    if framing == "blur_bg":
        video_filter = (
            f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:10[bg];"
            f"[0:v]scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
            f"subtitles='{escaped_srt}':force_style='FontName={font_name},FontSize=18,PrimaryColour={primary_color},"
            f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=65'[v]"
        )
        map_args = ["-map", "[v]", "-map", "0:a?"]
    else:
        video_filter = (
            f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920,"
            f"subtitles='{escaped_srt}':force_style='FontName={font_name},FontSize=18,PrimaryColour={primary_color},"
            f"OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=65'"
        )
        map_args = ["-vf", video_filter]

    video_codec = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"] if use_gpu else [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23"
    ]

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-ss", f"{start_sec:.2f}",
        "-i", str(input_video),
        "-t", f"{duration:.2f}",
        *map_args,
        *video_codec,
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_video),
    ]

    subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


# ---------------------------------------------------------------------------
# Background Processing Pipeline
# ---------------------------------------------------------------------------
def process_video_job(job_id: str, video_path: Path, options: dict):
    try:
        hardware = options.get("hardware", "cpu")
        ai_engine = options.get("ai_engine", "local_builtin")
        api_key = options.get("api_key", "").strip()
        num_clips = int(options.get("num_clips", 3))
        clip_length = options.get("clip_length", "smart")
        font_name = options.get("font_name", "Arial Black")
        subtitle_color = options.get("subtitle_color", "yellow")
        framing = options.get("framing", "center_crop")

        if clip_length == "short":
            min_sec, max_sec = 15, 30
        elif clip_length == "long":
            min_sec, max_sec = 45, 59
        else:
            min_sec, max_sec = 25, 58

        log_job(job_id, "🚀 Job started...", progress=5, status="running")
        log_job(job_id, f"📁 Loaded local file: {video_path.name}", progress=10)

        # 1. Extract 16kHz mono WAV audio via bundled bin\ffmpeg.exe
        wav_path = TEMP_DIR / f"{job_id}.wav"
        log_job(job_id, "🔊 Extracting audio track via FFmpeg...", progress=18)
        subprocess.run(
            [
                FFMPEG_BIN, "-y", "-i", str(video_path),
                "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                str(wav_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

        # 2. Transcribe locally with Faster-Whisper
        model, active_device = get_whisper_model(hardware)
        device_label = "NVIDIA GPU (CUDA)" if active_device == "cuda" else "System CPU + RAM"
        log_job(job_id, f"🧠 Loading Whisper (small / int8) on {device_label}...", progress=30)

        raw_segments, _ = model.transcribe(str(wav_path), vad_filter=True)
        segments = [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in raw_segments
        ]
        word_count = sum(len(s["text"].split()) for s in segments)
        log_job(job_id, f"✅ Transcription complete ({word_count} words detected).", progress=55)

        # 3. Find viral moments (Built-in Local AI by default — zero API keys needed)
        if ai_engine == "local_builtin":
            log_job(job_id, "⚡ Running Built-in Local AI Viral Hunt (100% offline)...", progress=65)
            clips = find_viral_moments_local(segments, num_clips, min_sec, max_sec)
        else:
            log_job(job_id, f"🧠 Sending transcript to {ai_engine} to find viral moments...", progress=65)
            clips = find_viral_moments_llm(segments, ai_engine, api_key, num_clips, min_sec, max_sec)

        log_job(job_id, f"🎯 Found {len(clips)} high-retention viral moments!", progress=75)

        # 4. Render each 9:16 vertical Short with burned-in captions
        use_gpu_encode = (active_device == "cuda")
        rendered_files = []

        for idx, clip in enumerate(clips, start=1):
            pct = 75 + int((idx / max(1, len(clips))) * 22)
            log_job(
                job_id,
                f"🎬 Rendering Short #{idx}/{len(clips)} ({clip['start']}s - {clip['end']}s)...",
                progress=pct,
            )
            srt_file = TEMP_DIR / f"{job_id}_clip_{idx}.srt"
            out_name = f"Short_{idx}_{job_id[:6]}.mp4"
            out_file = OUTPUT_DIR / out_name

            build_clip_srt(segments, clip["start"], clip["end"], srt_file)
            render_vertical_short(
                input_video=video_path,
                output_video=out_file,
                start_sec=clip["start"],
                end_sec=clip["end"],
                srt_path=srt_file,
                font_name=font_name,
                subtitle_color=subtitle_color,
                framing=framing,
                use_gpu=use_gpu_encode,
            )
            rendered_files.append(
                {
                    "index": idx,
                    "title": clip["title"],
                    "start": clip["start"],
                    "end": clip["end"],
                    "filename": out_name,
                    "download_url": f"/outputs/{out_name}",
                }
            )

        if wav_path.exists():
            wav_path.unlink(missing_ok=True)

        JOBS[job_id]["clips"] = rendered_files
        log_job(job_id, "🎉 All viral Shorts rendered and ready!", progress=100, status="completed")

    except Exception as exc:
        log_job(job_id, f"❌ Error: {str(exc)}", progress=0, status="error")


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", gpu_available=has_nvidia_gpu())


@app.route("/api/generate", methods=["POST"])
def generate_shorts():
    if "video" not in request.files:
        return jsonify({"error": "No video file uploaded."}), 400

    file = request.files["video"]
    if not file.filename:
        return jsonify({"error": "Empty filename."}), 400

    job_id = uuid.uuid4().hex
    safe_name = f"{job_id[:8]}_{secure_filename(file.filename)}"
    saved_path = UPLOAD_DIR / safe_name
    file.save(str(saved_path))

    options = {
        "hardware": request.form.get("hardware", "cpu"),
        "ai_engine": request.form.get("ai_engine", "local_builtin"),
        "api_key": request.form.get("api_key", ""),
        "num_clips": request.form.get("num_clips", 3),
        "clip_length": request.form.get("clip_length", "smart"),
        "font_name": request.form.get("font_name", "Arial Black"),
        "subtitle_color": request.form.get("subtitle_color", "yellow"),
        "framing": request.form.get("framing", "center_crop"),
    }

    JOBS[job_id] = {
        "status": "queued",
        "progress": 0,
        "logs": [],
        "clips": [],
    }

    thread = threading.Thread(
        target=process_video_job,
        args=(job_id, saved_path, options),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/outputs/<path:filename>")
def download_output(filename):
    return send_from_directory(str(OUTPUT_DIR), filename, as_attachment=False)


if __name__ == "__main__":
    port = 5000
    threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="127.0.0.1", port=port, debug=False)