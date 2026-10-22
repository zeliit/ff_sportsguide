"""Validation for SportsGuide B's decrypted upstream HLS root URL."""
import re
from urllib.parse import parse_qs, urlsplit
from .provider_config import provider_list

ROOT_PATH_RE = re.compile(r"^/bigwinsoft/([0-9]{1,8})/playlist\.m3u8$")


def validate_root_stream_url(value, seq):
    url = str(value or "").strip()
    seq = str(seq or "").strip()
    if not re.fullmatch(r"[0-9]{1,10}", seq):
        raise ValueError("Invalid B fixture ID")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in frozenset(provider_list("b_cdn_hosts"))
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.fragment):
        raise ValueError("Invalid B stream host: " + str(parsed.hostname or "<none>"))
    if ("%" in parsed.path or "\\" in parsed.path or "//" in parsed.path
            or "/./" in parsed.path or "/../" in parsed.path):
        raise ValueError("Invalid B stream path")
    match = ROOT_PATH_RE.fullmatch(parsed.path)
    if match is None:
        raise ValueError("Invalid B stream playlist path")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError("Invalid B stream query") from exc
    if set(query) != {"t", "ds"} or any(len(values) != 1 for values in query.values()):
        raise ValueError("Invalid B stream query keys")
    stamp = query["t"][0]
    fixture = query["ds"][0]
    if not re.fullmatch(r"[0-9]{8,12}", stamp) or fixture != seq:
        raise ValueError("B stream query does not match fixture")
    return url, match.group(1)
