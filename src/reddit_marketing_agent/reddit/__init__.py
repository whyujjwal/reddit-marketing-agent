"""Reddit access layer: one interface, two backends (OAuth API or public RSS)."""

from __future__ import annotations

from ..config import Settings
from .base import NotSupported, RateLimited, RedditBackend, RedditError
from .oauth import OAuthBackend
from .rss import RSSBackend


def make_backend(settings: Settings) -> RedditBackend:
    if settings.has_app_credentials:
        return OAuthBackend(settings)
    return RSSBackend(settings)


__all__ = [
    "NotSupported",
    "OAuthBackend",
    "RSSBackend",
    "RateLimited",
    "RedditBackend",
    "RedditError",
    "make_backend",
]
