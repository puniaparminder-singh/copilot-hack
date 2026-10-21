"""Run configuration, paths and the resumable manifest shared by every stage."""
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("scifi")


def load_config(path: Path) -> dict:
    if not path.exists():
        path = ROOT / "config.example.yaml"
        log.warning("config.yaml not found, using config.example.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


@dataclass
class Run:
    """One video's working directory. manifest.json records finished stages so a run can resume."""
    cfg: dict
    dir: Path
    dry_run: bool = False
    manifest: dict = field(default_factory=dict)

    @classmethod
    def open(cls, cfg: dict, run_dir: Path, dry_run: bool = False) -> "Run":
        run_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("images", "broll", "audio", "scenes", "final"):
            (run_dir / sub).mkdir(exist_ok=True)
        run = cls(cfg=cfg, dir=run_dir, dry_run=dry_run)
        mpath = run_dir / "manifest.json"
        if mpath.exists():
            run.manifest = json.loads(mpath.read_text())
        return run

    def save(self) -> None:
        (self.dir / "manifest.json").write_text(json.dumps(self.manifest, indent=2))

    def done(self, stage: str) -> bool:
        return stage in self.manifest.get("completed", [])

    def mark_done(self, stage: str) -> None:
        self.manifest.setdefault("completed", []).append(stage)
        self.save()

    @property
    def script(self) -> dict:
        return json.loads((self.dir / "script.json").read_text())

    def env(self, key: str) -> str:
        val = os.environ.get(key, "")
        if not val and not self.dry_run:
            raise RuntimeError(f"Missing environment variable {key} (see .env.example)")
        return val
