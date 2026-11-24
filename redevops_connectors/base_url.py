"""The base URL of a self-hosted or overridden API: the address an adapter sends its credential to.

Three adapters let the operator set it (``web`` ``search_base``, ``postiz`` and ``whatsapp_waha`` ``base_url``). The value is
configuration, not input, but it decides where a key goes, so it is checked once where it is set rather than trusted as typed:
an http(s) URL with a host and nothing else that changes the destination. ``https://api.example.com@other.example`` is refused
(the host of that URL is ``other.example``), and so are a query, a fragment, a blank, a control character and a backslash.
Plain http is accepted: a self-hosted instance on a private network is the ordinary case for two of the three."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_BAD = re.compile(r"[\x00-\x20\x7f\\]")


class BaseUrlError(ValueError):
    """The configured base URL is not one an adapter will send a credential to."""


def credential_base(url: object, *, what: str = "base URL") -> str:
    """``url`` without its trailing slash, or :class:`BaseUrlError`."""
    text = str(url or "").strip()
    if not text or _BAD.search(text):
        raise BaseUrlError(f"{what}: empty, or contains a blank, a control character or a backslash")
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - raises on a port that is not a number
    except ValueError as e:
        raise BaseUrlError(f"{what}: not a URL ({e})") from None
    if parts.scheme not in ("http", "https"):
        raise BaseUrlError(f"{what}: the scheme must be http or https")
    if not parts.hostname:
        raise BaseUrlError(f"{what}: no host")
    if "@" in parts.netloc:
        raise BaseUrlError(f"{what}: user information is not accepted (the host of such a URL is what follows the @)")
    if parts.query or parts.fragment:
        raise BaseUrlError(f"{what}: a query or a fragment is not part of a base URL")
    return text.rstrip("/")


class PathSegmentError(ValueError):
    """A value that was to become one segment of a request path cannot be one."""


def path_segment(value: object) -> str:
    """``value`` as exactly one segment of a request path. An id comes from a caller or from an earlier provider answer; written
    into a path as it is, ``ch_1/../customers`` or ``ch_1?expand[]=x`` would name another endpoint than the capability's. Every
    character outside the unreserved set is percent-encoded (``/``, ``?``, ``#`` and ``%`` included); an empty value and the two
    dot segments are refused, because a server resolves those however they are spelled."""
    from urllib.parse import quote
    text = str(value if value is not None else "")
    if not text or text in (".", "..") or _BAD.search(text):
        raise PathSegmentError("not a usable identifier for a request path")
    return quote(text, safe="")
