#!/usr/bin/env python3
"""Sci-fi channel pipeline.

  python run.py make [--topic "..." | --script scripts/x.json] [--dry-run] [--minutes 10]
                     [--resume output/<run>]
  python run.py growth           # daily: post comments on newly-public videos, refresh insights
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from pipeline import assemble, broll, images, premiere_xml, script_gen, thumbnail, voice, youtube
from pipeline.context import ROOT, Run, load_config

log = logging.getLogger("scifi")


def make(args, cfg) -> int:
    if args.minutes:
        cfg["video"]["target_minutes"] = args.minutes
        cfg["video"]["min_minutes"] = args.minutes * 0.95
    run_dir = Path(args.resume) if args.resume else (
        ROOT / "output" / datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    run = Run.open(cfg, run_dir, dry_run=args.dry_run)
    log.info("run directory: %s", run.dir)

    stages = [
        ("script", lambda: script_gen.load(run, Path(args.script)) if args.script
         else script_gen.generate(run, args.topic)),
        ("images", lambda: images.generate_all(run)),
        ("broll", lambda: broll.fetch_all(run)),
        ("voice", lambda: voice.narrate_all(run)),
        ("assemble", lambda: assemble.build(run)),
        ("short", lambda: assemble.make_short(run) if cfg["youtube"]["upload_short"] else None),
        ("premiere", lambda: premiere_xml.export(run) if cfg["editing"]["export_premiere_xml"]
         else None),
        ("thumbnail", lambda: thumbnail.make(run)),
    ]
    for name, fn in stages:
        if run.done(name):
            log.info("skip %s (done)", name)
            continue
        log.info("== %s ==", name)
        fn()
        run.mark_done(name)

    if cfg["editing"]["hold_for_manual_edit"] and not args.publish:
        log.info("Holding for manual edit. Open %s in Premiere, export over final/video.mp4, then "
                 "run: python run.py make --resume %s --publish",
                 run.dir / "final" / "premiere_timeline.xml", run.dir)
        return 0
    if args.dry_run or args.no_upload:
        log.info("Not uploading. Outputs are in %s", run.dir / "final")
        return 0
    if not run.done("upload"):
        short = run.dir / "final" / "short.mp4"
        run.manifest["upload"] = youtube.upload(run, short if short.exists() else None)
        run.mark_done("upload")
    log.info("done: %s", run.manifest["upload"])
    return 0


def main() -> int:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--topic")
    m.add_argument("--script", help="use a ready-made script JSON instead of asking ChatGPT")
    m.add_argument("--dry-run", action="store_true", help="no API calls, placeholder media")
    m.add_argument("--no-upload", action="store_true")
    m.add_argument("--publish", action="store_true", help="upload after a manual-edit hold")
    m.add_argument("--minutes", type=float)
    m.add_argument("--resume")
    sub.add_parser("growth")
    args = p.parse_args()
    cfg = load_config(Path(args.config))
    if args.cmd == "make":
        return make(args, cfg)
    youtube.growth(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
