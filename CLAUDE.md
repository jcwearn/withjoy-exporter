# CLAUDE.md

Orientation for Claude Code working in this repo. See `README.md` for the user-facing setup walkthrough; this file covers what's not obvious from a quick skim.

## Project

Daily-cron Docker tool that exports a WithJoy event's guest list to Google Sheets. WithJoy has no public API, so the script drives the real UI with headless Chromium (Playwright): logs in via Auth0, clicks **Export All Guests**, intercepts the CSV, then pushes it to Sheets via a service account. Maintains a `latest` tab plus dated `YYYY-MM-DD` history tabs, pruning past `HISTORY_KEEP_DAYS`.

## Layout

Single source file: `exporter.py`. No tests, no package, no Makefile.

- `main()` — env validation + orchestration
- `download_csv()` — Playwright login + export-button click + CSV byte capture
- `upload_to_sheets()` — orchestrates `_write_rows` for `latest` and today's tab, then prunes
- `_write_rows()` — writes a single tab; **must `resize()` before `update()`** (see Gotchas)
- `_prune_history()` — deletes dated tabs older than `HISTORY_KEEP_DAYS`
- `_dump_debug()` — screenshot + HTML dump on failure (or every step when `DEBUG=1`)

## How to run

Docker only — there is no `pip install` / direct-python path; Playwright browser binaries come from the base image.

```bash
docker build -t withjoy-exporter .
docker run --rm --env-file .env -v "$(pwd)/secrets:/secrets:ro" withjoy-exporter
```

For debug screenshots, add `-e DEBUG=1 -v "$(pwd)/debug:/tmp/debug"`.

## Config

`.env.example` lists required vs optional vars; `README.md` has the full table. Required: `WITHJOY_USERNAME`, `WITHJOY_PASSWORD`, `WITHJOY_GUEST_LIST_URL`, `SHEET_ID`. Service account JSON is mounted at `/secrets/google-service-account.json` by default.

## Auth

- **WithJoy**: dedicated bot/collaborator account. **MFA must be disabled** — the script will hang on the Auth0 MFA prompt with no recovery.
- **Google Sheets**: GCP service account; the target sheet must be shared with the service account email as Editor.

## Gotchas

- **Resize before update on existing tabs** (commit 9b540d3). `worksheet.clear()` preserves grid dimensions. If the existing `latest` tab is narrower than the new data, `worksheet.update()` silently broadcasts the first column's value across all columns instead of writing rows left-to-right. `_write_rows()` calls `worksheet.resize(rows=n_rows, cols=n_cols)` before `update()` for this reason. Don't remove it. Newly-created dated tabs are unaffected because `add_worksheet(rows, cols)` sizes them at creation time.
- **MFA on the WithJoy bot account will hang the run.** The `LoginFailed` error message in `exporter.py` already says this — keep it.
- **`.har` files in the repo root** are local debug captures (one is ~23MB). They're not source. Leave them alone; they're gitignored.

## Verification

There's no test suite. To verify changes end-to-end:

1. `docker build -t withjoy-exporter .`
2. Run against a test sheet/event with `DEBUG=1`; inspect `./debug/*.png` for any failures.
3. Confirm the `latest` tab and today's `YYYY-MM-DD` tab match the exported CSV — especially column count and that values aren't broadcast.

## CI

`.github/workflows/`:
- `build-image.yml` — PR validation; `docker build` only, no push.
- `release.yml` — on merged PR with a `release:patch|minor|major` label, bumps semver, tags, and pushes to GHCR.
- `require-release-label.yml` — enforces the release label on PRs to main.
