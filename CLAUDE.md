# CLAUDE.md

Orientation for Claude Code working in this repo. See `README.md` for the user-facing setup walkthrough; this file covers what's not obvious from a quick skim.

## Project

Daily-cron Docker tool that exports a WithJoy event's guest list to Google Sheets. WithJoy has no public API, so the script drives the real UI with headless Chromium (Playwright): logs in via Auth0, clicks **Export All Guests**, intercepts the CSV, then pushes it to Sheets via a service account. Maintains a `latest` tab plus dated `YYYY-MM-DD` history tabs, pruning past `HISTORY_KEEP_DAYS`.

The image also ships a manual-trigger web UI (`web.py`) for on-demand runs in Kubernetes.

## Layout

Three source files, no package, no Makefile.

`exporter.py` — the export itself (default ENTRYPOINT):
- `main()` — env validation + orchestration
- `download_csv()` — Playwright login + export-button click + CSV byte capture
- `_select_columns()` — restricts/reorders the CSV's columns to the `EXPORT_COLUMNS` list (`_parse_columns()` splits it on commas or newlines); no-op when unset
- `_expand_tags()` — appends one `<tag> (tag)` column per unique tag (alphabetical, int 1/0); finds the tags column by header name
- `upload_to_sheets()` — orchestrates `_write_rows` for `latest` and today's tab, then prunes
- `_write_rows()` — writes a single tab; **must `resize()` before `update()`** (see Gotchas)
- `_prune_history()` — deletes dated tabs older than `HISTORY_KEEP_DAYS`
- `_dump_debug()` — screenshot + HTML dump on failure (or every step when `DEBUG=1`)

`web.py` — Flask trigger page, k8s-only (run with `command: ["python", "web.py"]`):
- `GET /` — three-button status page; `GET /api/status` — `{export, schedule, chain}`
- `POST /api/trigger` — creates a Job from the CronJob template (409 if one is active)
- `POST /api/sync-schedule` — dispatches the wedding-site workflow (503 unconfigured, 502 on GitHub errors)
- `POST /api/run-both` — creates the Job, then `run_chain` on a daemon thread dispatches the workflow **only if the export succeeded** (409 if a chain is already in flight)
- Config: `NAMESPACE`, `CRONJOB_NAME` (both default `withjoy-exporter`), `PORT` (8080), plus the `GITHUB_*` vars listed in `README.md`
- Needs a ServiceAccount with `get` on the CronJob and `get`/`list`/`create` on Jobs (`get` on Jobs is what `run_chain` polls with)

`github_sync.py` — GitHub App client for the schedule sync. Signs an App JWT, exchanges it for a cached installation token, dispatches the workflow, and summarizes runs into the same `running`/`succeeded`/`failed` vocabulary the Job side uses. Flask-free so it tests standalone.

`test_web.py` — pytest suite for `web.py` (mocked k8s client and `github_sync`). `test_github_sync.py` — JWT claims, token caching, run matching. `test_exporter.py` — pure-function tests for the CSV transforms in `exporter.py`. The Playwright path has no tests; verify via Docker.

## How to run

The exporter is Docker only — there is no `pip install` / direct-python path; Playwright browser binaries come from the base image.

```bash
docker build -t withjoy-exporter .
docker run --rm --env-file .env -v "$(pwd)/secrets:/secrets:ro" withjoy-exporter
```

For debug screenshots, add `-e DEBUG=1 -v "$(pwd)/debug:/tmp/debug"`.

Tests run directly:

```bash
pip install -r requirements-dev.txt
pytest
```

## Config

`.env.example` lists required vs optional vars; `README.md` has the full table. Required: `WITHJOY_USERNAME`, `WITHJOY_PASSWORD`, `WITHJOY_GUEST_LIST_URL`, `SHEET_ID`. Service account JSON is mounted at `/secrets/google-service-account.json` by default.

## Auth

- **WithJoy**: dedicated bot/collaborator account. **MFA must be disabled** — the script will hang on the Auth0 MFA prompt with no recovery.
- **Google Sheets**: GCP service account; the target sheet must be shared with the service account email as Editor.

## Gotchas

- **Resize before update on existing tabs** (commit 9b540d3). `worksheet.clear()` preserves grid dimensions. If the existing `latest` tab is narrower than the new data, `worksheet.update()` silently broadcasts the first column's value across all columns instead of writing rows left-to-right. `_write_rows()` calls `worksheet.resize(rows=n_rows, cols=n_cols)` before `update()` for this reason. Don't remove it. Newly-created dated tabs are unaffected because `add_worksheet(rows, cols)` sizes them at creation time.
- **Pad rows to a rectangular shape before `update()`.** Same failure mode as the resize gotcha, but per-row. WithJoy's CSV strips trailing empty fields, so `csv.reader` returns ragged rows (e.g. a plus-one row with only 14 of 22 cells). `gspread.update()` does not pad them, and on a reused worksheet the API broadcasts the row's leading value across the wider grid. `_write_rows()` runs `gspread.utils.fill_gaps(rows, rows=n_rows, cols=n_cols)` before `update()` for this reason. Don't remove it.
- **`_select_columns()` must run before `_expand_tags()`.** `EXPORT_COLUMNS` is deliberately scoped to WithJoy's own columns; tag columns are generated afterwards and are never subject to the list, so new tags need no config change. Swapping the order in `upload_to_sheets()` would drop every tag column not named in the list. The list must keep `tags` in it, or expansion finds no tags column and generates nothing.
- **MFA on the WithJoy bot account will hang the run.** The `LoginFailed` error message in `exporter.py` already says this — keep it.
- **Keep `ignore-error=true` on `cache-to: type=gha`.** With `mode=max`, BuildKit re-reserves a cache entry for every layer on every run. The `refs/heads/main` cache scope holds blobs that every build reads (so they never age out), and GitHub's cache service treats re-reserving an existing key as a hard error — which cancels the in-flight image push. Without the flag, `release.yml` creates a git tag but pushes no image and no GitHub Release. Don't remove it.
- **`.har` files in the repo root** are local debug captures (one is ~23MB). They're not source. Leave them alone; they're gitignored.
- **`workflow_dispatch` answers `204` with an empty body**, so there is no run id to correlate on. `github_sync.find_run()` matches the newest run created at or after the dispatch time, with five seconds of slack for clock skew between the pod and GitHub. Don't tighten that slack: a run stamped a moment before our own clock read would be missed forever, and the page would sit on "waiting for the run to appear".
- **`schedule_state()` caches deliberately.** The page polls every 3s; hitting the GitHub API twice a tick would burn the App's hourly rate limit on an idle tab. Idle state is re-checked at most once a minute (`IDLE_REFRESH_SECONDS`), while a run in flight is never served from cache.

## Verification

`pytest` covers `web.py` and the pure CSV transforms in `exporter.py` (no Playwright/gspread). To verify exporter changes end-to-end:

1. `docker build -t withjoy-exporter .`
2. Run against a test sheet/event with `DEBUG=1`; inspect `./debug/*.png` for any failures.
3. Confirm the `latest` tab and today's `YYYY-MM-DD` tab match the exported CSV — especially column count and that values aren't broadcast.

## CI

`.github/workflows/`:
- `build-image.yml` — PR validation; runs `pytest` and a `docker build` (no push).
- `release.yml` — on merged PR with a `release:patch|minor|major` label, bumps semver, tags, and pushes to GHCR.
- `require-release-label.yml` — enforces the release label on PRs to main.
