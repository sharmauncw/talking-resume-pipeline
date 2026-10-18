# Talking Resume Pipeline

Turn a structured resume (`resume.yaml`) into a narrated video — the "talking resume" trend from Instagram, as a reproducible Python pipeline. No paid APIs: neural TTS via `edge-tts`, video assembly via `moviepy`, subtitles rendered with PIL (no ImageMagick needed).

## Pipeline

```
resume.yaml
    │  load_resume()          validate schema
    ▼
narration script              build_script() → sentences → subtitle segments
    │  synthesize_narration()  edge-tts  (needs network at runtime)
    ▼
narration.mp3 ──┬──► build_video()        moviepy + PIL
presenter photo ┘         │  timed subtitle frames + audio
(optional)                ▼
                   talking_resume.mp4
```

## Setup

```bash
pip install -r requirements.txt
# moviepy also needs ffmpeg on PATH (https://ffmpeg.org)
```

## Usage

```bash
# Full run: narration + video (needs network for edge-tts)
python pipeline.py --resume resume.yaml --photo me.jpg --out talking_resume.mp4

# No photo? A title card with your name + headline is rendered instead.
python pipeline.py --resume resume.yaml --out talking_resume.mp4

# Dry run: print the narration script and subtitle segments, no network/video
python pipeline.py --resume resume.yaml --dry-run

# Pick a different neural voice (see edge-tts --list-voices)
python pipeline.py --resume resume.yaml --voice en-US-GuyNeural --out talking_resume.mp4
```

## How the GitHub Action rebuilds the video

`.github/workflows/rebuild.yml` triggers on every push that touches `resume.yaml` (or `pipeline.py`) and on manual dispatch. It installs Python 3.12 + ffmpeg + the requirements, runs the pipeline without a presenter photo (title-card fallback), and uploads `talking_resume.mp4` as a workflow artifact. Update your resume → push → download the fresh video.

## Customizing

- Edit `resume.yaml` — the schema is documented in comments at the top of the file. The bundled file is **fictional sample data for "Jordan Lee"**; replace it with your own details.
- Subtitle timing is allocated proportionally to word count (`allocate_timings`).
- Frame layout lives in `render_frame()` — tweak colors, fonts, and layout there.

## Runtime caveats

- `edge-tts` calls Microsoft's neural-voice service: **network access is required** when synthesizing narration. `--dry-run` works fully offline.
- `moviepy` needs an `ffmpeg` binary on PATH.
- Subtitles are drawn with PIL using DejaVu Sans when present, else PIL's built-in bitmap font.
