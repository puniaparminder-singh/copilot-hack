"""Write an FCP7 XML timeline that Premiere Pro opens via File > Import.

The timeline references the raw assets (stills, b-roll, narration) rather than the rendered
scenes, so you can re-time cuts, swap shots, add effects and colour in Premiere.
"""
import json
from pathlib import Path
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, ElementTree

from .context import Run


def _rate(parent, fps):
    r = SubElement(parent, "rate")
    SubElement(r, "timebase").text = str(fps)
    SubElement(r, "ntsc").text = "FALSE"


def _file(parent, fid, path: Path, frames, fps, media):
    f = SubElement(parent, "file", id=fid)
    SubElement(f, "name").text = path.name
    SubElement(f, "pathurl").text = "file://localhost" + quote(str(path.resolve()))
    _rate(f, fps)
    SubElement(f, "duration").text = str(frames)
    m = SubElement(f, "media")
    SubElement(m, media)


def _clip(track, cid, name, start, length, fps, fid, path, media):
    c = SubElement(track, "clipitem", id=cid)
    SubElement(c, "name").text = name
    SubElement(c, "duration").text = str(length)
    _rate(c, fps)
    SubElement(c, "start").text = str(start)
    SubElement(c, "end").text = str(start + length)
    SubElement(c, "in").text = "0"
    SubElement(c, "out").text = str(length)
    _file(c, fid, path, length, fps, media)


def export(run: Run) -> Path:
    v = run.cfg["video"]
    fps = v["fps"]
    info = json.loads((run.dir / "timeline.json").read_text())
    total = int(info["seconds"] * fps)

    root = Element("xmeml", version="4")
    seq = SubElement(root, "sequence", id="seq1")
    SubElement(seq, "name").text = run.script["title"]
    SubElement(seq, "duration").text = str(total)
    _rate(seq, fps)
    media = SubElement(seq, "media")
    video = SubElement(media, "video")
    fmt = SubElement(SubElement(video, "format"), "samplecharacteristics")
    SubElement(fmt, "width").text = str(v["width"])
    SubElement(fmt, "height").text = str(v["height"])
    v1 = SubElement(video, "track")
    a1 = SubElement(SubElement(media, "audio"), "track")

    for t in info["timeline"]:
        i = t["index"]
        start, length = int(t["start"] * fps), int(t["duration"] * fps)
        if t["type"] == "broll":
            src = run.dir / "broll" / f"scene_{i:03d}.mp4"
        else:
            src = run.dir / "images" / f"scene_{i:03d}.png"
        _clip(v1, f"v{i}", src.name, start, length, fps, f"fv{i}", src, "video")
        narration = run.dir / "audio" / f"scene_{i:03d}.mp3"
        _clip(a1, f"a{i}", narration.name, start, length, fps, f"fa{i}", narration, "audio")

    out = run.dir / "final" / "premiere_timeline.xml"
    with open(out, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        ElementTree(root).write(f, encoding="utf-8")
    return out
