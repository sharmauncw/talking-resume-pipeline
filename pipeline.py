#!/usr/bin/env python3
"""Talking-resume pipeline.

Turns a structured resume (YAML) into a narrated video:

    resume.yaml -> narration script -> neural TTS -> video assembly
                   (presenter photo + burned-in subtitles) -> talking_resume.mp4

Usage:
    pip install -r requirements.txt
    python pipeline.py --resume resume.yaml --photo me.jpg --out talking_resume.mp4
    python pipeline.py --resume resume.yaml --dry-run  # no network, no video

Stages that need third-party packages fail with a clear message instead of a
traceback when the package is missing:
    * narration synthesis needs `edge-tts` (and network access at runtime)
    * video assembly needs `moviepy` (and an ffmpeg binary on PATH)

The bundled resume.yaml holds fictional SAMPLE data for "Jordan Lee".
Replace it with your own details for a real video.
"""
import argparse
import asyncio
import os
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("ERROR: pyyaml is required: pip install -r requirements.txt\n")
    sys.exit(2)

try:
    import edge_tts
except ImportError:
    edge_tts = None

try:  # moviepy 2.x import style, fall back to 1.x
    from moviepy import AudioFileClip, ImageSequenceClip
except ImportError:
    try:
        from moviepy.editor import AudioFileClip, ImageSequenceClip
    except ImportError:
        AudioFileClip = ImageSequenceClip = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None

VIDEO_W, VIDEO_H = 1280, 720
BG_COLOR = (11, 22, 34)
ACCENT = (45, 212, 191)
WHITE = (245, 245, 245)
MUTED = (160, 175, 190)


# ---------------------------------------------------------------------------
# Stage 1: load + validate the resume
# ---------------------------------------------------------------------------
REQUIRED_TOP = ["name", "title", "summary", "experience", "skills"]


def load_resume(path):
    """Load resume.yaml and validate its schema. Returns the parsed dict."""
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("resume.yaml must contain a mapping at the top level")
    missing = [k for k in REQUIRED_TOP if not data.get(k)]
    if missing:
        raise ValueError(f"resume.yaml is missing required fields: {missing}")
    for i, job in enumerate(data["experience"]):
        for key in ("role", "org", "period", "bullets"):
            if not job.get(key):
                raise ValueError(f"experience[{i}] is missing '{key}'")
    return data


# ---------------------------------------------------------------------------
# Stage 2: narration script + subtitle segments
# ---------------------------------------------------------------------------
_ABBR = ["B.S.", "M.S.", "Ph.D.", "B.A.", "M.A.", "M.B.A.", "U.S.",
         "e.g.", "i.e.", "Dr.", "Mr.", "Mrs.", "Ms.", "St.", "vs."]


def _sentences(text):
    # protect abbreviations so "B.S." doesn't split a sentence in two
    protected, tokens = text, {}
    for i, abbr in enumerate(_ABBR):
        key = f"\x00{i}\x00"
        if abbr in protected:
            tokens[key] = abbr
            protected = protected.replace(abbr, key)
    parts = re.split(r"(?<=[.!?])\s+", protected.strip())
    out = []
    for part in parts:
        for key, abbr in tokens.items():
            part = part.replace(key, abbr)
        part = part.strip()
        if part:
            out.append(part)
    return out


def chunk_segments(sentences, max_chars=140, max_sentences=2):
    """Group sentences into subtitle-sized segments."""
    segments, current = [], []
    for sent in sentences:
        trial = current + [sent]
        if (len(trial) > max_sentences
                or sum(len(s) for s in trial) > max_chars) and current:
            segments.append(" ".join(current))
            current = [sent]
        else:
            current = trial
    if current:
        segments.append(" ".join(current))
    return segments


def build_script(data):
    """Build the full narration text and subtitle segments from resume data.

    Returns (narration_text, segments).
    """
    name = data["name"]
    paras = [
        f"Hi, I'm {name}, {data['title']} based in "
        f"{data.get('location', 'the United States')}.",
        data["summary"],
    ]
    for job in data["experience"]:
        paras.append(
            f"As {job['role']} at {job['org']}, {job['period']}, "
            + " ".join(job["bullets"])
        )
    paras.append("My core skills include " + ", ".join(data["skills"]) + ".")
    for edu in data.get("education", []):
        period = f", {edu['period']}" if edu.get("period") else ""
        paras.append(f"I hold a {edu['degree']} from {edu['school']}{period}.")
    paras.append(f"Thanks for watching — you can reach me at {data['email']}."
                 if data.get("email") else "Thanks for watching.")

    sentences = []
    for para in paras:
        sentences.extend(_sentences(para))
    segments = chunk_segments(sentences)
    narration = " ".join(sentences)
    return narration, segments


# ---------------------------------------------------------------------------
# Stage 3: neural TTS narration (needs network)
# ---------------------------------------------------------------------------
async def _edge_speak(text, voice, out_path):
    communicator = edge_tts.Communicate(text, voice)
    await communicator.save(out_path)


def synthesize_narration(text, out_mp3, voice="en-US-AriaNeural"):
    """Synthesize narration.mp3 with edge-tts. Needs network access."""
    if edge_tts is None:
        sys.stderr.write(
            "ERROR: the 'edge-tts' package is not installed.\n"
            "Install it with: pip install -r requirements.txt\n"
        )
        sys.exit(2)
    asyncio.run(_edge_speak(text, voice, out_mp3))
    return out_mp3


