import os, re, uuid, subprocess, asyncio
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
import edge_tts

load_dotenv()
ROOT = Path(__file__).resolve().parent
OUT, UPLOADS = ROOT / "outputs", ROOT / "uploads"
OUT.mkdir(exist_ok=True); UPLOADS.mkdir(exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 800 * 1024 * 1024
ALLOWED = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Preset labels are styles, not cloned people. These map to generally available Burmese neural voices.
VOICE_PRESETS = {
    "kyawgyi": {"label": "ကျော်ကြီး · ယောက်ျားသံ", "voice": "my-MM-ThihaNeural",
                "instructions": "Use a confident, energetic masculine narration style. Speak clearly and naturally in Burmese."},
    "susu": {"label": "စုစု · မိန်းကလေးသံ", "voice": "my-MM-NilarNeural",
             "instructions": "Use a warm, friendly feminine narration style. Speak clearly and naturally in Burmese."},
    "nilar": {"label": "နီလာ · နူးညံ့သံ", "voice": "my-MM-NilarNeural",
              "instructions": "Use a gentle, calm feminine narration style. Speak clearly and naturally in Burmese."},
    "dramatic": {"label": "ဇာတ်လမ်းဆန်ဆန် · Dramatic", "voice": "my-MM-ThihaNeural",
                 "instructions": "Use a dramatic, suspenseful storyteller delivery, with natural pauses. Speak clearly in Burmese."},
    "fast": {"label": "မြန်မြန်တက်ကြွ · Energetic", "voice": "my-MM-ThihaNeural",
             "instructions": "Use an energetic, brisk but intelligible narration style in Burmese."},
}


def get_video_id(url):
    p = urlparse(url.strip())
    host = p.netloc.lower().replace("www.", "")
    if host == "youtu.be":
        return p.path.strip("/").split("/")[0]
    if host.endswith("youtube.com"):
        if p.path == "/watch":
            return parse_qs(p.query).get("v", [""])[0]
        parts = p.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live"):
            return parts[1]
    return None


def get_transcript(url):
    video_id = get_video_id(url)
    if not video_id:
        raise ValueError("မှန်ကန်တဲ့ YouTube URL ထည့်ပါ။")
    try:
        transcript = YouTubeTranscriptApi().fetch(video_id, languages=["my", "en", "en-US"])
    except Exception as e:
        raise RuntimeError("ဒီ Video မှာ ဖတ်ယူနိုင်တဲ့ Captions မရှိနိုင်ပါ။ Captions ဖွင့်ထားတဲ့ Video ကိုရွေးပါ၊ သို့မဟုတ် အသုံးပြုခွင့်ရှိတဲ့ Transcript ကို Paste လုပ်ပါ။") from e
    return " ".join(getattr(item, "text", "") for item in transcript).strip()


def make_script(source_text, minutes, style):
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError(".env ထဲမှာ GEMINI_API_KEY ထည့်ပါ။ Google AI Studio မှာ Free Tier ရရှိမှုကို စစ်ပါ။")
    client = genai.Client(api_key=key)
    prompt = f"""
Write an original Burmese-language movie recap narration from the source transcript.
Target duration: about {minutes} minute(s). Style: {style}.
Use natural spoken Burmese, start with a strong hook, cover important story beats in sequence,
do not invent unsupported details, and do not reproduce long movie dialogue or screenplay text.
Output narration only, without headings, markdown, timestamps or stage directions.
SOURCE TRANSCRIPT:
{source_text[:80000]}
"""
    result = client.models.generate_content(model=MODEL, contents=prompt)
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        raise RuntimeError("AI က Script မထုတ်ပေးနိုင်ခဲ့ပါ။")
    return text


def split_sentences(script):
    parts = re.split(r"(?<=[။!?])\s+|(?<=၊)\s+", script.strip())
    result = []
    for p in parts:
        p = re.sub(r"\s+", " ", p).strip()
        if not p: continue
        if len(p) > 110:
            result.extend(x.strip() for x in re.split(r"(?<=၊)\s*", p) if x.strip())
        else:
            result.append(p)
    return result or [script.strip()]


def run(args):
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError("FFmpeg error: " + (p.stderr or p.stdout or "")[-2500:])
    return p


def srt_time(seconds):
    ms = max(0, int(round(seconds * 1000)))
    h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def build_audio_and_srt(script, job_dir, preset_key):
    preset = VOICE_PRESETS.get(preset_key, VOICE_PRESETS["nilar"])
    chunks_dir = job_dir / "voice_chunks"
    chunks_dir.mkdir(exist_ok=True)
    entries, timings, cursor = [], [], 0.0
    for i, sentence in enumerate(split_sentences(script)):
        chunk = chunks_dir / f"{i:04}.mp3"
        async def save_chunk(text, path):
            await edge_tts.Communicate(text, preset["voice"], rate="+0%", pitch="+0Hz").save(str(path))
        asyncio.run(save_chunk(sentence, chunk))
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=noprint_wrappers=1:nokey=1", str(chunk)],
                               capture_output=True, text=True)
        if probe.returncode:
            raise RuntimeError("ffprobe မတွေ့ပါ။ FFmpeg full package install လုပ်ပါ။")
        duration = float(probe.stdout.strip())
        timings.append((cursor, cursor + duration, sentence)); cursor += duration
        entries.append("file '" + str(chunk.resolve()).replace("'", "'\\''") + "'")
    list_file = job_dir / "audio_list.txt"
    list_file.write_text("\n".join(entries), encoding="utf-8")
    audio_path = job_dir / "voiceover.mp3"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(audio_path)])
    srt_path = job_dir / "subtitles.srt"
    lines = []
    for i, (start, end, sentence) in enumerate(timings, 1):
        lines.extend([str(i), f"{srt_time(start)} --> {srt_time(max(start + .4, end))}", sentence, ""])
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    return audio_path, srt_path, cursor, preset["label"]


