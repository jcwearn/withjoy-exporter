import os
import sys
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string
from kubernetes import client, config
from kubernetes.client.rest import ApiException

NAMESPACE = os.environ.get("NAMESPACE", "withjoy-exporter")
CRONJOB_NAME = os.environ.get("CRONJOB_NAME", "withjoy-exporter")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WithJoy Exporter</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; max-width: 480px;
         margin: 4rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.4rem; }
  button { font-size: 1.1rem; padding: 0.6rem 1.4rem; border: none; border-radius: 6px;
           background: #2563eb; color: #fff; cursor: pointer; }
  button:disabled { background: #93c5fd; cursor: default; }
  #status { margin-top: 1.5rem; padding: 1rem; border-radius: 6px; background: #f3f4f6; }
  .running { color: #b45309; } .succeeded { color: #15803d; } .failed { color: #b91c1c; }
</style>
</head>
<body>
<h1>WithJoy Guest List Exporter</h1>
<p>Exports the guest list to Google Sheets. Runs automatically every morning;
use the button to run it now.</p>
<button id="trigger">Run export now</button>
<div id="status">Loading status…</div>
<script>
const btn = document.getElementById("trigger");
const box = document.getElementById("status");

function render(s) {
  if (s.state === "none") { box.textContent = "No runs found."; btn.disabled = false; return; }
  const kind = s.manual ? "manual" : "scheduled";
  let line = `Last run (${kind}): <span class="${s.state}">${s.state}</span><br>` +
             `Job: ${s.job_name}<br>Started: ${s.started_at || "—"}`;
  if (s.finished_at) line += `<br>Finished: ${s.finished_at}`;
  box.innerHTML = line;
  btn.disabled = s.state === "running";
}

async function refresh() {
  try {
    const res = await fetch("/api/status");
    render(await res.json());
  } catch (e) {
    box.textContent = "Failed to load status: " + e;
  }
}

btn.addEventListener("click", async () => {
  btn.disabled = true;
  const res = await fetch("/api/trigger", { method: "POST" });
  const body = await res.json();
  if (!res.ok) alert(body.error || "Trigger failed");
  await refresh();
});

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


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def summarize_jobs(jobs: list) -> dict:
    if not jobs:
        return {"state": "none"}
    latest = max(jobs, key=lambda j: j.metadata.creation_timestamp)
    status = latest.status
    if status.succeeded:
        state = "succeeded"
    elif status.failed:
        state = "failed"
    else:
        state = "running"
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


@app.get("/")
def index():
    return render_template_string(PAGE)


@app.get("/api/status")
def api_status():
    jobs = batch_api().list_namespaced_job(NAMESPACE).items
    return jsonify(summarize_jobs(jobs))


@app.post("/api/trigger")
def api_trigger():
    api = batch_api()
    jobs = api.list_namespaced_job(NAMESPACE).items
    running = active_job(jobs)
    if running is not None:
        return jsonify({"error": f"A run is already in progress ({running.metadata.name})."}), 409
    try:
        cronjob = api.read_namespaced_cron_job(CRONJOB_NAME, NAMESPACE)
    except ApiException as exc:
        return jsonify({"error": f"Could not read CronJob {CRONJOB_NAME}: {exc.reason}"}), 500
    job = build_manual_job(cronjob, datetime.now(timezone.utc))
    api.create_namespaced_job(NAMESPACE, job)
    return jsonify({"job_name": job.metadata.name}), 202


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"serving on 0.0.0.0:{port} (namespace={NAMESPACE}, cronjob={CRONJOB_NAME})", file=sys.stderr)
    app.run(host="0.0.0.0", port=port)
