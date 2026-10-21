"""Narration per scene with OpenAI text-to-speech."""
import logging
import subprocess

from .context import Run
from .fixer import with_fix

log = logging.getLogger("scifi.voice")

TONE = ("Deep, calm documentary narrator. Measured pace, slight sense of wonder, "
        "clear emphasis on key numbers and names.")


def duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def narrate_all(run: Run) -> None:
    cfg = run.cfg
    for i, scene in enumerate(run.script["scenes"]):
        out = run.dir / "audio" / f"scene_{i:03d}.mp3"
        if out.exists():
            continue
        if run.dry_run:
            secs = len(scene["narration"].split()) / cfg["video"]["words_per_minute"] * 60
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "anullsrc=r=44100:cl=stereo", "-t", f"{secs:.2f}", "-q:a", "9",
                            str(out)], check=True)
            continue
        from openai import OpenAI

        client = OpenAI(api_key=run.env("OPENAI_API_KEY"))

        def speak(text: str):
            with client.audio.speech.with_streaming_response.create(
                model=cfg["models"]["tts"], voice=cfg["models"]["tts_voice"],
                input=text, instructions=TONE,
            ) as resp:
                resp.stream_to_file(out)
            return out

        with_fix(run, f"voice.scene_{i}", speak, scene["narration"])
        log.info("scene %d narration %.1fs", i, duration(out))
