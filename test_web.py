from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from kubernetes import client

import web


def _job(name, created, active=None, succeeded=None, failed=None, labels=None,
         start_time=None, completion_time=None):
    return client.V1Job(
        metadata=client.V1ObjectMeta(
            name=name, creation_timestamp=created, labels=labels or {}
        ),
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


T1 = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)
T2 = datetime(2026, 7, 2, 6, 0, tzinfo=timezone.utc)


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
    now = datetime(2026, 7, 3, 12, 30, 45, tzinfo=timezone.utc)
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


def test_status_endpoint():
    api = _mock_api([_job("run", T1, succeeded=1, completion_time=T2)])
    with patch.object(web, "batch_api", return_value=api):
        res = web.app.test_client().get("/api/status")
    assert res.status_code == 200
    assert res.get_json()["state"] == "succeeded"
