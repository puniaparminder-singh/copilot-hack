"""ChatGPT writes the topic, the scene-by-scene script and the YouTube metadata."""
import json
import logging

from .context import ROOT, Run
from .fixer import with_fix

log = logging.getLogger("scifi.script")

SCHEMA_HINT = """Return ONLY JSON with this shape:
{
 "topic": str,
 "title": str (<= 70 chars, curiosity-driven, no clickbait lies),
 "title_variants": [str, str] (alternatives for Studio's Test & Compare),
 "thumbnail_text": str (2-4 punchy words),
 "description": str (3 short paragraphs, no links),
 "tags": [str] (10-15),
 "hashtags": [str] (3, each starting with #),
 "comment_question": str (a question to spark discussion),
 "scenes": [
   {"chapter": str, "narration": str,
    "visual": {"type": "ai_image" | "broll", "prompt": str, "query": str}}
 ]
}
Rules: the first scene is a 10-20 second hook. Each scene's narration is 40-90 words.
About 60% of scenes are "ai_image" (prompt = detailed image description, no text in image) and
40% "broll" (query = 2-4 word stock-footage search like "rocket launch" or "nebula").
Group scenes into 5-8 chapters. State real science accurately; label speculation as speculation.
Do not quote dialogue from films; describe and analyse them in your own words."""


def _history(run: Run) -> list:
    path = ROOT / "output" / "history.json"
    return json.loads(path.read_text()) if path.exists() else []


def _insights() -> str:
    path = ROOT / "output" / "insights.json"
    if not path.exists():
        return ""
    data = json.loads(path.read_text())
    return "What performed best on this channel so far (lean into it): " + json.dumps(data)[:3000]


def word_count(script: dict) -> int:
    return sum(len(s["narration"].split()) for s in script["scenes"])


def _validate(script: dict) -> dict:
    for key in ("title", "description", "tags", "scenes", "thumbnail_text"):
        if key not in script:
            raise ValueError(f"script missing '{key}'")
    for i, s in enumerate(script["scenes"]):
        if not s.get("narration") or s.get("visual", {}).get("type") not in ("ai_image", "broll"):
            raise ValueError(f"scene {i} malformed: {s}")
    return script


def _ask_gpt(run: Run, instructions: str, prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=run.env("OPENAI_API_KEY"))
    resp = client.responses.create(
        model=run.cfg["models"]["script"],
        instructions=instructions,
        input=prompt,
        text={"format": {"type": "json_object"}},
    )
    return resp.output_text


def _parse(raw):
    # The fixer may hand back already-corrected JSON as a dict instead of a string.
    return _validate(raw if isinstance(raw, dict) else json.loads(raw))


def generate(run: Run, topic: str | None = None) -> dict:
    cfg = run.cfg
    target_words = int(cfg["video"]["target_minutes"] * cfg["video"]["words_per_minute"])
    min_words = int(cfg["video"]["min_minutes"] * cfg["video"]["words_per_minute"])

    if run.dry_run:
        script = _dry_script(target_words, topic)
    else:
        done = [h["topic"] for h in _history(run)][-50:]
        instructions = (
            f"You are the head writer for the YouTube channel '{cfg['channel']['name']}' about "
            f"{cfg['channel']['niche']}. Write in {cfg['channel']['language']}, conversational, "
            "vivid and fact-checked.\n" + SCHEMA_HINT
        )
        prompt = (
            (f"Topic: {topic}\n" if topic else "Pick a fresh, high-interest topic.\n")
            + f"Avoid these covered topics: {done}\n{_insights()}\n"
            + f"Total narration must be about {target_words} words (minimum {min_words})."
        )
        raw = _ask_gpt(run, instructions, prompt)
        script = with_fix(run, "script.parse", _parse, raw)

        # Too short for a 10-minute video: ask ChatGPT to expand it, keeping the same JSON shape.
        for _ in range(3):
            words = word_count(script)
            if words >= min_words:
                break
            log.info("script has %d words, expanding to %d", words, target_words)
            raw = _ask_gpt(
                run, instructions,
                f"Expand this script to about {target_words} words by adding scenes and depth "
                f"(not filler). Return the full JSON.\n{json.dumps(script)}",
            )
            script = with_fix(run, "script.parse", _parse, raw)

    log.info("script: '%s', %d scenes, %d words", script["title"], len(script["scenes"]),
             word_count(script))
    (run.dir / "script.json").write_text(json.dumps(script, indent=2))
    return script


def _dry_script(target_words: int, topic: str | None) -> dict:
    words_per_scene = 60
    n = max(3, -(-target_words // words_per_scene))
    filler = ("The signal arrived from beyond the heliopause, and nothing about it made sense "
              "to the engineers who first heard it. ")
    scenes = []
    for i in range(n):
        narration = " ".join((filler * 5).split()[:words_per_scene])
        visual = ({"type": "ai_image", "prompt": f"alien megastructure {i}", "query": ""}
                  if i % 5 not in (1, 3) else {"type": "broll", "prompt": "", "query": "space nebula"})
        scenes.append({"chapter": f"Part {i // 4 + 1}", "narration": narration, "visual": visual})
    return {
        "topic": topic or "Dry run topic",
        "title": "What If the Signal Was Never Meant for Us?",
        "title_variants": ["The Signal From Beyond", "Nobody Was Supposed to Hear This"],
        "thumbnail_text": "IT ANSWERED",
        "description": "A dry-run description.\n\nSecond paragraph.\n\nThird paragraph.",
        "tags": ["sci-fi", "space", "aliens"],
        "hashtags": ["#scifi", "#space", "#aliens"],
        "comment_question": "Would you answer the signal?",
        "scenes": scenes,
    }
