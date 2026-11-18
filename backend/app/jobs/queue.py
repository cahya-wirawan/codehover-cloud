"""RabbitMQ topology (04, 22): durable direct exchange, quorum work queue covered by the bootstrap DLX policy."""

from __future__ import annotations

from typing import Any

import aio_pika

from app.jobs.envelope import EXCHANGE, QUEUE, ROUTING_KEY


async def topology(channel: Any) -> tuple[Any, Any]:
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    queue = await channel.declare_queue(QUEUE, durable=True, arguments={"x-queue-type": "quorum"})
    await queue.bind(exchange, routing_key=ROUTING_KEY)
    return exchange, queue
