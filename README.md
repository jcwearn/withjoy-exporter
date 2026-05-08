# WithJoy Exporter

A daily-cron-friendly Docker image that exports your [WithJoy](https://withjoy.com) guest list to a Google Sheet, with a "latest" tab plus rolling daily history snapshots.

## How it works

WithJoy doesn't expose a server-side export endpoint — its **Export All Guests** button generates the CSV entirely in the browser from data delivered over Firebase Realtime Database. So this tool drives the real UI:

1. Headless Chromium logs into WithJoy via Auth0 with username + password
2. Navigates to your guest list page and clicks **Export All Guests**
3. Intercepts the resulting CSV download
4. Pushes it to a Google Sheet using a service account:
   - Overwrites a `latest` tab on every run
   - Creates a dated tab (`YYYY-MM-DD`) for history
   - Prunes dated tabs beyond `HISTORY_KEEP_DAYS` (default 5)

## Prerequisites

- Docker
- A Google account
- A WithJoy account with access to the event you want to export

## Setup

### 1. Create a dedicated WithJoy collaborator account

**Recommended:** create a separate WithJoy account for this script and add it as a collaborator on your event. This avoids flagging your main account as a bot if Auth0's automated-login detection ever kicks in. You can use a [SimpleLogin](https://simplelogin.io) / Mozilla relay alias for the email.

Steps in WithJoy:
1. Sign up for a new WithJoy account using the alias email
2. From your main account, invite that email as a collaborator on the event
3. Accept the invite from the new account
4. Confirm MFA is **disabled** on the new account (scripted login will not survive MFA prompts)

### 2. Create a Google Cloud service account

1. Go to https://console.cloud.google.com and create a new project (or pick an existing one)
2. **Enable the Sheets API:** APIs & Services → Library → search "Google Sheets API" → **Enable**
3. **Create the service account:** IAM & Admin → Service Accounts → **Create Service Account** → name it (e.g. `withjoy-exporter`) → skip the "grant access" step → **Done**
4. **Create a JSON key:** click the new service account → **Keys** tab → **Add Key** → **Create new key** → JSON. The file downloads automatically.
5. Save the file as `secrets/google-service-account.json` in this project (the `secrets/` folder is gitignored).

### 3. Create the Google Sheet and share it

1. Create a new Google Sheet
2. Copy the **Spreadsheet ID** from the URL — it's the long string between `/d/` and `/edit`
3. Click **Share**, paste the service account's email (looks like `withjoy-exporter@<project-id>.iam.gserviceaccount.com`), give it **Editor**, and send

### 4. Configure your environment

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

```ini
WITHJOY_USERNAME=your-collaborator-email@example.com
WITHJOY_PASSWORD=...
WITHJOY_GUEST_LIST_URL=https://withjoy.com/<event-slug>/edit/guests
SHEET_ID=<spreadsheet id from step 3>
```

`.env` is gitignored, so credentials stay local.

## Running locally

Build the image:

```bash
docker build -t withjoy-exporter .
```

Run it:

```bash
docker run --rm \
  --env-file .env \
  -v "$(pwd)/secrets:/secrets:ro" \
  withjoy-exporter
```

Expected output:

```
Exported 85 guests → tab 'latest' + tab '2026-05-08'. Pruned 0 old tabs.
```

Then open the Google Sheet and confirm the `latest` and `YYYY-MM-DD` tabs are populated.

### Debugging

If something fails (login, button locator, etc.), the script writes a screenshot and HTML snapshot to `/tmp/debug` inside the container. Mount that path to inspect:

```bash
docker run --rm \
  --env-file .env \
  -v "$(pwd)/secrets:/secrets:ro" \
  -v "$(pwd)/debug:/tmp/debug" \
  -e DEBUG=1 \
  withjoy-exporter
```

`DEBUG=1` also captures intermediate screenshots on the success path.

## Configuration

| Variable                          | Required | Default                                | Description                                                  |
| --------------------------------- | -------- | -------------------------------------- | ------------------------------------------------------------ |
| `WITHJOY_USERNAME`                | yes      | —                                      | WithJoy login email                                          |
| `WITHJOY_PASSWORD`                | yes      | —                                      | WithJoy password                                             |
| `WITHJOY_GUEST_LIST_URL`          | yes      | —                                      | Full URL to your event's guest list page                     |
| `SHEET_ID`                        | yes      | —                                      | Google Spreadsheet ID                                        |
| `GOOGLE_APPLICATION_CREDENTIALS`  | no       | `/secrets/google-service-account.json` | Path inside the container to the service account JSON        |
| `LATEST_TAB_NAME`                 | no       | `latest`                               | Name of the always-overwritten tab                           |
| `TIMEZONE`                        | no       | `America/New_York`                     | IANA timezone used to date the history tabs                  |
| `HISTORY_KEEP_DAYS`               | no       | `5`                                    | How many dated history tabs to retain                        |
| `DEBUG`                           | no       | unset                                  | When set, dumps screenshots/HTML at each step to `/tmp/debug` |
| `DEBUG_OUTPUT_DIR`                | no       | `/tmp/debug`                           | Where debug artifacts are written on failure                 |

## Running in k3s

A `release:patch` / `release:minor` / `release:major` label on a merged PR triggers the `Release` workflow, which builds and publishes the image to GitHub Container Registry:

```
ghcr.io/jcwearn/withjoy-exporter:vX.Y.Z
ghcr.io/jcwearn/withjoy-exporter:vX.Y
ghcr.io/jcwearn/withjoy-exporter:sha-<commit>
```

Pull from any of those tags as a `CronJob` image. Mount the GCP service account JSON as a `Secret` volume and the WithJoy credentials + Sheet ID as `Secret` env vars. Concrete manifests are out of scope for this repo.

## Troubleshooting

- **`Login did not leave the auth flow`** — bad credentials, MFA enabled on the account, or Auth0 is rate-limiting / showing a CAPTCHA. Check `debug/login_failed.png`.
- **`Navigating to the guest list bounced back to login`** — login looked successful but the session wasn't established. Usually transient; retry.
- **`Locator.wait_for: Timeout 30000ms exceeded`** on the export button — WithJoy may have changed the button's text or DOM. Check `debug/export_button_not_found.png` and update the locator in `exporter.py`.
- **`gspread.SpreadsheetNotFound`** — you forgot to share the Sheet with the service account email, or `SHEET_ID` is wrong.
