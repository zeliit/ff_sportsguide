"""Isolated B HLS test: signed, channel-scoped relay with no Alive writes.

This test only issues playable links from a fresh B preview rendered by FF's
authenticated menu. Never accepts caller-supplied upstream host or path.
"""
import re
from functools import lru_cache
from urllib.parse import quote, urljoin, urlsplit, unquote

import requests
from flask import Response, request, stream_with_context
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from tool import ToolUtil

from .hls_proxy import _secret
from .proxy_config import configured_playback_proxy_url, requests_proxy_options, safe_network_error
from .b_stream_url import validate_root_stream_url
from .provider_config import provider_list, require_provider_value

BASE = "/ff_sportsguide/api/"
ROOT_AGE = 3 * 3600
ASSET_AGE = 3 * 3600
URI_RE = re.compile(r'(URI=")([^"]+)(")')
MEDIA_RE = re.compile(r"^/bigwinsoft/([0-9]{1,8})/([a-zA-Z0-9_.-]+)$")
ALLOWED_EXT = (".m3u8", ".ts", ".m4s", ".aac", ".mp4", ".key")
MAX_PLAYLIST = 1024 * 1024
CHUNK_SIZE = 65536


def _media_proxy_url():
    return configured_playback_proxy_url()


@lru_cache(maxsize=1)
def _serializer():
    return URLSafeTimedSerializer(_secret(), salt="ff-sportsguide-B-HLS-test-v1")


def _safe_channel(value):
    channel = str(value or "")
    if not re.fullmatch(r"[0-9]{1,8}", channel):
        raise ValueError("올바르지 않은 중계 B 영상번호입니다.")
    return channel


def _validate_url(url, channel):
    channel = _safe_channel(channel)
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in frozenset(provider_list("b_cdn_hosts")) or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.fragment):
        raise ValueError("허용되지 않은 중계 B HLS 호스트입니다.")
    raw = parsed.path
    if "%" in raw or "\\" in raw or "//" in raw or "/./" in raw or "/../" in raw:
        raise ValueError("안전하지 않은 B 미디어 경로입니다.")
    match = MEDIA_RE.fullmatch(raw)
    if not match or match.group(1) != channel or not match.group(2).endswith(ALLOWED_EXT):
        raise ValueError("영상번호와 미디어 경로가 일치하지 않습니다.")
    return url


def _root_url(channel, host=None):
    channel = _safe_channel(channel)
    if host is None:
        host = require_provider_value("b_primary_cdn_host")
    if host not in frozenset(provider_list("b_cdn_hosts")):
        raise ValueError("Unsupported B CDN")
    return "https://" + host + "/bigwinsoft/" + channel + "/playlist.m3u8"


def issue_ticket(item):
    """Only call while generating an authenticated FF menu from a fresh snapshot."""
    if item.get("state") != "ING":
        raise ValueError("방송 중인 경기만 재생할 수 있습니다.")
    seq = str(item.get("seq") or "")
    stream_url, channel = validate_root_stream_url(item.get("stream_url"), seq)
    return _serializer().dumps({"kind": "entry", "ch": channel, "seq": seq, "url": stream_url})


def _read_ticket(token, kind, seconds):
    try:
        data = _serializer().loads(str(token or ""), max_age=seconds)
        if not isinstance(data, dict) or data.get("kind") != kind:
            raise ValueError("서명 토큰 종류가 올바르지 않습니다.")
        return data
    except (BadSignature, SignatureExpired, ValueError, TypeError) as exc:
        raise ValueError("B 재생 토큰이 유효하지 않거나 만료되었습니다.") from exc


def _asset_link(channel, absolute_url):
    _validate_url(absolute_url, channel)
    token = _serializer().dumps({"kind": "asset", "ch": channel, "url": absolute_url})
    return ToolUtil.make_apikey_url(BASE + "b_asset?t=" + quote(token, safe=""))


def _rewrite_playlist(text, current_url, channel):
    output = []
    for line in text.splitlines():
        line_stripped = line.strip()
        if line_stripped.startswith("#"):
            if "URI=" in line:
                line = URI_RE.sub(
                    lambda m: m.group(1) + _asset_link(channel, urljoin(current_url, m.group(2))) + m.group(3),
                    line,
                )
        elif line_stripped:
            line = _asset_link(channel, urljoin(current_url, line_stripped))
        output.append(line)
    return "\n".join(output) + "\n"


