"""B-roll from sources you're allowed to use: your licensed clips folder, Pexels and NASA.

Movie scenes are only used when you've placed a clip you hold rights to (or a public-domain film)
in assets/licensed_clips. The pipeline does not download from YouTube or film sites: re-uploading
someone else's footage gets Content ID claims/strikes and fails YouTube's reused-content rules.
Every clip used is written to credits.json and appended to the video description.
"""
import json
import logging
import shutil
import subprocess
from pathlib import Path

import requests

from .context import ROOT, Run
from . import images

log = logging.getLogger("scifi.broll")


def _licensed(run: Run, query: str) -> tuple[Path, str] | None:
    folder = ROOT / run.cfg["footage"]["licensed_clips_dir"]
    words = {w.lower() for w in query.split()}
    best, score = None, 0
    for clip in folder.glob("*.mp4"):
        s = len(words & set(clip.stem.lower().replace("-", "_").split("_")))
        if s > score:
            best, score = clip, s
    return (best, f"Licensed clip: {best.name}") if best else None


def _download(url: str, out: Path) -> Path:
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return out


def _pexels(run: Run, query: str, out: Path) -> tuple[Path, str] | None:
    r = requests.get(
        "https://api.pexels.com/v1/videos/search",
        headers={"Authorization": run.env("PEXELS_API_KEY")},
        params={"query": query, "orientation": "landscape", "size": "medium", "per_page": 5},
        timeout=30,
    )
    r.raise_for_status()
    for video in r.json().get("videos", []):
        files = [f for f in video["video_files"]
                 if f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= 1280]
        if files:
            best = min(files, key=lambda f: abs(f["width"] - 1920))
            _download(best["link"], out)
            return out, f"Video by {video['user']['name']} on Pexels: {video['url']}"
    return None


def _nasa(run: Run, query: str, out: Path) -> tuple[Path, str] | None:
    r = requests.get("https://images-api.nasa.gov/search",
                     params={"q": query, "media_type": "video"}, timeout=30)
    r.raise_for_status()
    for item in r.json()["collection"]["items"][:5]:
        assets = requests.get(item["href"], timeout=30).json()
        mp4s = [a for a in assets if a.endswith(".mp4")]
        pick = next((a for a in mp4s if "~medium" in a), None) or next(iter(mp4s), None)
        if pick:
            _download(pick.replace("http://", "https://"), out)
            nasa_id = item["data"][0]["nasa_id"]
            return out, f"NASA footage ({nasa_id}): https://images.nasa.gov/details/{nasa_id}"
    return None


SOURCES = {"licensed_clips": None, "pexels": _pexels, "nasa": _nasa}


def fetch_all(run: Run) -> None:
    credits = []
    script = run.script
    for i, scene in enumerate(script["scenes"]):
        if scene["visual"]["type"] != "broll":
            continue
        out = run.dir / "broll" / f"scene_{i:03d}.mp4"
        query = scene["visual"].get("query") or scene["chapter"]
        found = None
        if run.dry_run:
            # Synthetic footage so the b-roll render path is exercised without API keys.
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "mandelbrot=s=1280x720:rate=30", "-t", "6", "-pix_fmt", "yuv420p",
                            str(out)], check=True)
            found = (out, "Dry-run synthetic footage")
        else:
            for name in run.cfg["footage"]["sources"]:
                try:
                    if name == "licensed_clips":
                        hit = _licensed(run, query)
                        if hit:
                            shutil.copy(hit[0], out)
                            found = (out, hit[1])
                    else:
                        found = SOURCES[name](run, query, out)
                except Exception as err:  # noqa: BLE001 - try next source
                    log.warning("%s failed for '%s': %s", name, query, err)
                if found:
                    break
        if found:
            credits.append(found[1])
            log.info("scene %d b-roll: %s", i, found[1])
        else:
            # No footage: turn the scene into an AI image scene instead.
            scene["visual"] = {"type": "ai_image", "prompt": scene["visual"].get("prompt") or
                               f"{query}, {scene['chapter']}", "query": query}
            images.generate_one(run, i, scene["visual"]["prompt"])
    (run.dir / "script.json").write_text(json.dumps(script, indent=2))
    (run.dir / "credits.json").write_text(json.dumps(sorted(set(credits)), indent=2))
