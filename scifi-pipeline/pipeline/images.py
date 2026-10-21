"""Scene images from Gemini (Nano Banana) and OpenAI GPT Image, alternating per scene."""
import base64
import logging
from pathlib import Path

from .context import Run
from .fixer import with_fix

log = logging.getLogger("scifi.images")


def _gemini(run: Run, prompt: str, out: Path) -> None:
    from google import genai

    client = genai.Client(api_key=run.env("GEMINI_API_KEY"))
    model = run.cfg["models"]["gemini_image"]
    if hasattr(client, "interactions"):
        # Current SDKs: Interactions API, as shown in Google's image-generation guide.
        interaction = client.interactions.create(
            model=model, input=prompt,
            response_format={"type": "image", "aspect_ratio": "16:9", "image_size": "2K"},
        )
        out.write_bytes(base64.b64decode(interaction.output_image.data))
        return
    from google.genai import types

    resp = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="16:9")),
    )
    for part in resp.parts:
        if part.inline_data:
            out.write_bytes(part.inline_data.data)
            return
    raise RuntimeError("Gemini returned no image (possibly blocked by safety filters)")


def _openai(run: Run, prompt: str, out: Path) -> None:
    from openai import OpenAI

    client = OpenAI(api_key=run.env("OPENAI_API_KEY"))
    result = client.images.generate(
        model=run.cfg["models"]["openai_image"], prompt=prompt, size="1536x864",
    )
    out.write_bytes(base64.b64decode(result.data[0].b64_json))


PROVIDERS = {"gemini": _gemini, "openai": _openai}


def _placeholder(prompt: str, out: Path, size=(1920, 1080)) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    seed = sum(map(ord, prompt))
    for y in range(size[1]):
        t = y / size[1]
        draw.line([(0, y), (size[0], y)],
                  fill=(int(10 + 40 * t), int(5 + (seed % 60) * t), int(40 + 120 * t)))
    draw.text((60, 60), prompt[:80], fill=(220, 220, 255))
    img.save(out)


def generate_one(run: Run, idx: int, prompt: str) -> Path:
    out = run.dir / "images" / f"scene_{idx:03d}.png"
    if out.exists():
        return out
    full_prompt = f"{prompt}. Style: {run.cfg['images']['style']}"
    if run.dry_run:
        _placeholder(full_prompt, out)
        return out

    providers = run.cfg["images"]["providers"]
    order = providers[idx % len(providers):] + providers[:idx % len(providers)]

    def attempt(p: str) -> Path:
        last = None
        for name in order:
            try:
                PROVIDERS[name](run, p, out)
                log.info("scene %d image via %s", idx, name)
                return out
            except Exception as err:  # noqa: BLE001 - fall through to next provider
                log.warning("%s failed for scene %d: %s", name, idx, err)
                last = err
        raise last

    # If both providers refuse the prompt, Claude rewrites it and we retry.
    return with_fix(run, f"images.scene_{idx}", attempt, full_prompt)


def generate_all(run: Run) -> None:
    for i, scene in enumerate(run.script["scenes"]):
        if scene["visual"]["type"] == "ai_image":
            generate_one(run, i, scene["visual"]["prompt"])