def fpath(path):
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")


def render_vertical(source, audio, srt, output, blur_english):
    subs = fpath(srt)
    if blur_english:
        # Blur the bottom ~22% of the source frame before fitting it into the vertical foreground.
        # This is a practical mask for common subtitle placement; it may need tuning per source.
        graph = (
            "[0:v]split=2[base][strip];"
            "[strip]crop=iw:ih*0.23:0:ih*0.77,gblur=sigma=24[blurstrip];"
            "[base][blurstrip]overlay=0:H-h[clean];"
            "[clean]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=28[blur];"
            "[fg]scale=1040:1500:force_original_aspect_ratio=decrease[front];"
            "[blur][front]overlay=(W-w)/2:(H-h)/2,"
            f"subtitles='{subs}':force_style='FontSize=18,Outline=2,Shadow=0,Alignment=2,MarginV=100'[v]"
        )
    else:
        graph = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=28[blur];"
            "[fg]scale=1040:1500:force_original_aspect_ratio=decrease[front];"
            "[blur][front]overlay=(W-w)/2:(H-h)/2,"
            f"subtitles='{subs}':force_style='FontSize=18,Outline=2,Shadow=0,Alignment=2,MarginV=100'[v]"
        )
    run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(source), "-i", str(audio),
         "-filter_complex", graph, "-map", "[v]", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
         "-c:a", "aac", "-b:a", "160k", "-shortest", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(output)])


@app.get("/")
def index():
    return render_template("index.html", presets=VOICE_PRESETS)


@app.post("/api/create")
def create():
    job = uuid.uuid4().hex[:12]
    folder = OUT / job
    folder.mkdir(exist_ok=True)
    source_video = None
    try:
        url = (request.form.get("youtube_url") or "").strip()
        pasted = (request.form.get("transcript") or "").strip()
        try: minutes = int(request.form.get("duration", "3"))
        except ValueError: minutes = 3
        if minutes not in (1, 3, 5, 10): minutes = 3
        style = (request.form.get("style") or "Suspenseful and emotional").strip()
        voice_key = request.form.get("voice", "nilar")
        blur_english = request.form.get("blur_english") == "on"

        transcript = pasted if pasted else get_transcript(url) if url else ""
        if not transcript:
            return jsonify(error="YouTube Link သို့မဟုတ် Transcript ထည့်ပါ။"), 400

        video = request.files.get("video")
        if video and video.filename:
            name = secure_filename(video.filename)
            if Path(name).suffix.lower() not in ALLOWED:
                return jsonify(error="MP4, MOV, MKV, WEBM, M4V ဖိုင်ကိုသာ တင်ပါ။"), 400
            source_video = UPLOADS / f"{job}_{name}"
            video.save(source_video)

        script = make_script(transcript, minutes, style)
        (folder / "recap_script.txt").write_text(script, encoding="utf-8")
        audio, srt, seconds, voice_label = build_audio_and_srt(script, folder, voice_key)
        video_url = None
        if source_video:
            out_video = folder / "shorts_video.mp4"
            render_vertical(source_video, audio, srt, out_video, blur_english)
            video_url = f"/files/{job}/shorts_video.mp4"
            source_video.unlink(missing_ok=True)
        return jsonify(success=True, script=script, voice_label=voice_label,
            script_url=f"/files/{job}/recap_script.txt", audio_url=f"/files/{job}/voiceover.mp3",
            subtitles_url=f"/files/{job}/subtitles.srt", video_url=video_url,
            duration_seconds=round(seconds, 1),
            message=("Video, voice-over နဲ့ subtitle ပြီးပါပြီ။" if video_url else
                     "Script, voice-over နဲ့ subtitle ပြီးပါပြီ။ MP4 ထုတ်ဖို့ ကိုယ်ပိုင်/လိုင်စင်ရှိတဲ့ clip upload လုပ်ပါ။"))
    except Exception as e:
        app.logger.exception("Recap failed")
        return jsonify(error=str(e)), 500


@app.get("/files/<job>/<path:name>")
def files(job, name):
    folder = (OUT / job).resolve()
    file = (folder / name).resolve()
    if folder not in file.parents or not file.is_file():
        return "Not found", 404
    return send_from_directory(folder, name, as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
