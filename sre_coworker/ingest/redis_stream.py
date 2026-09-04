from __future__ import annotations

import json
from collections.abc import AsyncIterator

import redis.asyncio as aioredis

from sre_coworker.ingest import Envelope


class RedisStreamQueue:
    """Redis Streams consumer group. Handy for self-hosted setups and for local testing.

    Publish with: XADD alerts * source datadog payload '{...json...}'
    """

    def __init__(
        self,
        url: str,
        stream: str = "alerts",
        group: str = "sre-coworker",
        consumer: str = "worker-1",
        block_ms: int = 5000,
    ) -> None:
        self.redis: aioredis.Redis = aioredis.Redis.from_url(url, decode_responses=True)
        self.stream, self.group, self.consumer, self.block_ms = stream, group, consumer, block_ms

    async def _ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def messages(self) -> AsyncIterator[Envelope]:
        await self._ensure_group()
        while True:
            batches = await self.redis.xreadgroup(
                self.group, self.consumer, {self.stream: ">"}, count=10, block=self.block_ms
            )
            for _stream, entries in batches or []:
                for msg_id, fields in entries:

                    async def ack(msg_id: str = msg_id) -> None:
                        await self.redis.xack(self.stream, self.group, msg_id)

                    async def nack(msg_id: str = msg_id) -> None:
                        return None  # left pending; redelivered via XAUTOCLAIM by another consumer

                    yield Envelope(
                        source=fields.get("source", "generic"),
                        payload=json.loads(fields["payload"]),
                        ack=ack,
                        nack=nack,
                        message_id=msg_id,
                    )
