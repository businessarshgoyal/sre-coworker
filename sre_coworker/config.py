from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All external credentials are optional; missing ones force dry-run for that action."""

    model_config = SettingsConfigDict(env_prefix="SRE_", env_file=".env", extra="ignore")

    dry_run: bool = True
    auto_approve_min_confidence: float = 2.0  # >1 disables auto-approval entirely
    min_confidence_for_fix_session: float = 0.5
    runbooks_dir: Path = Path("runbooks")
    known_issues_file: Path = Path("examples/known_issues.json")
    deploy_window_minutes: int = 240

    github_repo: str | None = None  # owner/name
    github_token: str | None = None

    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_project_key: str = "ENG"

    devin_api_key: str | None = None
    devin_api_base: str = "https://api.devin.ai/v1"

    # Inbound webhook (optional; disabled unless a secret is set)
    webhook_secret: str | None = None

    # Pull-based ingestion
    queue_backend: str | None = None  # pubsub | sqs | redis
    pubsub_project: str | None = None
    pubsub_subscription: str | None = None
    sqs_queue_url: str | None = None
    sqs_region: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    redis_stream: str = "alerts"

    def queue_kwargs(self) -> dict[str, str]:
        if self.queue_backend == "pubsub":
            if not (self.pubsub_project and self.pubsub_subscription):
                raise ValueError("SRE_PUBSUB_PROJECT and SRE_PUBSUB_SUBSCRIPTION are required")
            return {"project": self.pubsub_project, "subscription": self.pubsub_subscription}
        if self.queue_backend == "sqs":
            if not self.sqs_queue_url:
                raise ValueError("SRE_SQS_QUEUE_URL is required")
            kw = {"queue_url": self.sqs_queue_url}
            if self.sqs_region:
                kw["region"] = self.sqs_region
            return kw
        if self.queue_backend == "redis":
            return {"url": self.redis_url, "stream": self.redis_stream}
        raise ValueError(f"unknown SRE_QUEUE_BACKEND {self.queue_backend!r}")

    @property
    def jira_live(self) -> bool:
        return not self.dry_run and bool(
            self.jira_base_url and self.jira_email and self.jira_api_token
        )

    @property
    def devin_live(self) -> bool:
        return not self.dry_run and bool(self.devin_api_key)

    @property
    def github_live(self) -> bool:
        return bool(self.github_repo)


settings = Settings()
