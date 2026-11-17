"""Idempotent infrastructure bootstrap: `python -m app.ops.bootstrap` (22 "Local workflow" step 5).

- Object storage: ensure the private artifact bucket exists.
- RabbitMQ: ensure the dead-letter exchange and the policy that attaches it (plus an explicit
  delivery limit) to every work quorum queue. RabbitMQ 4 drops over-limit messages unless a
  dead-letter exchange is set (04, 22). Queues themselves are declared by the application (M1).

Database roles are not created here: they come from deployment/init-db.sh locally and from the
production bootstrap elsewhere; grants come from migrations. Safe to run repeatedly.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import boto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.logging import configure_logging
from app.core.settings import Settings, get_settings

logger = logging.getLogger("app.ops.bootstrap")

WORK_QUEUE_PATTERN = r"^codehover\.work\."
DEAD_LETTER_EXCHANGE = "codehover.dlx"
WORK_POLICY_NAME = "codehover-work-dlx"
DELIVERY_LIMIT = 20  # RabbitMQ 4 default, set explicitly so it is visible and reviewed


def retry[T](action: Callable[[], T], *, what: str, attempts: int = 30, delay_seconds: float = 2.0) -> T:
    """Retry while a dependency is still starting (object store and broker start in parallel)."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except (EndpointConnectionError, httpx.TransportError) as exc:
            if attempt == attempts:
                raise
            logger.info(
                "waiting for dependency", extra={"dependency": what, "error_type": type(exc).__name__}
            )
            time.sleep(delay_seconds)
    raise AssertionError("unreachable")


def s3_client(settings: Settings) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id.get_secret_value() if settings.s3_access_key_id else None,
        aws_secret_access_key=(
            settings.s3_secret_access_key.get_secret_value() if settings.s3_secret_access_key else None
        ),
        config=BotoConfig(s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"}),
    )


def ensure_bucket(client: Any, bucket: str, region: str) -> bool:
    """Create the bucket if missing. Returns True when it was created."""
    try:
        client.head_bucket(Bucket=bucket)
        return False
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        missing = status == 404 or exc.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket")
        if not missing:
            raise  # 403 and friends: wrong credentials or someone else's bucket, never "create"
    params: dict[str, Any] = {"Bucket": bucket}
    if region != "us-east-1":
        params["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        client.create_bucket(**params)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            return False
        raise
    return True


@dataclass(frozen=True)
class BrokerTarget:
    base_url: str
    vhost: str
    username: str
    password: str


def broker_target(settings: Settings) -> BrokerTarget | None:
    if not settings.rabbitmq_management_url or not settings.amqp_url:
        return None
    amqp = urlsplit(settings.amqp_url.get_secret_value())
    vhost = unquote(amqp.path[1:]) if amqp.path not in ("", "/") else "/"
    return BrokerTarget(
        base_url=settings.rabbitmq_management_url.rstrip("/"),
        vhost=vhost,
        username=unquote(amqp.username or ""),
        password=unquote(amqp.password or ""),
    )


def ensure_broker_policy(client: httpx.Client, target: BrokerTarget) -> None:
    vhost = quote(target.vhost, safe="")
    exchange = client.put(
        f"/api/exchanges/{vhost}/{quote(DEAD_LETTER_EXCHANGE, safe='')}",
        json={"type": "topic", "durable": True, "auto_delete": False, "internal": False, "arguments": {}},
    )
    exchange.raise_for_status()
    policy = client.put(
        f"/api/policies/{vhost}/{WORK_POLICY_NAME}",
        json={
            "pattern": WORK_QUEUE_PATTERN,
            "apply-to": "quorum_queues",
            "priority": 0,
            "definition": {"dead-letter-exchange": DEAD_LETTER_EXCHANGE, "delivery-limit": DELIVERY_LIMIT},
        },
    )
    policy.raise_for_status()


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    client = s3_client(settings)
    created = retry(
        lambda: ensure_bucket(client, settings.s3_bucket, settings.s3_region), what="object store"
    )
    logger.info("bucket ready", extra={"bucket": settings.s3_bucket, "bucket_created": created})

    target = broker_target(settings)
    if target is None:
        logger.warning("RABBITMQ_MANAGEMENT_URL or AMQP_URL not set; broker policy skipped")
    else:
        with httpx.Client(
            base_url=target.base_url, auth=(target.username, target.password), timeout=10.0
        ) as http:
            retry(lambda: ensure_broker_policy(http, target), what="broker")
        logger.info("broker policy ready", extra={"vhost": target.vhost, "policy": WORK_POLICY_NAME})
    return 0


if __name__ == "__main__":
    sys.exit(main())
