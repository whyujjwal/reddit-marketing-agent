"""Runtime settings, read from environment variables and optional .env files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from . import __version__

DEFAULT_DATA_DIR = Path.home() / ".reddit-marketing-agent"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def load_env_files() -> None:
    """Load ``.env`` from the data dir and the working directory (env vars always win)."""
    data_dir = Path(os.environ.get("RMA_DATA_DIR", DEFAULT_DATA_DIR)).expanduser()
    for candidate in (data_dir / ".env", Path.cwd() / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


@dataclass
class Settings:
    client_id: str | None = None
    client_secret: str | None = None
    username: str | None = None
    password: str | None = None
    refresh_token: str | None = None
    user_agent: str | None = None

    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR)

    # Safety rails
    posting_enabled: bool = False
    approval_mode: str = "chat"  # "chat" (approve in Claude) or "terminal" (CLI only)
    max_actions_per_day: int = 5
    min_minutes_between_actions: int = 10
    subreddit_promo_cooldown_hours: int = 72
    max_promo_ratio: float = 0.10

    # Networking
    request_timeout: float = 20.0
    max_rate_wait_seconds: float = 45.0  # stay under common 60s MCP client timeouts
    cache_ttl_seconds: int = 600

    @classmethod
    def from_env(cls, load_files: bool = True) -> Settings:
        if load_files:
            load_env_files()
        env = os.environ.get
        approval = (env("RMA_APPROVAL_MODE") or "chat").strip().lower()
        return cls(
            client_id=env("REDDIT_CLIENT_ID") or None,
            client_secret=env("REDDIT_CLIENT_SECRET") or None,
            username=env("REDDIT_USERNAME") or None,
            password=env("REDDIT_PASSWORD") or None,
            refresh_token=env("REDDIT_REFRESH_TOKEN") or None,
            user_agent=env("REDDIT_USER_AGENT") or None,
            data_dir=Path(env("RMA_DATA_DIR") or DEFAULT_DATA_DIR).expanduser(),
            posting_enabled=_bool(env("RMA_POSTING_ENABLED"), False),
            approval_mode=approval if approval in {"chat", "terminal"} else "chat",
            max_actions_per_day=_int(env("RMA_MAX_ACTIONS_PER_DAY"), 5),
            min_minutes_between_actions=_int(env("RMA_MIN_MINUTES_BETWEEN_ACTIONS"), 10),
            subreddit_promo_cooldown_hours=_int(env("RMA_SUBREDDIT_COOLDOWN_HOURS"), 72),
            max_promo_ratio=_float(env("RMA_MAX_PROMO_RATIO"), 0.10),
            request_timeout=_float(env("RMA_REQUEST_TIMEOUT"), 20.0),
            max_rate_wait_seconds=_float(env("RMA_MAX_RATE_WAIT"), 45.0),
            cache_ttl_seconds=_int(env("RMA_CACHE_TTL"), 600),
        )

    # -- derived -----------------------------------------------------------------
    @property
    def has_app_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def has_user_auth(self) -> bool:
        return self.has_app_credentials and bool(self.refresh_token or (self.username and self.password))

    @property
    def mode(self) -> str:
        """``oauth-user`` (read + write), ``oauth-app`` (full read), or ``rss`` (limited read)."""
        if self.has_user_auth:
            return "oauth-user"
        if self.has_app_credentials:
            return "oauth-app"
        return "rss"

    @property
    def effective_user_agent(self) -> str:
        if self.user_agent:
            return self.user_agent
        who = f"by /u/{self.username}" if self.username else "open-source MCP server"
        return f"python:reddit-marketing-agent:v{__version__} ({who})"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"
