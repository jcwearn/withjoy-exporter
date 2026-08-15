from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import github_sync


def _key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


# Generating a key is slow enough to be worth doing once for the module.
PRIVATE_PEM, PUBLIC_PEM = _key_pair()

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _run(run_id=1, created="2026-07-01T12:00:00Z", status="completed", conclusion="success"):
    return {
        "id": run_id,
        "created_at": created,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


def test_configured_requires_all_three():
    with (
        patch.object(github_sync, "APP_ID", "1"),
        patch.object(github_sync, "INSTALLATION_ID", "2"),
        patch.object(github_sync, "PRIVATE_KEY", "key"),
    ):
        assert github_sync.configured() is True
    with (
        patch.object(github_sync, "APP_ID", "1"),
        patch.object(github_sync, "INSTALLATION_ID", ""),
        patch.object(github_sync, "PRIVATE_KEY", "key"),
    ):
        assert github_sync.configured() is False


def test_app_jwt_claims():
    with (
        patch.object(github_sync, "APP_ID", "123456"),
        patch.object(github_sync, "PRIVATE_KEY", PRIVATE_PEM),
    ):
        token = github_sync._app_jwt(NOW)
    claims = jwt.decode(token, PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "123456"
    # Backdated a minute so GitHub never sees a future-issued token.
    assert claims["iat"] == int(NOW.timestamp()) - 60
    assert claims["exp"] == int(NOW.timestamp()) + 540


def test_installation_token_caches_until_near_expiry():
    body = {"token": "ghs_abc", "expires_at": "2026-07-01T13:00:00Z"}
    with (
        patch.object(github_sync, "APP_ID", "1"),
        patch.object(github_sync, "INSTALLATION_ID", "2"),
        patch.object(github_sync, "PRIVATE_KEY", PRIVATE_PEM),
        patch.object(github_sync, "_request", return_value=(201, body)) as request,
    ):
        github_sync._token.clear()
        assert github_sync.installation_token(NOW) == "ghs_abc"
        # Still valid half an hour later, so no second exchange.
        assert github_sync.installation_token(NOW + timedelta(minutes=30)) == "ghs_abc"
        assert request.call_count == 1
        # Inside the one-minute safety margin, it re-exchanges.
        github_sync.installation_token(NOW + timedelta(minutes=59, seconds=30))
        assert request.call_count == 2
    github_sync._token.clear()


def test_installation_token_needs_configuration():
    with patch.object(github_sync, "APP_ID", ""):
        github_sync._token.clear()
        with pytest.raises(github_sync.GitHubError, match="not configured"):
            github_sync.installation_token(NOW)


def test_dispatch_accepts_204():
    with (
        patch.object(github_sync, "installation_token", return_value="t"),
        patch.object(github_sync, "_request", return_value=(204, {})) as request,
    ):
        github_sync.dispatch_workflow()
    method, path = request.call_args.args[0], request.call_args.args[1]
    assert method == "POST"
    assert path.endswith("/dispatches")
    assert request.call_args.args[3] == {"ref": github_sync.REF}


def test_dispatch_rejects_unexpected_status():
    with (
        patch.object(github_sync, "installation_token", return_value="t"),
        patch.object(github_sync, "_request", return_value=(200, {})),
        pytest.raises(github_sync.GitHubError, match="Unexpected dispatch status"),
    ):
        github_sync.dispatch_workflow()


def test_find_run_ignores_runs_older_than_the_dispatch():
    with patch.object(github_sync, "_runs", return_value=[_run(created="2026-07-01T11:00:00Z")]):
        assert github_sync.find_run(NOW) is None


def test_find_run_matches_a_run_created_after_the_dispatch():
    runs = [
        _run(run_id=3, created="2026-07-01T12:00:04Z"),
        _run(run_id=2, created="2026-07-01T11:00:00Z"),
    ]
    with patch.object(github_sync, "_runs", return_value=runs):
        assert github_sync.find_run(NOW)["run_id"] == 3


def test_find_run_tolerates_clock_skew():
    # Stamped just before our own clock read; the slack has to catch it.
    with patch.object(
        github_sync, "_runs", return_value=[_run(run_id=4, created="2026-07-01T11:59:58Z")]
    ):
        assert github_sync.find_run(NOW)["run_id"] == 4


def test_summarize_run_maps_github_states():
    assert github_sync.summarize_run(_run())["state"] == "succeeded"
    assert github_sync.summarize_run(_run(conclusion="failure"))["state"] == "failed"
    assert github_sync.summarize_run(_run(conclusion="cancelled"))["state"] == "failed"
    assert (
        github_sync.summarize_run(_run(status="in_progress", conclusion=None))["state"] == "running"
    )
    assert github_sync.summarize_run(_run(status="queued", conclusion=None))["state"] == "running"


def test_summarize_run_carries_the_link():
    summary = github_sync.summarize_run(_run(run_id=9))
    assert summary["run_id"] == 9
    assert summary["html_url"].endswith("/9")
