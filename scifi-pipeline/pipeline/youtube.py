"""YouTube upload, scheduling, captions, playlists and the analytics feedback loop."""
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .context import ROOT, Run

log = logging.getLogger("scifi.youtube")
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
HISTORY = ROOT / "output" / "history.json"


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token = ROOT / os.environ.get("YOUTUBE_TOKEN_FILE", "output/.youtube_token.json")
    creds = Credentials.from_authorized_user_file(token, SCOPES) if token.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        if os.environ.get("CI"):
            raise RuntimeError("No YouTube token in CI: run `python run.py growth` locally once "
                               "and store output/.youtube_token.json as YOUTUBE_TOKEN_JSON")
        secrets = ROOT / os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secret.json")
        creds = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES).run_local_server(port=0)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json())
    return creds


def service(name="youtube", version="v3"):
    from googleapiclient.discovery import build
    return build(name, version, credentials=_creds())


def next_slot(cfg: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    days, hour = cfg["youtube"]["publish_weekdays"], cfg["youtube"]["publish_hour_utc"]
    for add in range(0, 15):
        d = (now + timedelta(days=add)).replace(hour=hour, minute=0, second=0, microsecond=0)
        if d.weekday() in days and d > now + timedelta(hours=2):
            return d.isoformat().replace("+00:00", "Z")
    raise ValueError("no publish weekday configured")


def description(run: Run, chapters: str) -> str:
    s = run.script
    credits = json.loads((run.dir / "credits.json").read_text()) if (
        run.dir / "credits.json").exists() else []
    parts = [s["description"]]
    if chapters:
        parts.append("Chapters:\n" + chapters)
    if credits:
        parts.append("Footage credits:\n" + "\n".join(credits))
    parts.append("Visuals in this video were created with AI image tools; narration is "
                 "AI-generated from a script researched and reviewed for this channel.")
    parts.append(" ".join(s.get("hashtags", [])))
    return "\n\n".join(parts)[:4900]   # API limit is 5000 bytes


def _insert(yt, path: Path, snippet: dict, status: dict) -> str:
    from googleapiclient.http import MediaFileUpload

    req = yt.videos().insert(
        part="snippet,status", body={"snippet": snippet, "status": status},
        media_body=MediaFileUpload(str(path), chunksize=16 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        progress, resp = req.next_chunk()
        if progress:
            log.info("upload %s %d%%", path.name, progress.progress() * 100)
    return resp["id"]


def upload(run: Run, short: Path | None = None) -> dict:
    from googleapiclient.http import MediaFileUpload

    cfg, s = run.cfg, run.script
    info = json.loads((run.dir / "timeline.json").read_text())
    yt = service()
    ycfg = cfg["youtube"]
    status = {"privacyStatus": ycfg["privacy"], "selfDeclaredMadeForKids": ycfg["made_for_kids"],
              "containsSyntheticMedia": ycfg["contains_synthetic_media"]}
    if ycfg["schedule_publish"]:
        status.update(privacyStatus="private", publishAt=next_slot(cfg))
    snippet = {"title": s["title"][:100], "description": description(run, info["chapters"]),
               "tags": s["tags"], "categoryId": ycfg["category_id"],
               "defaultLanguage": cfg["channel"]["language"],
               "defaultAudioLanguage": cfg["channel"]["language"]}

    vid = _insert(yt, Path(info["video"]), snippet, status)
    log.info("uploaded https://youtu.be/%s (publishAt=%s)", vid, status.get("publishAt"))
    result = {"video_id": vid, "publish_at": status.get("publishAt")}

    try:
        yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(
            str(run.dir / "final" / "thumbnail.jpg"), mimetype="image/jpeg")).execute()
    except Exception as err:  # noqa: BLE001 - custom thumbnails need a verified channel
        log.warning("thumbnail not set (is the channel phone-verified?): %s", err)
    yt.captions().insert(part="snippet", body={"snippet": {
        "videoId": vid, "language": cfg["channel"]["language"], "name": "English"}},
        media_body=MediaFileUpload(info["captions"], mimetype="application/octet-stream")).execute()
    if ycfg.get("playlist_id"):
        yt.playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": ycfg["playlist_id"],
            "resourceId": {"kind": "youtube#video", "videoId": vid}}}).execute()

    if short:
        sid = _insert(yt, short, {
            "title": f"{s['title'][:85]} #Shorts",
            "description": f"Full video: https://youtu.be/{vid}\n\n" + " ".join(s["hashtags"]),
            "tags": s["tags"], "categoryId": ycfg["category_id"]}, dict(status))
        result["short_id"] = sid
        log.info("short uploaded https://youtu.be/%s", sid)

    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else []
    history.append({"topic": s["topic"], "title": s["title"], "video_id": vid,
                    "short_id": result.get("short_id"), "publish_at": result["publish_at"],
                    "comment": s.get("comment_question"), "comment_posted": False,
                    "title_variants": s.get("title_variants", []), "run_dir": str(run.dir)})
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, indent=2))
    return result


def growth(cfg: dict) -> None:
    """Daily job: comment on newly-public videos and refresh insights.json for the script writer."""
    if not HISTORY.exists():
        return
    history = json.loads(HISTORY.read_text())
    yt = service()
    if cfg["youtube"]["post_comment"]:
        pending = [h for h in history if h.get("comment") and not h["comment_posted"]]
        if pending:
            ids = ",".join(h["video_id"] for h in pending)
            public = {v["id"] for v in yt.videos().list(part="status", id=ids).execute()["items"]
                      if v["status"]["privacyStatus"] == "public"}
            for h in pending:
                if h["video_id"] in public:
                    yt.commentThreads().insert(part="snippet", body={"snippet": {
                        "videoId": h["video_id"],
                        "topLevelComment": {"snippet": {"textOriginal": h["comment"]}}}}).execute()
                    h["comment_posted"] = True
                    log.info("commented on %s - pin it in Studio", h["video_id"])
            HISTORY.write_text(json.dumps(history, indent=2))

    yta = service("youtubeAnalytics", "v2")
    rows = yta.reports().query(
        ids="channel==MINE", startDate=(date.today() - timedelta(days=90)).isoformat(),
        endDate=date.today().isoformat(), dimensions="video",
        metrics="views,estimatedMinutesWatched,averageViewPercentage,subscribersGained",
        sort="-estimatedMinutesWatched", maxResults=25).execute().get("rows", [])
    by_id = {h["video_id"]: h for h in history}
    insights = [{"title": by_id[r[0]]["title"], "topic": by_id[r[0]]["topic"], "views": r[1],
                 "watch_minutes": r[2], "avg_view_pct": r[3], "subs": r[4]}
                for r in rows if r[0] in by_id]
    (ROOT / "output" / "insights.json").write_text(json.dumps(insights[:10], indent=2))
    log.info("insights updated from %d videos", len(insights))
