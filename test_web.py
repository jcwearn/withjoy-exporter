from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from kubernetes import client

import web


def _job(
    name,
    created,
    active=None,
    succeeded=None,
    failed=None,
    labels=None,
    start_time=None,
    completion_time=None,
):
    return client.V1Job(
        metadata=client.V1ObjectMeta(name=name, creation_timestamp=created, labels=labels or {}),
        status=client.V1JobStatus(
            active=active,
            succeeded=succeeded,
            failed=failed,
            start_time=start_time or created,
            completion_time=completion_time,
        ),
    )


def _cronjob(name="withjoy-exporter", namespace="withjoy-exporter"):
    return client.V1CronJob(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1CronJobSpec(
            schedule="0 6 * * *",
            job_template=client.V1JobTemplateSpec(
                metadata=client.V1ObjectMeta(labels={"app": name}),
                spec=client.V1JobSpec(
                    template=client.V1PodTemplateSpec(
                        spec=client.V1PodSpec(
                            restart_policy="Never",
                            containers=[client.V1Container(name=name, image="img:tag")],
                        )
                    )
                ),
            ),
        ),
    )


T1 = datetime(2026, 7, 1, 6, 0, tzinfo=UTC)
T2 = datetime(2026, 7, 2, 6, 0, tzinfo=UTC)


def test_summarize_no_jobs():
    assert web.summarize_jobs([]) == {"state": "none"}


def test_summarize_picks_newest_job():
    jobs = [
        _job("old", T1, succeeded=1, completion_time=T1),
        _job("new", T2, failed=1),
    ]
    summary = web.summarize_jobs(jobs)
    assert summary["job_name"] == "new"
    assert summary["state"] == "failed"


def test_summarize_succeeded_manual():
    jobs = [_job("run", T1, succeeded=1, completion_time=T2, labels={"trigger": "manual"})]
    summary = web.summarize_jobs(jobs)
    assert summary["state"] == "succeeded"
    assert summary["manual"] is True
    assert summary["started_at"] == T1.isoformat()
    assert summary["finished_at"] == T2.isoformat()


def test_summarize_running():
    summary = web.summarize_jobs([_job("run", T1, active=1)])
    assert summary["state"] == "running"
    assert summary["manual"] is False
    assert summary["finished_at"] is None


def test_active_job():
    idle = _job("done", T1, succeeded=1)
    running = _job("busy", T2, active=1)
    assert web.active_job([idle]) is None
    assert web.active_job([idle, running]) is running


def test_build_manual_job():
    now = datetime(2026, 7, 3, 12, 30, 45, tzinfo=UTC)
    job = web.build_manual_job(_cronjob(), now)
    assert job.metadata.name == "withjoy-exporter-manual-20260703123045"
    assert job.metadata.namespace == "withjoy-exporter"
    assert job.metadata.labels == {"app": "withjoy-exporter", "trigger": "manual"}
    assert job.metadata.annotations == {"cronjob.kubernetes.io/instantiate": "manual"}
    assert job.spec.ttl_seconds_after_finished == 86400
    assert job.spec.template.spec.containers[0].image == "img:tag"


def _mock_api(jobs, cronjob=None):
    api = MagicMock()
    api.list_namespaced_job.return_value = MagicMock(items=jobs)
    if cronjob is not None:
        api.read_namespaced_cron_job.return_value = cronjob
    return api


def test_trigger_rejects_when_job_active():
    api = _mock_api([_job("busy", T1, active=1)])
    with patch.object(web, "batch_api", return_value=api):
        res = web.app.test_client().post("/api/trigger")
    assert res.status_code == 409
    assert "busy" in res.get_json()["error"]
    api.create_namespaced_job.assert_not_called()


def test_trigger_creates_job():
    api = _mock_api([_job("done", T1, succeeded=1)], cronjob=_cronjob())
    with patch.object(web, "batch_api", return_value=api):
        res = web.app.test_client().post("/api/trigger")
    assert res.status_code == 202
    namespace, job = api.create_namespaced_job.call_args.args
    assert namespace == "withjoy-exporter"
    assert job.metadata.name == res.get_json()["job_name"]
    assert job.metadata.name.startswith("withjoy-exporter-manual-")
    assert job.metadata.labels["trigger"] == "manual"


def _reset_state():
    """Clear the module globals that survive between requests."""
    web._schedule.clear()
    web._set_chain(state="idle")


def test_status_endpoint():
    _reset_state()
    api = _mock_api([_job("run", T1, succeeded=1, completion_time=T2)])
    with (
        patch.object(web, "batch_api", return_value=api),
        patch.object(web.github_sync, "configured", return_value=False),
    ):
        res = web.app.test_client().get("/api/status")
    assert res.status_code == 200
    body = res.get_json()
    assert body["export"]["state"] == "succeeded"
    assert body["schedule"]["state"] == "unconfigured"
    assert body["chain"]["state"] == "idle"


def test_index_renders_all_three_buttons():
    res = web.app.test_client().get("/")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    for button_id in ('id="export"', 'id="sync"', 'id="both"'):
        assert button_id in html


# --- schedule sync ---------------------------------------------------------


def test_sync_schedule_dispatches():
    _reset_state()
    with (
        patch.object(web.github_sync, "configured", return_value=True),
        patch.object(web.github_sync, "dispatch_workflow") as dispatch,
    ):
        res = web.app.test_client().post("/api/sync-schedule")
    assert res.status_code == 202
    dispatch.assert_called_once()
    # Dispatch gives us no run id, so we remember when to start looking.
    assert web._schedule["awaiting_since"] is not None
    _reset_state()


