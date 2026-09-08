"""Provider adapters. Each is a thin, governed translation of one SaaS API to the
IntegrationAdapter contract. Providers pull their own dependencies as extras; the base
install ships only the SDK and lightweight definitions.

Auth is per-provider — OAuth2 (Slack, Google) or a private API key / token (the rest);
the SDK handles both. Every adapter is exercised deterministically against fixtures.
"""
from __future__ import annotations

# chat + approvals (OAuth2)
from .slack import SLACK_OAUTH, SlackAdapter
# marketing (API key)
from .klaviyo import KLAVIYO_REVISION, KlaviyoAdapter
# publish content to many venues in one call (API key)
from .ayrshare import AyrshareAdapter
from .blotato import BlotatoAdapter
from .postiz import POSTIZ_PUBLIC_API, PostizAdapter
# the Google vertical (OAuth2, shared helper)
from .google_common import GOOGLE_OAUTH
from .gmail import GmailAdapter
from .gcalendar import GoogleCalendarAdapter
# customer channel + CRM + billing (token / API key)
from .whatsapp import WhatsAppAdapter
from .hubspot import HubSpotAdapter
from .stripe import StripeAdapter

__all__ = [
    "SlackAdapter", "SLACK_OAUTH",
    "KlaviyoAdapter", "KLAVIYO_REVISION",
    "AyrshareAdapter",
    "BlotatoAdapter",
    "PostizAdapter", "POSTIZ_PUBLIC_API",
    "GOOGLE_OAUTH", "GmailAdapter", "GoogleCalendarAdapter",
    "WhatsAppAdapter",
    "HubSpotAdapter",
    "StripeAdapter",
]
