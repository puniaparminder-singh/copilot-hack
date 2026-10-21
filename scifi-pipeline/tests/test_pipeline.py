import copy
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline import assemble, broll, images, premiere_xml, script_gen, thumbnail, voice, youtube
from pipeline.context import ROOT, Run, load_config

CFG = load_config(ROOT / "config.example.yaml")


def test_next_slot_lands_on_configured_weekday_and_hour():
    now = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)   # a Wednesday
    slot = youtube.next_slot(CFG, now)
    assert slot == "2026-10-23T15:00:00Z"                       # Friday 15:00 UTC (weekday 4)


def test_chapters_follow_youtube_rules():
    script = {"scenes": [{"chapter": c} for c in ["Intro", "A", "A", "B", "C"]]}
    timeline = [{"start": s} for s in [0, 15, 40, 70, 120]]
    assert assemble._chapters(script, timeline) == "0:00 Intro\n0:15 A\n1:10 B\n2:00 C"
    assert assemble._chapters({"scenes": script["scenes"][:2]}, timeline[:2]) == ""


def test_fixer_retries_with_repaired_input(tmp_path, monkeypatch):
    from pipeline import fixer

    run = Run.open(copy.deepcopy(CFG), tmp_path)
    monkeypatch.setattr(fixer, "ask_claude", lambda *a: {
        "diagnosis": "trailing comma", "action": "retry_with_input", "repaired_input": '{"a": 1}'})
    assert fixer.with_fix(run, "parse", json.loads, '{"a": 1,}') == {"a": 1}
    assert "trailing comma" in (tmp_path / "fixes.md").read_text()


@pytest.fixture(scope="module")
def dry_run(tmp_path_factory):
    cfg = copy.deepcopy(CFG)
    cfg["video"].update(target_minutes=0.5, min_minutes=0.45)
    music = tmp_path_factory.mktemp("music")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=220:duration=5", str(music / "track.mp3")], check=True)
    cfg["footage"]["music_dir"] = str(music)   # absolute path wins over ROOT join
    run = Run.open(cfg, tmp_path_factory.mktemp("run"), dry_run=True)
    script_gen.generate(run)
    images.generate_all(run)
    broll.fetch_all(run)
    voice.narrate_all(run)
    result = assemble.build(run)
    return run, result


def test_dry_run_renders_full_video(dry_run):
    run, result = dry_run
    assert abs(result["seconds"] - voice.duration(result["video"])) < 0.5
    assert result["seconds"] > 25
    assert any(t["type"] == "broll" for t in result["timeline"])
    assert Path(result["captions"]).read_text().startswith("1\n00:00:00,000 --> ")


def test_short_is_vertical(dry_run):
    run, _ = dry_run
    short = assemble.make_short(run)
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", str(short)],
                           capture_output=True, text=True, check=True).stdout.strip()
    assert probe == "1080,1920"


def test_premiere_xml_and_thumbnail(dry_run):
    run, result = dry_run
    xml = premiere_xml.export(run)
    clips = ET.parse(xml).getroot().findall(".//video/track/clipitem")
    assert len(clips) == len(result["timeline"])
    thumb = thumbnail.make(run)
    assert thumb.stat().st_size < 2 * 1024 * 1024


def test_description_includes_chapters_credits_and_disclosure(dry_run):
    run, result = dry_run
    text = youtube.description(run, "0:00 Intro\n0:15 A\n0:30 B")
    assert "Chapters:" in text and "Footage credits:" in text and "AI" in text
