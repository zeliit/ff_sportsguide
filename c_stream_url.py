from urllib.parse import urlsplit
import re

ROOT_RE = re.compile(r"^/live/([A-Za-z0-9_-]{5,80})/playlist\.m3u8$")


def validate_root_stream_url(value):
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("허용되지 않은 중계 C HLS 주소입니다.")
    if parsed.query:
        raise ValueError("중계 C 원본 HLS 주소에 예상하지 못한 쿼리가 있습니다.")
    match = ROOT_RE.fullmatch(parsed.path)
    if not match:
        raise ValueError("중계 C HLS 경로 형식이 올바르지 않습니다.")
    return url, match.group(1), parsed.hostname