# ---------------------------------------------------------------------------
# Stage 4: video assembly (presenter photo + burned-in subtitles)
# ---------------------------------------------------------------------------
def _resolve_font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()  # last resort: small but works


def _wrap(draw, text, font, max_width):
    words, lines, line = text.split(), [], ""
    for word in words:
        trial = (line + " " + word).strip()
        if draw.textlength(trial, font=font) <= max_width:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def allocate_timings(segments, total_seconds):
    """Split total_seconds across segments proportional to word count."""
    counts = [max(1, len(seg.split())) for seg in segments]
    total = sum(counts)
    timings, cursor = [], 0.0
    for seg, count in zip(segments, counts):
        dur = total_seconds * count / total
        timings.append((cursor, cursor + dur, seg))
        cursor += dur
    # fix float drift on the last segment
    start, _, seg = timings[-1]
    timings[-1] = (start, total_seconds, seg)
    return timings


def render_frame(photo_path, name, title, subtitle, fonts):
    """Render one 1280x720 frame with PIL. Returns a numpy array."""
    import numpy as np

    img = Image.new("RGB", (VIDEO_W, VIDEO_H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    y = 60
    if photo_path and os.path.exists(photo_path):
        photo = Image.open(photo_path).convert("RGB")
        photo.thumbnail((380, 380))
        img.paste(photo, ((VIDEO_W - photo.width) // 2, y))
        y += photo.height + 30
    else:
        # title-card fallback when no presenter photo is supplied
        draw.text((VIDEO_W // 2, y + 40), name, font=fonts["title"],
                  fill=WHITE, anchor="mm")
        draw.text((VIDEO_W // 2, y + 100), title, font=fonts["heading"],
                  fill=ACCENT, anchor="mm")
        y += 190

    # subtitle block near the bottom
    lines = _wrap(draw, subtitle, fonts["body"], VIDEO_W - 160)
    line_h = fonts["body"].size + 8
    block_h = line_h * len(lines) + 24
    top = VIDEO_H - block_h - 50
    draw.rectangle([60, top - 12, VIDEO_W - 60, top + block_h],
                   fill=(20, 34, 50))
    for i, line in enumerate(lines):
        draw.text((VIDEO_W // 2, top + i * line_h), line,
                  font=fonts["body"], fill=WHITE, anchor="ma")
    return np.asarray(img)


def build_video(photo_path, audio_path, timings, name, title, out_mp4,
                fps=24):
    """Assemble the final mp4: timed subtitle frames + narration audio."""
    if Image is None or ImageSequenceClip is None:
        sys.stderr.write(
            "ERROR: video assembly needs 'pillow' and 'moviepy'.\n"
            "Install them with: pip install -r requirements.txt\n"
            "moviepy also needs an ffmpeg binary on PATH.\n"
        )
        sys.exit(2)

    fonts = {
        "title": _resolve_font(64),
        "heading": _resolve_font(40),
        "body": _resolve_font(34),
    }
    frames = [render_frame(photo_path, name, title, seg, fonts)
              for _, _, seg in timings]
    durations = [end - start for start, end, _ in timings]

    clip = ImageSequenceClip(frames, durations=durations)
    audio = AudioFileClip(audio_path)
    if hasattr(clip, "set_audio"):  # moviepy 1.x
        clip = clip.set_audio(audio)
    else:  # moviepy 2.x
        clip = clip.with_audio(audio)
    clip.write_videofile(out_mp4, fps=fps, codec="libx264",
                         audio_codec="aac", logger=None)
    return out_mp4


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Turn a structured resume (YAML) into a narrated video.")
    parser.add_argument("--resume", default="resume.yaml",
                        help="Path to the resume YAML file")
    parser.add_argument("--photo", default=None,
                        help="Presenter photo (JPG/PNG). Optional: a title "
                             "card is rendered when omitted.")
    parser.add_argument("--out", default="talking_resume.mp4",
                        help="Output video path")
    parser.add_argument("--voice", default="en-US-AriaNeural",
                        help="edge-tts voice id")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the narration script and subtitle "
                             "segments without TTS or video")
    args = parser.parse_args(argv)

    try:
        data = load_resume(args.resume)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 1

    narration, segments = build_script(data)

    if args.dry_run:
        print("=== NARRATION SCRIPT ===")
        print(narration)
        print("\n=== SUBTITLE SEGMENTS ===")
        for i, seg in enumerate(segments, 1):
            print(f"[{i}] {seg}")
        return 0

    workdir = os.path.dirname(os.path.abspath(args.out)) or "."
    audio_path = os.path.join(workdir, "narration.mp3")
    print("Synthesizing narration (needs network for edge-tts)...")
    synthesize_narration(narration, audio_path, args.voice)

    audio = AudioFileClip(audio_path)
    total = audio.duration
    timings = allocate_timings(segments, total)
    print(f"Narration is {total:.1f}s; assembling video...")
    build_video(args.photo, audio_path, timings,
                data["name"], data["title"], args.out)
    print(f"Done: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
