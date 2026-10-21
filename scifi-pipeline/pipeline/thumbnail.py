"""Thumbnail: AI background + headline, laid out by a Canva brand template (or locally)."""
import base64
import json
import logging
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .context import ROOT, Run
from . import images

log = logging.getLogger("scifi.thumbnail")
CANVA = "https://api.canva.com/rest/v1"
TOKEN_FILE = ROOT / "output" / ".canva_token.json"


def _canva_token(run: Run) -> str:
    refresh = (json.loads(TOKEN_FILE.read_text())["refresh_token"] if TOKEN_FILE.exists()
               else run.env("CANVA_REFRESH_TOKEN"))
    r = requests.post(f"{CANVA}/oauth/token",
                      auth=(run.env("CANVA_CLIENT_ID"), run.env("CANVA_CLIENT_SECRET")),
                      data={"grant_type": "refresh_token", "refresh_token": refresh}, timeout=30)
    r.raise_for_status()
    tok = r.json()
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"refresh_token": tok["refresh_token"]}))  # single-use
    return tok["access_token"]


def _poll(url: str, headers: dict, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = requests.get(url, headers=headers, timeout=30).json()["job"]
        if job["status"] == "success":
            return job
        if job["status"] == "failed":
            raise RuntimeError(f"Canva job failed: {job.get('error')}")
        time.sleep(2)
    raise TimeoutError(url)


def _canva(run: Run, background: Path, headline: str, out: Path) -> Path:
    tcfg = run.cfg["thumbnail"]
    h = {"Authorization": f"Bearer {_canva_token(run)}"}

    meta = json.dumps({"name_base64": base64.b64encode(b"thumbnail-bg").decode()})
    r = requests.post(f"{CANVA}/asset-uploads", data=background.read_bytes(), timeout=60,
                      headers={**h, "Content-Type": "application/octet-stream",
                               "Asset-Upload-Metadata": meta})
    r.raise_for_status()
    asset = _poll(f"{CANVA}/asset-uploads/{r.json()['job']['id']}", h)["asset"]["id"]

    r = requests.post(f"{CANVA}/autofills", headers=h, timeout=30, json={
        "brand_template_id": tcfg["canva_brand_template_id"],
        "title": f"Thumbnail - {run.script['title'][:40]}",
        "data": {tcfg["canva_text_field"]: {"type": "text", "text": headline},
                 tcfg["canva_image_field"]: {"type": "image", "asset_id": asset}},
    })
    r.raise_for_status()
    design = _poll(f"{CANVA}/autofills/{r.json()['job']['id']}", h)["result"]["design"]
    log.info("Canva design (edit it there if you like): %s", design.get("url"))

    r = requests.post(f"{CANVA}/exports", headers=h, timeout=30,
                      json={"design_id": design["id"], "format": {"type": "jpg", "quality": 90}})
    r.raise_for_status()
    url = _poll(f"{CANVA}/exports/{r.json()['job']['id']}", h)["urls"][0]
    out.write_bytes(requests.get(url, timeout=60).content)
    return out


def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "Arial Bold.ttf", "Impact.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _local(background: Path, headline: str, out: Path) -> Path:
    img = ImageOps.fit(Image.open(background).convert("RGB"), (1280, 720))
    shade = Image.new("L", (1280, 720))
    sd = ImageDraw.Draw(shade)
    for x in range(900):   # darken the text side, fading out towards the subject
        sd.line([(x, 0), (x, 720)], fill=int(170 * (1 - x / 900) ** 0.7))
    img.paste((0, 0, 0), mask=shade)
    draw = ImageDraw.Draw(img)
    words, lines = headline.upper().split(), []
    for w in words:
        if lines and len(lines[-1]) + len(w) < 12:
            lines[-1] += " " + w
        else:
            lines.append(w)
    size = 170
    font = _font(size)
    # Shrink until the widest line fits inside the darkened left panel.
    while size > 40 and max(draw.textlength(ln, font=font) for ln in lines) > 680:
        size -= 6
        font = _font(size)
    y = 360 - len(lines) * int(font.size * 1.05) // 2
    for line in lines:
        draw.text((60, y), line, font=font, fill=(255, 230, 40), stroke_width=8,
                  stroke_fill=(0, 0, 0))
        y += int(font.size * 1.05)
    img.save(out, "JPEG", quality=90)
    return out


def make(run: Run) -> Path:
    script = run.script
    bg = run.dir / "images" / "thumbnail_bg.png"
    if not bg.exists():
        prompt = (f"YouTube thumbnail background for '{script['title']}': one dramatic focal "
                  "subject on the right third, high contrast, glowing rim light, dark negative "
                  "space on the left for text")
        idx_png = images.generate_one(run, 999, prompt)
        idx_png.rename(bg)
    out = run.dir / "final" / "thumbnail.jpg"
    headline = script["thumbnail_text"]
    if run.cfg["thumbnail"]["provider"] == "canva" and not run.dry_run:
        try:
            return _canva(run, bg, headline, out)
        except Exception as err:  # noqa: BLE001
            log.warning("Canva failed (%s), using local thumbnail", err)
    return _local(bg, headline, out)
