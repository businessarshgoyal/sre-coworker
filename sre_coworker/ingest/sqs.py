from __future__ import annotations

import json
from collections.abc import AsyncIterator

import aioboto3

from sre_coworker.ingest import Envelope


class SQSQueue:
    """AWS SQS long-poll consumer. Datadog and Sentry can both target SNS -> SQS natively.

    Set message attribute `source` (String) to pick the adapter; SNS-wrapped bodies are
    unwrapped automatically.
    """

    def __init__(self, queue_url: str, region: str | None = None, wait_seconds: int = 20) -> None:
        self.queue_url = queue_url
        self.region = region
        self.wait_seconds = wait_seconds
        self.session = aioboto3.Session()

    async def messages(self) -> AsyncIterator[Envelope]:
        async with self.session.client("sqs", region_name=self.region) as sqs:
            while True:
                resp = await sqs.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=self.wait_seconds,
                    MessageAttributeNames=["source"],
                )
                for m in resp.get("Messages", []):
                    handle = m["ReceiptHandle"]
                    body = json.loads(m["Body"])
                    source = (
                        m.get("MessageAttributes", {}).get("source", {}).get("StringValue")
                        or "generic"
                    )
                    if body.get("Type") == "Notification" and "Message" in body:  # SNS envelope
                        attrs = body.get("MessageAttributes", {})
                        source = attrs.get("source", {}).get("Value", source)
                        body = json.loads(body["Message"])

                    async def ack(handle: str = handle) -> None:
                        await sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=handle)

                    async def nack(handle: str = handle) -> None:
                        await sqs.change_message_visibility(
                            QueueUrl=self.queue_url, ReceiptHandle=handle, VisibilityTimeout=0
                        )

                    yield Envelope(
                        source=source,
                        payload=body,
                        ack=ack,
                        nack=nack,
                        message_id=m.get("MessageId"),
                    )
