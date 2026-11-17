from __future__ import annotations

import json

import boto3
import httpx
import pytest
from botocore.stub import Stubber
from pydantic import SecretStr

from app.ops.bootstrap import (
    DEAD_LETTER_EXCHANGE,
    WORK_POLICY_NAME,
    BrokerTarget,
    broker_target,
    ensure_broker_policy,
    ensure_bucket,
)
from tests.conftest import make_settings


@pytest.fixture
def s3() -> object:
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        endpoint_url="http://objectstore.test:8333",
    )


def test_existing_bucket_is_left_alone(s3: object) -> None:
    with Stubber(s3) as stub:  # type: ignore[arg-type]
        stub.add_response("head_bucket", {}, {"Bucket": "codehover"})
        assert ensure_bucket(s3, "codehover", "us-east-1") is False
        stub.assert_no_pending_responses()


def test_missing_bucket_is_created_without_location_in_us_east_1(s3: object) -> None:
    with Stubber(s3) as stub:  # type: ignore[arg-type]
        stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404)
        stub.add_response("create_bucket", {}, {"Bucket": "codehover"})
        assert ensure_bucket(s3, "codehover", "us-east-1") is True
        stub.assert_no_pending_responses()


def test_forbidden_bucket_is_an_error_not_a_create(s3: object) -> None:
    from botocore.exceptions import ClientError

    with Stubber(s3) as stub:  # type: ignore[arg-type]
        stub.add_client_error("head_bucket", service_error_code="403", http_status_code=403)
        with pytest.raises(ClientError):
            ensure_bucket(s3, "codehover", "us-east-1")


def test_broker_target_parses_vhost_and_credentials() -> None:
    settings = make_settings(
        amqp_url=SecretStr("amqp://codehover:p%40ss@rabbitmq:5672/codehover"),
        rabbitmq_management_url="http://rabbitmq:15672/",
    )
    assert broker_target(settings) == BrokerTarget("http://rabbitmq:15672", "codehover", "codehover", "p@ss")
    assert broker_target(make_settings()) is None


def test_broker_policy_declares_dlx_and_policy() -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(201)

    target = BrokerTarget("http://rabbitmq:15672", "codehover", "u", "p")
    with httpx.Client(base_url=target.base_url, transport=httpx.MockTransport(handler)) as client:
        ensure_broker_policy(client, target)
    assert [(m, p) for m, p, _ in calls] == [
        ("PUT", f"/api/exchanges/codehover/{DEAD_LETTER_EXCHANGE}"),
        ("PUT", f"/api/policies/codehover/{WORK_POLICY_NAME}"),
    ]
    policy = calls[1][2]
    assert policy["apply-to"] == "quorum_queues"
    assert policy["definition"] == {"dead-letter-exchange": DEAD_LETTER_EXCHANGE, "delivery-limit": 20}


def test_broker_policy_failure_raises() -> None:
    target = BrokerTarget("http://rabbitmq:15672", "codehover", "u", "p")
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    with (
        httpx.Client(base_url=target.base_url, transport=transport) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        ensure_broker_policy(client, target)


def test_main_runs_both_steps_and_logs_cleanly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Exercise main() itself: its log calls once used a reserved LogRecord key and crashed."""
    import logging

    from app.ops import bootstrap

    settings = make_settings(
        amqp_url=SecretStr("amqp://u:p@rabbitmq:5672/codehover"),
        rabbitmq_management_url="http://rabbitmq:15672",
    )
    seen: list[str] = []
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "configure_logging", lambda level: None)
    monkeypatch.setattr(bootstrap, "s3_client", lambda s: object())
    monkeypatch.setattr(
        bootstrap, "ensure_bucket", lambda client, bucket, region: seen.append("bucket") or True
    )
    monkeypatch.setattr(bootstrap, "ensure_broker_policy", lambda client, target: seen.append("broker"))
    with caplog.at_level(logging.INFO, logger="app.ops.bootstrap"):
        assert bootstrap.main() == 0
    assert seen == ["bucket", "broker"]
    assert [r.getMessage() for r in caplog.records] == ["bucket ready", "broker policy ready"]
