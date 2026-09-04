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
