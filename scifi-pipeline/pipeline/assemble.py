"""FFmpeg assembly: Ken Burns stills + b-roll + narration + ducked music + captions + chapters."""
import json
import logging
import random
import subprocess
from pathlib import Path

from .context import ROOT, Run
from .voice import duration

log = logging.getLogger("scifi.assemble")
PAD = 0.4      # silence after each scene's narration
FADE = 0.3


def _ff(args: list) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def _ts(sec: float, srt: bool = False) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if srt:
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"
    return f"{int(h)}:{int(m):02d}:{int(s):02d}" if h else f"{int(m)}:{int(s):02d}"


def _scene_clip(run: Run, i: int, scene: dict, dur: float) -> Path:
    v = run.cfg["video"]
    w, h, fps = v["width"], v["height"], v["fps"]
    audio = run.dir / "audio" / f"scene_{i:03d}.mp3"
    out = run.dir / "scenes" / f"scene_{i:03d}.mp4"
    fades = f"fade=t=in:st=0:d={FADE},fade=t=out:st={dur - FADE:.3f}:d={FADE},format=yuv420p"
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(fps),
           "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-t", f"{dur:.3f}"]

    if scene["visual"]["type"] == "broll":
        src = run.dir / "broll" / f"scene_{i:03d}.mp4"
        vf = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
              f"fps={fps},setsar=1,{fades}[v]")
        _ff(["-stream_loop", "-1", "-i", str(src), "-i", str(audio), "-filter_complex",
             f"{vf};[1:a]apad[a]", "-map", "[v]", "-map", "[a]", *enc, str(out)])
    else:
        src = run.dir / "images" / f"scene_{i:03d}.png"
        frames = int(dur * fps) + 1
        rng = random.Random(i)
        # Vary the camera move per scene: slow push-in, pull-out, or a lateral drift.
        move = rng.choice(["in", "out", "pan"])
        zoom = {"in": "min(1+0.12*on/{f},1.12)", "out": "max(1.12-0.12*on/{f},1)",
                "pan": "1.10"}[move].format(f=frames)
        x = "(iw-iw/zoom)*on/{f}".format(f=frames) if move == "pan" else "iw/2-(iw/zoom/2)"
        bw, bh = w * 3 // 2, h * 3 // 2   # upscale first so zoompan doesn't jitter
        vf = (f"[0:v]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
              f"zoompan=z='{zoom}':x='{x}':y='ih/2-(ih/zoom/2)':d={frames}:s={w}x{h}:fps={fps},"
              f"setsar=1,{fades}[v]")
        _ff(["-i", str(src), "-i", str(audio), "-filter_complex", f"{vf};[1:a]apad[a]",
             "-map", "[v]", "-map", "[a]", *enc, str(out)])
    return out


def _captions(script: dict, timeline: list, path: Path) -> None:
    lines, n = [], 1
    for scene, t in zip(script["scenes"], timeline):
        words = scene["narration"].split()
        chunks = [words[k:k + 8] for k in range(0, len(words), 8)]
        speech = t["duration"] - PAD
        start = t["start"]
        for chunk in chunks:
            d = speech * len(chunk) / len(words)
            lines.append(f"{n}\n{_ts(start, True)} --> {_ts(start + d, True)}\n{' '.join(chunk)}\n")
            start += d
            n += 1
    path.write_text("\n".join(lines))


def _chapters(script: dict, timeline: list) -> str:
    out, last = [], None
    for scene, t in zip(script["scenes"], timeline):
        if scene["chapter"] != last:
            if out and t["start"] - out[-1][0] < 10:   # YouTube requires >= 10s per chapter
                continue
            out.append((0.0 if not out else t["start"], scene["chapter"]))
            last = scene["chapter"]
    if len(out) < 3:   # YouTube needs at least 3 timestamps starting at 0:00
        return ""
    return "\n".join(f"{_ts(s)} {name}" for s, name in out)


def build(run: Run) -> dict:
    script = run.script
    timeline, clips, t = [], [], 0.0
    for i, scene in enumerate(script["scenes"]):
        dur = duration(run.dir / "audio" / f"scene_{i:03d}.mp3") + PAD
        clips.append(_scene_clip(run, i, scene, dur))
        timeline.append({"index": i, "start": t, "duration": dur, "file": str(clips[-1]),
                         "type": scene["visual"]["type"]})
        t += dur
        log.info("scene %d/%d rendered", i + 1, len(script["scenes"]))

    concat = run.dir / "scenes" / "concat.txt"
    concat.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    joined = run.dir / "scenes" / "joined.mp4"
    _ff(["-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(joined)])

    srt = run.dir / "final" / "captions.srt"
    _captions(script, timeline, srt)

    final = run.dir / "final" / "video.mp4"
    vol = run.cfg["video"]["music_volume"]
    music = sorted((ROOT / run.cfg["footage"]["music_dir"]).glob("*.mp3"))
    vf = []
    if run.cfg["video"]["burn_subtitles"]:
        vf = ["-vf", f"subtitles={srt}:force_style='FontSize=22,Outline=2'"]
    vcodec = ["-c:v", "libx264", "-preset", "medium", "-crf", "18"] if vf else ["-c:v", "copy"]
    if music:
        track = random.Random(script["title"]).choice(music)
        # Music ducks under the narration (sidechain), then everything is normalised to -14 LUFS.
        fc = (f"[0:a]asplit=2[n1][n2];[1:a]volume={vol}[m];"
              f"[m][n1]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[d];"
              f"[n2][d]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]")
        _ff(["-i", str(joined), "-stream_loop", "-1", "-i", str(track), "-filter_complex", fc,
             "-map", "0:v", "-map", "[a]", *vf, *vcodec, "-c:a", "aac", "-b:a", "192k",
             "-shortest", "-movflags", "+faststart", str(final)])
    else:
        log.warning("no music in %s; exporting narration only", run.cfg["footage"]["music_dir"])
        _ff(["-i", str(joined), "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", *vf, *vcodec,
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)])

    total = duration(final)
    minutes = total / 60
    if minutes < run.cfg["video"]["min_minutes"]:
        log.warning("video is %.1f min, below the %.1f min target", minutes,
                    run.cfg["video"]["min_minutes"])
    result = {"video": str(final), "captions": str(srt), "seconds": total,
              "chapters": _chapters(script, timeline), "timeline": timeline}
    (run.dir / "timeline.json").write_text(json.dumps(result, indent=2))
    log.info("final video %s (%.1f min)", final, minutes)
    return result


def make_short(run: Run, max_seconds: float = 58) -> Path:
    """Vertical 9:16 Short from the opening scenes, cut on a scene boundary."""
    info = json.loads((run.dir / "timeline.json").read_text())
    end = 0.0
    for t in info["timeline"]:
        if t["start"] + t["duration"] > max_seconds:
            break
        end = t["start"] + t["duration"]
    end = end or min(max_seconds, info["seconds"])
    out = run.dir / "final" / "short.mp4"
    # Blur a small copy for the background (much faster than blurring at full size).
    vf = ("split[a][b];[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
          "boxblur=8:2,scale=1080:1920[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p")
    _ff(["-i", info["video"], "-t", f"{end:.2f}", "-vf", vf, "-c:v", "libx264", "-preset",
         "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
         str(out)])
    log.info("short %s (%.0fs)", out, end)
    return out
