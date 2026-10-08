import os
import sys
import threading
import time
from datetime import UTC, datetime

from flask import Flask, jsonify, render_template_string
from kubernetes import client, config
from kubernetes.client.rest import ApiException

import github_sync

NAMESPACE = os.environ.get("NAMESPACE", "withjoy-exporter")
CRONJOB_NAME = os.environ.get("CRONJOB_NAME", "withjoy-exporter")

# How long the chain will wait for an export before giving up. The CronJob's own
# activeDeadlineSeconds is 1800, so this outlasts any run k8s would allow. The
# poll was 5s, which put up to 5s of dead air between a ~16s export and the
# dispatch; a get on one Job a second is nothing to the API server.
CHAIN_POLL_SECONDS = 1
CHAIN_MAX_POLLS = 2000

# An idle workflow is re-checked at most this often, so a tab left open
# overnight doesn't burn the App's hourly rate limit.
IDLE_REFRESH_SECONDS = 60

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WithJoy Exporter</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 560px;
         margin: 4rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em;
       color: #6b7280; margin: 0 0 0.5rem; }
  .actions { display: flex; flex-wrap: wrap; gap: 0.6rem; margin: 1.5rem 0; }
  button { font-size: 1.1rem; padding: 0.6rem 1.4rem; border: none; border-radius: 6px;
           background: #2563eb; color: #fff; cursor: pointer; }
  button:disabled { background: #93c5fd; cursor: default; }
  button.secondary { background: #4b5563; }
  button.secondary:disabled { background: #d1d5db; }
  .card { margin-top: 1rem; padding: 1rem; border-radius: 6px; background: #f3f4f6; }
  #chain { background: #eff6ff; border-left: 3px solid #2563eb; }
  #chain.aborted { background: #fef2f2; border-left-color: #b91c1c; }
  .note { margin-top: 1.5rem; font-size: 0.85rem; color: #6b7280; }
  .running { color: #b45309; } .succeeded { color: #15803d; } .failed { color: #b91c1c; }
  .unconfigured, .none { color: #6b7280; } .error { color: #b91c1c; }
</style>
</head>
<body>
<h1>WithJoy Guest List Exporter</h1>
<p>Exports the guest list to Google Sheets, then rebuilds the wedding site's
schedule index from it. Both run automatically each morning; use these to run
them now.</p>

<div class="actions">
  <button id="export">Run export</button>
  <button id="sync" class="secondary">Sync schedule</button>
  <button id="both" class="secondary">Run both</button>
</div>

<div id="chain" class="card" hidden></div>

<div class="card">
  <h2>Export &rarr; Google Sheets</h2>
  <div id="export-status">Loading status…</div>
</div>

<div class="card">
  <h2>Schedule index &rarr; wedding site</h2>
  <div id="sync-status">Loading status…</div>
</div>

<p class="note">Both steps skip writing when nothing has changed, so a run that
succeeds without producing an update is normal.</p>

<script>
const btn = {
  export: document.getElementById("export"),
  sync: document.getElementById("sync"),
  both: document.getElementById("both"),
};
const box = {
  export: document.getElementById("export-status"),
  sync: document.getElementById("sync-status"),
  chain: document.getElementById("chain"),
};

const CHAIN_TEXT = {
  "waiting-export": "Running both — waiting for the export to finish…",
  "dispatching": "Running both — export succeeded, starting the schedule sync…",
  "done": "Ran both — the export finished and the schedule sync was started.",
  "done-unchanged": "Ran both — the sheet hadn't changed, so the schedule sync was skipped. Use Sync schedule to force one.",
  "aborted": "Ran both — stopped before the schedule sync.",
};

function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, c => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
  ));
}

function when(v) {
  if (!v) return "—";
  const d = new Date(v);
  return isNaN(d) ? esc(v) : d.toLocaleString();
}

function tag(state) {
  return `<span class="${esc(state)}">${esc(state)}</span>`;
}

function renderExport(e) {
  if (!e || e.state === "none") return "No runs found.";
  let out = `Last run (${e.manual ? "manual" : "scheduled"}): ${tag(e.state)}<br>` +
            `Job: ${esc(e.job_name)}<br>Started: ${when(e.started_at)}`;
  if (e.finished_at) out += `<br>Finished: ${when(e.finished_at)}`;
  return out;
}

function renderSchedule(s) {
  if (!s || s.state === "none") return "No runs found.";
  if (s.state === "unconfigured") {
    return "GitHub App not configured — the schedule sync is unavailable.";
  }
  if (s.state === "error") {
    return `${tag("error")} Could not reach GitHub.<br>${esc(s.error)}`;
  }
  let out = `Last run: ${tag(s.state)}`;
  if (s.created_at) out += `<br>Started: ${when(s.created_at)}`;
  if (s.html_url) {
    out += `<br><a href="${esc(s.html_url)}" target="_blank" rel="noopener">View on GitHub</a>`;
  }
  if (!s.run_id && s.state === "running") out += "<br>Waiting for the run to appear…";
  return out;
}

function renderChain(c) {
  const text = CHAIN_TEXT[c && c.state];
  if (!text) { box.chain.hidden = true; return; }
  box.chain.hidden = false;
  box.chain.classList.toggle("aborted", c.state === "aborted");
  box.chain.innerHTML = c.error ? `${esc(text)}<br>${tag("failed")} ${esc(c.error)}` : esc(text);
}

function render(s) {
  box.export.innerHTML = renderExport(s.export);
  box.sync.innerHTML = renderSchedule(s.schedule);
  renderChain(s.chain);

  const chaining = ["waiting-export", "dispatching"].includes(s.chain && s.chain.state);
  const exporting = s.export && s.export.state === "running";
  const syncing = s.schedule && s.schedule.state === "running";
  const unconfigured = s.schedule && s.schedule.state === "unconfigured";

  btn.export.disabled = exporting || chaining;
  btn.sync.disabled = syncing || chaining || unconfigured;
  btn.both.disabled = exporting || syncing || chaining || unconfigured;
}

async function refresh() {
  try {
    const res = await fetch("/api/status");
    render(await res.json());
  } catch (e) {
    box.export.textContent = "Failed to load status: " + e;
  }
}

async function post(path, button) {
  button.disabled = true;
  try {
    const res = await fetch(path, { method: "POST" });
    const body = await res.json();
    if (!res.ok) alert(body.error || "Request failed");
  } catch (e) {
    alert("Request failed: " + e);
  }
  await refresh();
}

btn.export.addEventListener("click", () => post("/api/trigger", btn.export));
btn.sync.addEventListener("click", () => post("/api/sync-schedule", btn.sync));
btn.both.addEventListener("click", () => post("/api/run-both", btn.both));

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""

app = Flask(__name__)

_batch = None


def batch_api() -> client.BatchV1Api:
    global _batch
    if _batch is None:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        _batch = client.BatchV1Api()
    return _batch


_core = None


def core_api() -> client.CoreV1Api:
    """Pods, for reading the export's termination message. Config is loaded by
    batch_api(), which every caller has already gone through."""
    global _core
    if _core is None:
        batch_api()
        _core = client.CoreV1Api()
    return _core


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def job_state(job) -> str:
    """Read a Job's outcome from its conditions.

    `status.failed` counts failed pods, not a failed Job: with a backoffLimit
    the first pod can die (e.g. Auth0 throttling the login) while a retry pod
    goes on to succeed. Only the Failed condition means k8s has given up.
    """
    for cond in job.status.conditions or []:
        if cond.status == "True" and cond.type == "Complete":
            return "succeeded"
        if cond.status == "True" and cond.type == "Failed":
            return "failed"
    return "succeeded" if job.status.succeeded else "running"


def summarize_jobs(jobs: list) -> dict:
    if not jobs:
        return {"state": "none"}
    latest = max(jobs, key=lambda j: j.metadata.creation_timestamp)
    status = latest.status
    state = job_state(latest)
    labels = latest.metadata.labels or {}
    return {
        "state": state,
        "job_name": latest.metadata.name,
        "started_at": _iso(status.start_time or latest.metadata.creation_timestamp),
        "finished_at": _iso(status.completion_time),
        "manual": labels.get("trigger") == "manual",
    }


def active_job(jobs: list):
    for job in jobs:
        if job.status.active:
            return job
    return None


def build_manual_job(cronjob, now: datetime) -> client.V1Job:
    template = cronjob.spec.job_template
    spec = template.spec
    spec.ttl_seconds_after_finished = 86400
    labels = dict(template.metadata.labels or {})
    labels["trigger"] = "manual"
    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            name=f"{cronjob.metadata.name}-manual-{now.strftime('%Y%m%d%H%M%S')}",
            namespace=cronjob.metadata.namespace,
            labels=labels,
            annotations={"cronjob.kubernetes.io/instantiate": "manual"},
        ),
        spec=spec,
    )


class TriggerError(Exception):
    """A reason we could not start an export, carrying the HTTP status to use."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def create_export_job(api, now: datetime) -> client.V1Job:
    running = active_job(api.list_namespaced_job(NAMESPACE).items)
    if running is not None:
        raise TriggerError(f"A run is already in progress ({running.metadata.name}).", 409)
    try:
        cronjob = api.read_namespaced_cron_job(CRONJOB_NAME, NAMESPACE)
    except ApiException as exc:
        raise TriggerError(f"Could not read CronJob {CRONJOB_NAME}: {exc.reason}", 500) from exc
    job = build_manual_job(cronjob, now)
    api.create_namespaced_job(NAMESPACE, job)
    return job


# ---------------------------------------------------------------------------
# Schedule-sync state
#
# The workflow run lives in GitHub, not here; these globals are just a cache so
# the 3-second poll from the page doesn't mean two API calls a tick forever.
# ---------------------------------------------------------------------------

_schedule_lock = threading.Lock()
_schedule: dict = {}

_INTERNAL_KEYS = ("checked_at", "awaiting_since")


def _public(state: dict) -> dict:
    return {k: v for k, v in state.items() if k not in _INTERNAL_KEYS}


def mark_dispatched(dispatched_at: datetime) -> None:
    """Record that we just asked for a run, before its id is known."""
    with _schedule_lock:
        _schedule.clear()
        _schedule.update(
            state="running",
            run_id=None,
            html_url=None,
            awaiting_since=dispatched_at,
        )


def schedule_state(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    if not github_sync.configured():
        return {"state": "unconfigured"}
    with _schedule_lock:
        cached = dict(_schedule)
    if (
        cached.get("checked_at")
        and cached.get("state") != "running"
        and (now - cached["checked_at"]).total_seconds() < IDLE_REFRESH_SECONDS
    ):
        return _public(cached)
    try:
        if cached.get("run_id"):
            run = github_sync.run_status(cached["run_id"])
        elif cached.get("awaiting_since"):
            # Dispatched, but the run may not be listable yet.
            run = github_sync.find_run(cached["awaiting_since"])
        else:
            run = github_sync.latest_run() or {"state": "none"}
    except github_sync.GitHubError as exc:
        return {"state": "error", "error": str(exc)}
    with _schedule_lock:
        if run is None:
            _schedule.update(checked_at=now)
        else:
            awaiting = cached.get("awaiting_since")
            _schedule.clear()
            _schedule.update(run, checked_at=now)
            if run.get("run_id") is None and awaiting:
                _schedule["awaiting_since"] = awaiting
        return _public(dict(_schedule))


# ---------------------------------------------------------------------------
# "Run both" chain
# ---------------------------------------------------------------------------

_chain_lock = threading.Lock()
_chain: dict = {"state": "idle"}


def _set_chain(**fields) -> None:
    with _chain_lock:
        _chain.clear()
        _chain.update(fields)


def chain_state() -> dict:
    with _chain_lock:
        return dict(_chain)


def chain_running() -> bool:
    return chain_state().get("state") in ("waiting-export", "dispatching")


def export_result(core, job_name: str) -> str | None:
    """What the export reported in its termination message, if anything.

    exporter.py writes `changed` or `unchanged` to /dev/termination-log. Read
    from whichever of the Job's pods exited 0, since a backoffLimit retry can
    leave a failed pod beside it. Any failure to read it -- no RBAC on pods
    yet, an older image that writes nothing -- returns None, and the caller
    treats None as "changed": a sync that turns out to be a no-op costs a
    minute, a skipped one that mattered leaves the site stale until morning.
    """
    try:
        pods = core.list_namespaced_pod(NAMESPACE, label_selector=f"job-name={job_name}")
    except ApiException:
        return None
    for pod in pods.items or []:
        for status in pod.status.container_statuses or []:
            terminated = status.state.terminated if status.state else None
            if terminated and terminated.exit_code == 0:
                return (terminated.message or "").strip() or None
    return None


def run_chain(
    api,
    job_name: str,
    core=None,
    sleep=time.sleep,
    attempts: int = CHAIN_MAX_POLLS,
    now: datetime | None = None,
) -> None:
    """Wait for an export Job, then dispatch the schedule sync if it succeeded.

    Runs on a daemon thread so closing the browser tab doesn't abandon the
    chain. State is in memory only, so a pod restart mid-chain loses it — the
    page shows the chain as idle rather than pretending it is still going.

    A failed export deliberately does not dispatch: the workflow would rebuild
    the schedule from a stale or half-written sheet. Neither does one that
    reports the sheet unchanged -- the sync would rebuild an identical index
    and commit nothing, after the person pressing the button had waited a
    minute and a half for it. `core` is how that report is read; without it
    the chain always dispatches.
    """
    _set_chain(state="waiting-export", job_name=job_name)
    for attempt in range(attempts):
        try:
            job = api.read_namespaced_job(job_name, NAMESPACE)
        except ApiException as exc:
            _set_chain(
                state="aborted",
                job_name=job_name,
                error=f"Could not read Job {job_name}: {exc.reason}",
            )
            return
        state = job_state(job)
        if state == "succeeded":
            break
        if state == "failed":
            _set_chain(
                state="aborted",
                job_name=job_name,
                error=f"Export {job_name} failed, so the schedule sync was not started.",
            )
            return
        if attempt == attempts - 1:
            _set_chain(
                state="aborted",
                job_name=job_name,
                error=f"Gave up waiting for export {job_name} to finish.",
            )
            return
        sleep(CHAIN_POLL_SECONDS)

    if core is not None and export_result(core, job_name) == "unchanged":
        _set_chain(state="done-unchanged", job_name=job_name)
        return

    dispatched_at = now or datetime.now(UTC)
    _set_chain(state="dispatching", job_name=job_name)
    try:
        github_sync.dispatch_workflow()
    except github_sync.GitHubError as exc:
        _set_chain(state="aborted", job_name=job_name, error=str(exc))
        return
    mark_dispatched(dispatched_at)
    _set_chain(state="done", job_name=job_name)


@app.get("/")
def index():
    return render_template_string(PAGE)


@app.get("/api/status")
def api_status():
    jobs = batch_api().list_namespaced_job(NAMESPACE).items
    return jsonify(
        {
            "export": summarize_jobs(jobs),
            "schedule": schedule_state(),
            "chain": chain_state(),
        }
    )


@app.post("/api/trigger")
def api_trigger():
    try:
        job = create_export_job(batch_api(), datetime.now(UTC))
    except TriggerError as exc:
        return jsonify({"error": str(exc)}), exc.status
    return jsonify({"job_name": job.metadata.name}), 202


@app.post("/api/sync-schedule")
def api_sync_schedule():
    if not github_sync.configured():
        return jsonify({"error": "GitHub App is not configured."}), 503
    dispatched_at = datetime.now(UTC)
    try:
        github_sync.dispatch_workflow()
    except github_sync.GitHubError as exc:
        return jsonify({"error": str(exc)}), 502
    mark_dispatched(dispatched_at)
    return jsonify({"dispatched": True}), 202


@app.post("/api/run-both")
def api_run_both():
    if not github_sync.configured():
        return jsonify({"error": "GitHub App is not configured."}), 503
    if chain_running():
        return jsonify({"error": "A chained run is already in progress."}), 409
    api = batch_api()
    try:
        job = create_export_job(api, datetime.now(UTC))
    except TriggerError as exc:
        return jsonify({"error": str(exc)}), exc.status
    threading.Thread(
        target=run_chain, args=(api, job.metadata.name, core_api()), daemon=True
    ).start()
    return jsonify({"job_name": job.metadata.name}), 202


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(
        f"serving on 0.0.0.0:{port} (namespace={NAMESPACE}, cronjob={CRONJOB_NAME}, "
        f"github={'configured' if github_sync.configured() else 'unconfigured'})",
        file=sys.stderr,
    )
    app.run(host="0.0.0.0", port=port)
