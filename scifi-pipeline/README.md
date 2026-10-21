# Sci-fi channel automation pipeline

Makes a ~10-minute sci-fi video from scratch, plus a vertical Short and a thumbnail, and schedules
both on YouTube. A daily job then posts a discussion comment and feeds analytics back into the next
script.

```
topic + past performance
        │
        ▼
 ChatGPT ── script.json (scenes, narration, image prompts, b-roll queries, title, tags…)
        │                                    ▲
        ├─ Gemini / GPT Image ── scene stills│
        ├─ licensed clips / Pexels / NASA ── b-roll
        ├─ OpenAI TTS ── narration           │  on any failure: Claude diagnoses,
        ▼                                    │  repairs the input and the stage retries
 FFmpeg ── 1080p video, captions, chapters, 9:16 Short
        ├─ Premiere Pro XML timeline (optional manual polish)
        ├─ Canva brand template ── thumbnail
        ▼
 YouTube ── scheduled upload, captions, playlist, AI disclosure, Short
        ▼
 daily growth job ── comment on new videos, pull analytics → insights.json → next script
```

| Stage | Tool | Module |
|---|---|---|
| Script, title, tags, description | ChatGPT (OpenAI Responses API) | `pipeline/script_gen.py` |
| Scene images | Gemini (Nano Banana) and GPT Image, alternating per scene with fallback | `pipeline/images.py` |
| Error fixing | Claude: reads the traceback, repairs bad JSON or blocked prompts, logs fixes to `fixes.md` | `pipeline/fixer.py` |
| B-roll | Your licensed clips, Pexels, NASA | `pipeline/broll.py` |
| Narration | OpenAI text-to-speech | `pipeline/voice.py` |
| Edit and render | FFmpeg (Ken Burns stills, fades, ducked music, -14 LUFS loudness) | `pipeline/assemble.py` |
| Premiere Pro handoff | FCP7 XML timeline | `pipeline/premiere_xml.py` |
| Thumbnail | Canva Connect autofill, with a local Pillow fallback | `pipeline/thumbnail.py` |
| Publish and grow | YouTube Data API v3 and YouTube Analytics API | `pipeline/youtube.py` |

## About movie scenes and online videos

The pipeline does **not** download scenes from films or other people's videos. Re-uploading
copyrighted footage gets Content ID claims and copyright strikes. YouTube's monetisation rules
(updated 15 July 2025) also demonetise "mass-produced and repetitious" content.

Footage can still come from three places:

- Clips you have a licence for, or public-domain films (e.g. pre-1929 titles on the Internet
  Archive). Drop them in `assets/licensed_clips/` with descriptive filenames, such as
  `rocket_launch_metropolis1927.mp4`. Scenes are matched to clips by keyword.
- Pexels stock video (free licence; attribution is added to the description automatically).
- NASA footage (generally not copyrighted; credited in the description).

If you want to discuss a film, the script prompt has ChatGPT analyse it in its own words instead
of quoting it. If you add short clips under fair use, do that yourself in Premiere with real
commentary over them. That is a judgement call the pipeline can't make for you.

## Setup

```bash
cd scifi-pipeline
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # also needs ffmpeg on PATH
cp config.example.yaml config.yaml       # set channel name, niche, schedule…
cp .env.example .env                     # add API keys
python run.py make --dry-run             # full 10-min test render, no API calls
```

You'll need:

1. **OpenAI, Gemini, Anthropic, Pexels** API keys in `.env`.
2. **YouTube**: a Google Cloud project with *YouTube Data API v3* and *YouTube Analytics API*
   enabled, plus an OAuth client of type *Desktop app* saved as `client_secret.json`. The first
   `python run.py make` opens a browser to authorise and saves the token.
   Custom thumbnails only work once the channel is verified in YouTube Studio.
3. **Canva** (optional, needs Canva Pro or higher): create a Connect integration with scopes
   `asset:write`, `brandtemplate:meta:read`, `brandtemplate:content:read`,
   `design:content:write`, `design:content:read`. Make a 1280×720 brand template with a text
   field `headline` and an image field `background`, put its ID in `config.yaml`, and put the
   client ID, secret and a refresh token in `.env`. Set `thumbnail.provider: local` to skip
   Canva.
4. **Music**: royalty-free `.mp3` files in `assets/music/`, for example from the YouTube Audio
   Library.

## Running

```bash
python run.py make                         # new topic picked by ChatGPT
python run.py make --topic "Dyson spheres" # specific topic
python run.py make --no-upload             # render only
python run.py make --resume output/<run>   # continue a run after an error (finished stages are skipped)
python run.py growth                       # comments + analytics (run daily)
```

**Polishing in Premiere Pro.** Set `editing.hold_for_manual_edit: true`. The run stops before
uploading. In Premiere, go to *File > Import* and pick `output/<run>/final/premiere_timeline.xml`.
Edit, then export over `final/video.mp4` and run `python run.py make --resume output/<run> --publish`.

**Fully automatic.** `.github/workflows/scifi-pipeline.yml` makes a video every Sunday and
Wednesday, and runs the growth job daily. Add these repository secrets:

- every key from `.env`
- `YOUTUBE_CLIENT_SECRET_JSON`: the contents of `client_secret.json`
- `YOUTUBE_TOKEN_JSON`: the contents of `output/.youtube_token.json` after one local run
- optionally `SCIFI_CONFIG_YAML`

Each run's video, thumbnail, script and `fixes.md` are saved as a workflow artifact.

## Growth automations included

- Publish slots from `config.yaml`: uploads go out as private with a `publishAt` time, so new
  videos appear on a consistent schedule.
- Every long video gets a Short cut from its hook, linking back to the full video.
- Chapters are generated in the description. YouTube only accepts them when the first timestamp
  is 0:00, there are at least 3, and each lasts at least 10 seconds, and the generator enforces
  all three rules.
- Captions (SRT) are uploaded as a caption track.
- The AI disclosure flag (`containsSyntheticMedia`) is set and a disclosure line is added to the
  description.
- A discussion question is posted as a comment once the video is public. The API can't pin
  comments, so pin it in Studio.
- The feedback loop: the top videos by watch time over the last 90 days go into
  `insights.json`, and ChatGPT is told to lean into what works. It also avoids repeating past
  topics.
- Two extra titles are saved in `history.json` for YouTube Studio's *Test & Compare*. That
  feature has no API, so set it up in Studio.

## Things to check before going live

- **Model IDs change often.** The defaults in `config.example.yaml` were taken from each
  provider's docs on 2026-10-21.
- **OpenAI's Speech API models** (`gpt-4o-mini-tts`, `tts-1`, `tts-1-hd`) are deprecated and
  shut down on 2027-01-06. Move `voice.py` to the replacement before then.
- **YouTube API quota.** The `videos.insert` docs now list 1 unit in a separate "Video Uploads"
  bucket. `thumbnails.set` costs about 50 units and `captions.insert` about 400 units of the
  default 10,000/day quota.
- **Review before publishing.** At least skim each script and the first minute of each video. The
  July 2025 policy targets channels that publish templated, low-effort uploads, so varying topics,
  formats and adding your own voice or commentary keeps the channel monetisable.

## Tests

```bash
python -m pytest -q    # renders a short dry-run video end to end (needs ffmpeg)
```