def _relay(channel, url):
    _validate_url(url, channel)
    headers = {"Referer": require_provider_value("b_hls_referer"), "Accept": "*/*", "Accept-Encoding": "identity", "User-Agent": "FF-SportsGuide-B-Test/0.4"}
    requested_range = request.headers.get("Range", "")
    if requested_range and re.fullmatch(r"bytes=[0-9]*-[0-9]*", requested_range):
        headers["Range"] = requested_range
    try:
        proxy_options = requests_proxy_options(_media_proxy_url())
        upstream = requests.get(url, headers=headers, proxies=proxy_options,
                                timeout=(6, 18), stream=True, allow_redirects=False)
        if upstream.status_code == 403 and proxy_options:
            upstream.close()
            try:
                from .setup import P
                P.logger.warning("[SportsGuide] B CDN rejected playback proxy; retrying direct")
            except Exception:
                pass
            upstream = requests.get(url, headers=headers, proxies={},
                                    timeout=(6, 18), stream=True, allow_redirects=False)
        if upstream.status_code not in (200, 206):
            code = upstream.status_code
            upstream.close()
            return Response("원본 응답 오류 (" + str(code) + ")", status=502, mimetype="text/plain")
        ctype = upstream.headers.get("Content-Type", "").lower()
        is_manifest = urlsplit(url).path.endswith(".m3u8") or "mpegurl" in ctype
        if is_manifest:
            try:
                raw = bytearray()
                for part in upstream.iter_content(CHUNK_SIZE):
                    raw.extend(part)
                    if len(raw) > MAX_PLAYLIST:
                        raise ValueError("B 재생목록 허용 크기 초과")
                text = raw.decode("utf-8-sig")
                if not text.lstrip().startswith("#EXTM3U"):
                    raise ValueError("B 원본의 HLS 재생목록 형식 오류")
                result = _rewrite_playlist(text, url, channel)
            finally:
                upstream.close()
            response = Response(result, mimetype="application/vnd.apple.mpegurl")
        else:
            def iter_bytes():
                try:
                    for chunk in upstream.iter_content(CHUNK_SIZE):
                        if chunk:
                            yield chunk
                finally:
                    upstream.close()
            response = Response(stream_with_context(iter_bytes()), status=upstream.status_code,
                                mimetype=upstream.headers.get("Content-Type", "application/octet-stream").split(";")[0],
                                direct_passthrough=True)
            for key in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if upstream.headers.get(key):
                    response.headers[key] = upstream.headers[key]
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except (OSError, ValueError, requests.RequestException) as exc:
        detail = safe_network_error(exc) if isinstance(exc, requests.RequestException) else str(exc)
        return Response("B HLS relay failed: " + detail[:180], status=502, mimetype="text/plain")


def _serve_root_channel(channel, host=None):
    hosts = provider_list("b_cdn_hosts")
    host = host or require_provider_value("b_primary_cdn_host")
    order = [host] + [candidate for candidate in hosts if candidate != host]
    last_reply = None
    for candidate in order:
        reply = _relay(channel, _root_url(channel, candidate))
        last_reply = reply
        is_not_found = reply.status_code == 502 and reply.get_data(as_text=True).strip().endswith("(404)")
        if not is_not_found:
            return reply
    return last_reply


def serve_channel(seq):
    from .b_preview import read_b_preview
    if not re.fullmatch(r"[0-9]{1,10}", str(seq or "")):
        return Response("Invalid fixture", status=404)
    preview = read_b_preview()
    if not preview["available"] or preview["stale"]:
        return Response("B listing is not fresh; refresh required", status=503)
    item = next((x for x in preview["items"] if x["seq"] == str(seq)), None)
    if item is None:
        return Response("B fixture is no longer listed live", status=404)
    try:
        stream_url, channel = validate_root_stream_url(item.get("stream_url"), str(seq))
    except ValueError:
        return Response("B fixture stream is invalid; refresh required", status=503)
    return _relay(channel, stream_url)


def serve_root(token):
    try:
        entry = _read_ticket(token, "entry", ROOT_AGE)
        stream_url, channel = validate_root_stream_url(entry.get("url"), str(entry.get("seq") or ""))
        if channel != _safe_channel(entry.get("ch")):
            raise ValueError("B stream channel mismatch")
        return _relay(channel, stream_url)
    except ValueError:
        return Response("유효하지 않거나 만료된 B 재생 링크입니다.", status=403, mimetype="text/plain")


def serve_asset(token):
    try:
        item = _read_ticket(token, "asset", ASSET_AGE)
        ch = _safe_channel(item.get("ch"))
        url = _validate_url(item.get("url"), ch)
        return _relay(ch, url)
    except (ValueError, TypeError):
        return Response("유효하지 않거나 만료된 B 미디어 링크입니다.", status=403, mimetype="text/plain")
