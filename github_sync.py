"""Dispatch and track the wedding site's "Refresh schedule index" workflow.

The export writes a Google Sheet; a GitHub Actions workflow in
jcwearn/anupamaandjackson reads that sheet and rebuilds the site's schedule
index. This module is the client half of that handoff: it authenticates as a
GitHub App, dispatches the workflow, and reports on the resulting run.

Deliberately Flask-free so it can be unit-tested on its own.
"""

import json
import os
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import jwt

API_ROOT = "https://api.github.com"

APP_ID = os.environ.get("GITHUB_APP_ID", "")
INSTALLATION_ID = os.environ.get("GITHUB_APP_INSTALLATION_ID", "")
PRIVATE_KEY = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
REPO = os.environ.get("GITHUB_REPO", "jcwearn/anupamaandjackson")
WORKFLOW_FILE = os.environ.get("GITHUB_WORKFLOW_FILE", "refresh-schedule-index.yml")
REF = os.environ.get("GITHUB_REF", "main")


class GitHubError(RuntimeError):
    """Anything that stops us from dispatching or reading a workflow run."""


def configured() -> bool:
    """True when the App credentials are present. Lets the UI degrade politely."""
    return bool(APP_ID and INSTALLATION_ID and PRIVATE_KEY)


def _request(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    url = path if path.startswith("http") else f"{API_ROOT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            raw = res.read()
            # workflow dispatch answers 204 with an empty body.
            return res.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise GitHubError(f"{method} {path} failed: {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise GitHubError(f"{method} {path} failed: {exc.reason}") from exc


def _app_jwt(now: datetime) -> str:
    """Sign the short-lived assertion that identifies us as the App itself.

    `iat` is backdated a minute because GitHub rejects tokens issued in its
    future, and container clocks drift.
    """
    claims = {
        "iat": int((now - timedelta(seconds=60)).timestamp()),
        "exp": int((now + timedelta(seconds=540)).timestamp()),
        "iss": APP_ID,
    }
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256")


_token_lock = threading.Lock()
_token: dict = {}


def installation_token(now: datetime | None = None) -> str:
    """An installation token, cached until a minute before it expires."""
    now = now or datetime.now(UTC)
    if not configured():
        raise GitHubError(
            "GitHub App is not configured (GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, "
            "GITHUB_APP_PRIVATE_KEY)."
        )
    with _token_lock:
        if _token and _token["expires_at"] - timedelta(seconds=60) > now:
            return _token["value"]
        _, body = _request(
            "POST",
            f"/app/installations/{INSTALLATION_ID}/access_tokens",
            _app_jwt(now),
        )
        if "token" not in body:
            raise GitHubError("Installation token response had no token.")
        _token.clear()
        _token.update(
            value=body["token"],
            expires_at=_parse_ts(body["expires_at"]),
        )
        return _token["value"]


def _parse_ts(value: str) -> datetime:
    # fromisoformat has parsed a trailing Z natively since 3.11 and the
    # container runs 3.12, so the old .replace("Z", "+00:00") was a no-op.
    return datetime.fromisoformat(value)


def dispatch_workflow() -> None:
    """Ask GitHub to start a run. Answers 204 with no body, so no run id here."""
    status, _ = _request(
        "POST",
        f"/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        installation_token(),
        {"ref": REF},
    )
    if status != 204:
        raise GitHubError(f"Unexpected dispatch status {status}.")


def _runs(limit: int = 10) -> list[dict]:
    _, body = _request(
        "GET",
        f"/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs?per_page={limit}",
        installation_token(),
    )
    return body.get("workflow_runs", [])


def find_run(since: datetime) -> dict | None:
    """The newest run created at or after `since`.

    Dispatch gives us nothing to correlate on, so we match on time. The five
    seconds of slack absorbs clock skew between us and GitHub; without it a run
    stamped a moment before our own clock read would be missed forever.
    """
    cutoff = since - timedelta(seconds=5)
    for run in _runs():
        created = run.get("created_at")
        if created and _parse_ts(created) >= cutoff:
            return summarize_run(run)
    return None


def latest_run() -> dict | None:
    """Whatever ran most recently, for repopulating the UI after a restart."""
    runs = _runs(limit=1)
    return summarize_run(runs[0]) if runs else None


def run_status(run_id: int) -> dict:
    _, body = _request("GET", f"/repos/{REPO}/actions/runs/{run_id}", installation_token())
    return summarize_run(body)


def summarize_run(run: dict) -> dict:
    """Flatten a run into the shape the page renders.

    GitHub splits progress across `status` and `conclusion`; the UI only wants
    one word, mapped onto the same running/succeeded/failed vocabulary the k8s
    Job side already uses.
    """
    status = run.get("status")
    conclusion = run.get("conclusion")
    if status == "completed":
        state = {"success": "succeeded"}.get(conclusion, "failed")
    else:
        state = "running"
    return {
        "state": state,
        "status": status,
        "conclusion": conclusion,
        "run_id": run.get("id"),
        "html_url": run.get("html_url"),
        "created_at": run.get("created_at"),
    }
