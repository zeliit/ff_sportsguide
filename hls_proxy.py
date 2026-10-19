"""Strict HLS relay for SportsGuide A.

The relay only fetches playlists and media from the configured HLS origin.
Subresource addresses are short-lived signed tokens; callers cannot select
an arbitrary upstream URL.
"""
import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests
from flask import Response, request, stream_with_context
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from tool import ToolUtil
from .storage_paths import RUNTIME_DIR
from .proxy_config import requests_proxy_options, validate_proxy_url as _validate_proxy_url, safe_network_error
from .provider_config import require_provider_value

SECRET_PATH = RUNTIME_DIR / "proxy_secret"
URI_RE = re.compile(r'(URI=")([^"]+)(")')
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
TOKEN_SECONDS = 1200


@lru_cache(maxsize=1)
def _secret():
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(SECRET_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_urlsafe(48).encode("ascii"))
        finally:
            os.close(fd)
    except FileExistsError:
        pass
    return SECRET_PATH.read_text(encoding="ascii").strip()


def _signer():
    return URLSafeTimedSerializer(_secret(), salt="ff-sportsguide-hls-v1")


def _allowed(url, main_url):
    parsed = urlsplit(url)
    master = urlsplit(main_url)
    if parsed.scheme != "https" or parsed.hostname != master.hostname:
        raise ValueError("HLS 원본과 다른 호스트는 프록시하지 않습니다.")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("허용되지 않은 HLS 주소 형식입니다.")
    # Channel streams and TS segment URIs are expected under /live/.
    if not parsed.path.startswith(master.path.rsplit("/", 1)[0] + "/"):
        raise ValueError("채널과 관계없는 리소스 경로입니다.")
    path = parsed.path.lower()
    if "/../" in path or "/./" in path or "%2e" in path or "%2f" in path or "%5c" in path:
        raise ValueError("잘못된 HLS 리소스 경로입니다.")
    return url


def _asset_url(channel, absolute_url, root_url):
    absolute_url = _allowed(absolute_url, root_url)
    token = _signer().dumps({"ch": channel, "url": absolute_url})
    return ToolUtil.make_apikey_url("/ff_sportsguide/api/proxy_asset?ch=" + quote(channel, safe="") + "&t=" + quote(token, safe=""))


def _rewrite_playlist(content, fetched_url, channel, root_url):
    result = []
    for line in content.splitlines():
        clean = line.strip()
        if clean.startswith("#"):
            line = URI_RE.sub(
                lambda m: m.group(1) + _asset_url(channel, urljoin(fetched_url, m.group(2)), root_url) + m.group(3),
                line,
            ) if "URI=" in line else line
        elif clean:
            line = _asset_url(channel, urljoin(fetched_url, clean), root_url)
        result.append(line)
    return "\n".join(result) + "\n"


def _proxy_options(proxy_url):
    return requests_proxy_options(proxy_url)


def validate_proxy_url(proxy_url):
    return _validate_proxy_url(proxy_url)


def relay(channel, upstream_url, root_url, proxy_url=""):
    """Return a Flask response for a trusted root or signed HLS subresource."""
    try:
        _allowed(upstream_url, root_url)
        proxies = _proxy_options(proxy_url)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; FF-SportsGuide/0.3)",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Referer": require_provider_value("a_hls_referer"),
        }
        if request.headers.get("Range"):
            headers["Range"] = request.headers["Range"][:100]
        response = requests.get(
            upstream_url,
            headers=headers,
            proxies=proxies,
            timeout=(7, 20),
            stream=True,
            allow_redirects=False,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            response.close()
            return Response("원본 스트림이 리다이렉트되었습니다.", status=502, mimetype="text/plain")
        if response.status_code not in (200, 206):
            status = response.status_code
            response.close()
            return Response("원본 스트림 오류 (" + str(status) + ")", status=502, mimetype="text/plain")
        media = response.headers.get("Content-Type", "").lower()
        is_m3u = urlsplit(upstream_url).path.endswith(".m3u8") or "mpegurl" in media
        if is_m3u:
            try:
                buff = bytearray()
                for part in response.iter_content(chunk_size=65536):
                    buff.extend(part)
                    if len(buff) > MAX_PLAYLIST_BYTES:
                        raise ValueError("HLS 재생목록 크기가 제한을 초과했습니다.")
                text = buff.decode("utf-8-sig")
                if not text.lstrip().startswith("#EXTM3U"):
                    raise ValueError("원본에서 유효한 HLS 목록을 받지 못했습니다.")
                playlist = _rewrite_playlist(text, upstream_url, channel, root_url)
            finally:
                response.close()
            res = Response(playlist, mimetype="application/vnd.apple.mpegurl")
        else:
            def chunks():
                try:
                    for part in response.iter_content(chunk_size=65536):
                        if part:
                            yield part
                finally:
                    response.close()
            res = Response(
                stream_with_context(chunks()),
                status=response.status_code,
                mimetype=response.headers.get("Content-Type", "application/octet-stream").split(";")[0],
                direct_passthrough=True,
            )
            for key in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if response.headers.get(key):
                    res.headers[key] = response.headers[key]
        res.headers["Access-Control-Allow-Origin"] = "*"
        res.headers["Cache-Control"] = "no-store"
        res.headers["X-Content-Type-Options"] = "nosniff"
        return res
    except (requests.RequestException, OSError, ValueError) as e:
        detail = safe_network_error(e) if isinstance(e, requests.RequestException) else str(e)
        return Response("HLS Proxy failed: " + detail[:180], status=502, mimetype="text/plain")


def parse_asset(channel, token, root_url):
    try:
        item = _signer().loads(token, max_age=TOKEN_SECONDS)
        if item.get("ch") != channel:
            raise ValueError("채널 불일치")
        return _allowed(item["url"], root_url)
    except (SignatureExpired, BadSignature, ValueError, KeyError, TypeError) as e:
        raise ValueError("유효하지 않거나 만료된 리소스 토큰입니다.") from e
