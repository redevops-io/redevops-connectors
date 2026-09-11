"""Provider adapters. Each is a thin, governed translation of one SaaS API to the
IntegrationAdapter contract. Providers pull their own dependencies as extras; the base
install ships only the SDK and lightweight definitions.

Auth is per-provider — OAuth2 (Slack, Google) or a private API key / token (the rest);
the SDK handles both. Every adapter is exercised deterministically against fixtures.
"""
from __future__ import annotations

from .slack import SLACK_OAUTH, SlackAdapter
from .klaviyo import KLAVIYO_REVISION, KlaviyoAdapter
from .ayrshare import AyrshareAdapter
from .blotato import BlotatoAdapter
from .postiz import POSTIZ_PUBLIC_API, PostizAdapter
from .google_common import GOOGLE_OAUTH
from .gmail import GmailAdapter
from .gcalendar import GoogleCalendarAdapter
from .whatsapp import WhatsAppAdapter
from .whatsapp_waha import WhatsAppWahaAdapter
from .web import WebAdapter
from .hubspot import HubSpotAdapter
from .stripe import StripeAdapter
from .polar import PolarAdapter
from .apollo import ApolloAdapter

__all__ = [
    "SlackAdapter", "SLACK_OAUTH",
    "KlaviyoAdapter", "KLAVIYO_REVISION",
    "AyrshareAdapter",
    "BlotatoAdapter",
    "PostizAdapter", "POSTIZ_PUBLIC_API",
    "GOOGLE_OAUTH", "GmailAdapter", "GoogleCalendarAdapter",
    "WhatsAppAdapter",
    "WhatsAppWahaAdapter",
    "WebAdapter",
    "HubSpotAdapter",
    "StripeAdapter",
    "PolarAdapter",
    "ApolloAdapter",
]
