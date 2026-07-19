# BookMyShow Movie Alert 🎬

Automatically pings you on **Telegram** the second a movie opens for
booking on BookMyShow — no more manually refreshing the page every hour
hoping tickets dropped. Runs completely hands-free on **GitHub Actions**,
triggered periodically by **cron-job.org**.

<img width="813" height="909" alt="Screenshot_2026-07-20-01-26-00-066_org telegram messenger" src="https://github.com/user-attachments/assets/612b99b0-ccdd-4af2-bc26-f2c462d0092f" />


## What it actually does

1. **cron-job.org** pings this repo's GitHub Actions workflow on a timer.
2. `poller.py` loads the target movie's BookMyShow page through
   **ScraperAPI** (routes the request via an Indian IP, since BMS blocks
   foreign/datacenter traffic and throws a 403 otherwise).
3. It checks if your target has opened for booking. The instant it flips
   from closed → open, you get exactly one Telegram message — after that
   it stays quiet (state is remembered in `state.json`).

## Setup

### Step 1 — Telegram bot
Message **@BotFather** on Telegram, run `/newbot`, and save the token it
gives you. Then message your new bot once, and open
`https://api.telegram.org/bot<TOKEN>/getUpdates` to grab your chat ID
from the response.

### Step 2 — ScraperAPI key
Free account at **scraperapi.com** → copy the API key from your dashboard.

### Step 3 — Repo secrets
**Settings → Secrets and variables → Actions**, add:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `SCRAPERAPI_KEY`

### Step 4 — Point it at your movie
Edit `config.json`:

**Watching one specific theatre (`venue_date`):**
```json
{
  "detector": "venue_date",
  "movie": "Movie Name Here",
  "requested_date": "20260731",
  "venue_code": "XXXX",
  "venue_label": "Cinema Name, Area",
  "url_template": "https://in.bookmyshow.com/movies/<city>/<slug>/buytickets/<ETcode>/{date}"
}
```

**Watching a date at any theatre (`bms_date`):**
```json
{
  "detector": "bms_date",
  "movie": "Movie Name Here",
  "requested_date": "20260731",
  "url_template": "https://in.bookmyshow.com/movies/<city>/<slug>/buytickets/<ETcode>/{date}",
  "min_references": 10
}
```

Grab `<city>/<slug>/<ETcode>` from the movie's own "Book tickets" URL on
BMS. For a theatre-specific `venue_code`, open a date where that theatre
already has live shows and copy the code from its cinema link
(`.../cinemas/<city>/<venue-slug>/buytickets/<CODE>/<date>`).

After editing, reset `state.json` back to `{"available": false}`.

### Step 5 — Automate the trigger
GitHub's built-in `schedule` trigger is unreliable for short intervals,
so this repo is instead triggered externally by **cron-job.org**:

1. Generate a fine-grained GitHub token (**Settings → Developer settings
   → Personal access tokens**), scoped to this repo only, with
   **Actions: Read and write** permission.
2. On cron-job.org, create a job:

| Field | Value |
|---|---|
| URL | `https://api.github.com/repos/<you>/<repo>/actions/workflows/booking-watch.yml/dispatches` |
| Schedule | every 10 min |
| Method | `POST` |
| Headers | `Accept: application/vnd.github+json`, `Authorization: Bearer <token>`, `X-GitHub-Api-Version: 2022-11-28` |
| Body | `{"ref":"main"}` |

Save, hit **Run now** once to verify it returns `204`, and you're live.

## Why the ScraperAPI detour?

BookMyShow serves India-only and blocks datacenter IPs outright — GitHub's
US-based runners get a flat 403 without it. `SCRAPERAPI_KEY` fixes this by
routing the request through an Indian IP. A `PROXY_URL` secret (any
India-based proxy) works as a drop-in alternative.

## Files

| File | Purpose |
|------|---------|
| `poller.py` | Fetches the page, runs the detector, sends the alert |
| `config.json` | Your movie / theatre / date target |
| `.github/workflows/booking-watch.yml` | The scheduled runner |
| `requirements.txt` | Python dependency (`requests`) |
| `state.json` | Tracks last-seen availability so alerts don't repeat |
