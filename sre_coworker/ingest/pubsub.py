from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from google.cloud import pubsub_v1

from sre_coworker.ingest import Envelope


class PubSubQueue:
    """Google Cloud Pub/Sub pull subscription (no inbound port, IAM-authenticated).

    Route alerts in via a Pub/Sub topic: Datadog/Sentry -> Cloud Function or Eventarc ->
    topic, or publish directly from your monitoring pipeline. Set the `source` attribute
    on the message to pick the adapter.
    """

    def __init__(self, project: str, subscription: str, max_messages: int = 10) -> None:
        self.client = pubsub_v1.SubscriberClient()
        self.path = self.client.subscription_path(project, subscription)
        self.max_messages = max_messages

    async def messages(self) -> AsyncIterator[Envelope]:
        loop = asyncio.get_running_loop()
        while True:
            resp = await loop.run_in_executor(
                None,
                lambda: self.client.pull(
                    request={"subscription": self.path, "max_messages": self.max_messages},
                    timeout=30,
                ),
            )
            if not resp.received_messages:
                await asyncio.sleep(1)
                continue
            for rm in resp.received_messages:
                ack_id = rm.ack_id

                async def ack(ack_id: str = ack_id) -> None:
                    await loop.run_in_executor(
                        None,
                        lambda: self.client.acknowledge(
                            request={"subscription": self.path, "ack_ids": [ack_id]}
                        ),
                    )

                async def nack(ack_id: str = ack_id) -> None:
                    await loop.run_in_executor(
                        None,
                        lambda: self.client.modify_ack_deadline(
                            request={
                                "subscription": self.path,
                                "ack_ids": [ack_id],
                                "ack_deadline_seconds": 0,
                            }
                        ),
                    )

                yield Envelope(
                    source=rm.message.attributes.get("source", "generic"),
                    payload=json.loads(rm.message.data.decode()),
                    ack=ack,
                    nack=nack,
                    message_id=rm.message.message_id,
                )