def test_sync_schedule_requires_configuration():
    _reset_state()
    with patch.object(web.github_sync, "configured", return_value=False):
        res = web.app.test_client().post("/api/sync-schedule")
    assert res.status_code == 503


def test_sync_schedule_surfaces_github_errors():
    _reset_state()
    with (
        patch.object(web.github_sync, "configured", return_value=True),
        patch.object(
            web.github_sync,
            "dispatch_workflow",
            side_effect=web.github_sync.GitHubError("boom"),
        ),
    ):
        res = web.app.test_client().post("/api/sync-schedule")
    assert res.status_code == 502
    assert "boom" in res.get_json()["error"]


def test_schedule_state_caches_while_idle():
    _reset_state()
    run = {"state": "succeeded", "run_id": 5, "html_url": "u", "created_at": "t"}
    with (
        patch.object(web.github_sync, "configured", return_value=True),
        patch.object(web.github_sync, "latest_run", return_value=run) as latest,
    ):
        first = web.schedule_state(T1)
        second = web.schedule_state(T1 + timedelta(seconds=10))
    assert first["run_id"] == 5
    assert second["run_id"] == 5
    assert latest.call_count == 1
    assert "checked_at" not in second
    _reset_state()


def test_schedule_state_refetches_a_running_run():
    _reset_state()
    running = {"state": "running", "run_id": 5, "html_url": "u", "created_at": "t"}
    with (
        patch.object(web.github_sync, "configured", return_value=True),
        patch.object(web.github_sync, "latest_run", return_value=running),
        patch.object(web.github_sync, "run_status", return_value=running) as status,
    ):
        web.schedule_state(T1)
        web.schedule_state(T1 + timedelta(seconds=3))
    # A run in flight is never served from cache.
    status.assert_called_once_with(5)
    _reset_state()


# --- run both --------------------------------------------------------------


def test_chain_aborts_without_dispatching_when_the_export_fails():
    _reset_state()
    api = MagicMock()
    api.read_namespaced_job.return_value = _job("run", T1, failed=1)
    with patch.object(web.github_sync, "dispatch_workflow") as dispatch:
        web.run_chain(api, "run", sleep=lambda _: None)
    dispatch.assert_not_called()
    state = web.chain_state()
    assert state["state"] == "aborted"
    assert "was not started" in state["error"]
    _reset_state()


def test_chain_waits_for_the_export_then_dispatches():
    _reset_state()
    api = MagicMock()
    api.read_namespaced_job.side_effect = [
        _job("run", T1, active=1),
        _job("run", T1, active=1),
        _job("run", T1, succeeded=1),
    ]
    with patch.object(web.github_sync, "dispatch_workflow") as dispatch:
        web.run_chain(api, "run", sleep=lambda _: None)
    dispatch.assert_called_once()
    assert web.chain_state()["state"] == "done"
    assert api.read_namespaced_job.call_count == 3
    _reset_state()


def test_chain_gives_up_on_an_export_that_never_finishes():
    _reset_state()
    api = MagicMock()
    api.read_namespaced_job.return_value = _job("run", T1, active=1)
    with patch.object(web.github_sync, "dispatch_workflow") as dispatch:
        web.run_chain(api, "run", sleep=lambda _: None, attempts=3)
    dispatch.assert_not_called()
    assert web.chain_state()["state"] == "aborted"
    assert "Gave up" in web.chain_state()["error"]
    _reset_state()


def test_chain_aborts_when_the_dispatch_fails():
    _reset_state()
    api = MagicMock()
    api.read_namespaced_job.return_value = _job("run", T1, succeeded=1)
    with patch.object(
        web.github_sync,
        "dispatch_workflow",
        side_effect=web.github_sync.GitHubError("no token"),
    ):
        web.run_chain(api, "run", sleep=lambda _: None)
    assert web.chain_state()["state"] == "aborted"
    assert "no token" in web.chain_state()["error"]
    _reset_state()


def test_run_both_creates_the_job_and_starts_the_chain():
    _reset_state()
    api = _mock_api([_job("done", T1, succeeded=1)], cronjob=_cronjob())
    with (
        patch.object(web, "batch_api", return_value=api),
        patch.object(web.github_sync, "configured", return_value=True),
        patch.object(web, "threading") as threading_mod,
    ):
        res = web.app.test_client().post("/api/run-both")
    assert res.status_code == 202
    api.create_namespaced_job.assert_called_once()
    kwargs = threading_mod.Thread.call_args.kwargs
    assert kwargs["target"] is web.run_chain
    assert kwargs["daemon"] is True
    assert kwargs["args"][1] == res.get_json()["job_name"]
    _reset_state()


def test_run_both_rejects_when_an_export_is_already_running():
    _reset_state()
    api = _mock_api([_job("busy", T1, active=1)])
    with (
        patch.object(web, "batch_api", return_value=api),
        patch.object(web.github_sync, "configured", return_value=True),
        patch.object(web, "threading") as threading_mod,
    ):
        res = web.app.test_client().post("/api/run-both")
    assert res.status_code == 409
    api.create_namespaced_job.assert_not_called()
    threading_mod.Thread.assert_not_called()
    _reset_state()


def test_run_both_rejects_a_second_chain():
    _reset_state()
    web._set_chain(state="waiting-export", job_name="busy")
    with patch.object(web.github_sync, "configured", return_value=True):
        res = web.app.test_client().post("/api/run-both")
    assert res.status_code == 409
    assert "chained run" in res.get_json()["error"]
    _reset_state()


def test_run_both_requires_configuration():
    _reset_state()
    with patch.object(web.github_sync, "configured", return_value=False):
        res = web.app.test_client().post("/api/run-both")
    assert res.status_code == 503
