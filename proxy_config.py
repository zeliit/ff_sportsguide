"""Shared, explicit HTTP(S) proxy configuration for A/B requests.

This module does not alter process-wide proxy settings and never retries a
configured proxy request directly. Authentication is split for Playwright.
"""
from urllib.parse import urlsplit, unquote
import requests


def validate_proxy_url(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid proxy host or port") from exc
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or port is None or not 1 <= port <= 65535
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("Proxy URL must be http(s)://host:port (optional credentials)")
    return value.rstrip("/")


def configured_proxy_url():
    from .setup import P
    return validate_proxy_url(P.ModelSetting.get("upstream_proxy_url") or "")


def _setting_enabled(key, default=False):
    from .setup import P
    try:
        value = P.ModelSetting.get(key)
    except Exception:
        return bool(default)
    if value in (None, ""):
        return bool(default)
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def configured_list_proxy_url():
    proxy = configured_proxy_url()
    # Older installs had no list_proxy key and always used the configured
    # upstream proxy for list collection. Preserve that behaviour on upgrade.
    return proxy if _setting_enabled("list_proxy", bool(proxy)) else ""


def configured_playback_proxy_url():
    proxy = configured_proxy_url()
    return proxy if _setting_enabled("playback_proxy", False) else ""


def requests_proxy_options(value):
    value = validate_proxy_url(value)
    return {"http": value, "https": value} if value else {}


def browser_proxy_options(value):
    value = validate_proxy_url(value)
    if not value:
        return None
    parsed = urlsplit(value)
    host = "[" + parsed.hostname + "]" if ":" in parsed.hostname else parsed.hostname
    options = {"server": parsed.scheme + "://" + host + ":" + str(parsed.port)}
    if parsed.username is not None:
        options["username"] = unquote(parsed.username)
        options["password"] = unquote(parsed.password or "")
    return options


def safe_network_error(exc):
    """Do not expose proxy credentials embedded in Requests exception URLs."""
    if isinstance(exc, requests.exceptions.ProxyError):
        return "Proxy connection failed; check proxy address, network and authentication."
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS certificate verification failed."
    if isinstance(exc, requests.exceptions.Timeout):
        return "Upstream request timed out."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "Upstream connection was interrupted."
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        return "Upstream HTTP error: " + str(getattr(response, "status_code", "unknown"))
    return "Upstream request failed (" + type(exc).__name__ + ")."
