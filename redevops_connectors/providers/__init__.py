"""Provider adapters. Each is a thin, governed translation of one SaaS API to the
IntegrationAdapter contract. Providers pull their own dependencies as extras; the base
install ships only the SDK and lightweight definitions."""
from __future__ import annotations

from .slack import SLACK_OAUTH, SlackAdapter

__all__ = ["SlackAdapter", "SLACK_OAUTH"]
