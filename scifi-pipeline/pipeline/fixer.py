"""Claude as the pipeline's error fixer.

When a stage fails, the traceback and the stage's input are sent to Claude, which returns a
diagnosis and, when the problem is in the data (malformed JSON, a prompt rejected by a safety
filter, a bad search query), a repaired input that the stage retries with. Claude never gets to
run code: suggested code changes are only written to fixes.md for you to review.
"""
import json
import logging
import re
import traceback
from datetime import datetime, timezone

from .context import Run

log = logging.getLogger("scifi.fixer")

SYSTEM = """You are the error-fixing step of an automated YouTube video pipeline.
You receive a failing stage name, the Python traceback, and the input the stage was called with.
Reply with ONLY a JSON object:
{"diagnosis": "<one or two sentences>",
 "action": "retry_with_input" | "retry" | "abort",
 "repaired_input": <same type/shape as the input, only when action is retry_with_input>,
 "code_suggestion": "<optional unified diff or note for the developer, never executed>"}
Use retry_with_input for data problems (invalid JSON you can correct, an image prompt blocked by
a content filter that you can rephrase safely, an empty search query). Use retry for transient
errors (timeouts, 429, 5xx). Use abort for missing credentials, quota exhaustion or bugs that
need a code change. Never suggest using copyrighted footage the owner hasn't licensed."""


def _extract_json(text: str):
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object in reply")
    return json.loads(match.group(0))


def ask_claude(run: Run, stage: str, err: BaseException, stage_input) -> dict:
    if run.dry_run:
        return {"diagnosis": f"dry run: {err}", "action": "abort"}
    import anthropic

    client = anthropic.Anthropic(api_key=run.env("ANTHROPIC_API_KEY"))
    payload = {
        "stage": stage,
        "traceback": "".join(traceback.format_exception(err))[-6000:],
        "input": stage_input if isinstance(stage_input, (str, dict, list)) else repr(stage_input),
    }
    msg = client.messages.create(
        model=run.cfg["models"]["fixer"],
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, default=str)[:60000]}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return _extract_json(text)


def _record(run: Run, stage: str, err: BaseException, verdict: dict) -> None:
    with open(run.dir / "fixes.md", "a") as f:
        f.write(f"\n## {datetime.now(timezone.utc).isoformat()} - {stage}\n")
        f.write(f"- error: `{type(err).__name__}: {err}`\n")
        f.write(f"- diagnosis: {verdict.get('diagnosis')}\n- action: {verdict.get('action')}\n")
        if verdict.get("code_suggestion"):
            f.write(f"\n```\n{verdict['code_suggestion']}\n```\n")


def with_fix(run: Run, stage: str, fn, stage_input, attempts: int = 3):
    """Call fn(stage_input); on failure let Claude diagnose and retry up to `attempts` times."""
    current = stage_input
    for i in range(attempts):
        try:
            return fn(current)
        except Exception as err:  # noqa: BLE001 - every failure goes to the fixer
            log.warning("%s failed (attempt %d/%d): %s", stage, i + 1, attempts, err)
            try:
                verdict = ask_claude(run, stage, err, current)
            except Exception as fix_err:  # noqa: BLE001
                log.error("fixer unavailable: %s", fix_err)
                raise err
            _record(run, stage, err, verdict)
            log.info("Claude: %s -> %s", verdict.get("diagnosis"), verdict.get("action"))
            action = verdict.get("action")
            if action == "abort" or i == attempts - 1:
                raise
            if action == "retry_with_input" and verdict.get("repaired_input") is not None:
                current = verdict["repaired_input"]
