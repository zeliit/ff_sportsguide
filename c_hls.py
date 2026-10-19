"""Signed C HLS relay.

C upstreams require a proxy and a provider content Referer. Direct fallback is intentionally disabled.
"""
import re
from functools import lru_cache
from urllib.parse import quote, urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from flask import Response, request, stream_with_context
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from tool import ToolUtil

from .hls_proxy import _secret
from .proxy_config import configured_proxy_url, requests_proxy_options, safe_network_error
from .c_stream_url import validate_root_stream_url
from .provider_config import require_provider_value

BASE = "/ff_sportsguide/api/"
ROOT_AGE = 3 * 3600
ASSET_AGE = 3 * 3600
URI_RE = re.compile(r'(URI=")([^"]+)(")')
ALLOWED_EXT = (".m3u8", ".ts", ".m4s", ".aac", ".mp4", ".key")
MAX_PLAYLIST = 1024 * 1024
CHUNK_SIZE = 65536


@lru_cache(maxsize=1)
def _serializer():
    return URLSafeTimedSerializer(_secret(), salt="ff-sportsguide-C-HLS-v1")


def _safe_fixture(value):
    value = str(value or "")
    if not re.fullmatch(r"[0-9]{1,12}", value):
        raise ValueError("올바르지 않은 중계 C 경기번호입니다.")
    return value


def _safe_referer(value):
    url = str(value or "").strip()
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    allowed_host = require_provider_value("c_referer_host").lower()
    path_prefix = require_provider_value("c_referer_path_prefix")
    if (p.scheme != "https" or p.port not in (None, 443) or p.username or p.password
            or p.query or p.fragment
            or not (host == allowed_host or host.endswith("." + allowed_host))
            or not p.path.startswith(path_prefix)):
        raise ValueError("허용되지 않은 중계 C Referer입니다.")
    return url


def _origin_from_referer(referer):
    p = urlsplit(referer)
    return urlunsplit((p.scheme, p.netloc, "", "", ""))


def _add_site(url):
    p = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "site"]
    pairs.append(("site", require_provider_value("c_site_query_value")))
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(pairs), ""))


def _validate_asset(url, root_url):
    root_url, _, root_host = validate_root_stream_url(root_url)
    rp = urlsplit(root_url)
    p = urlsplit(str(url or "").strip())
    if (p.scheme != "https" or p.hostname != root_host or p.port not in (None, 443)
            or p.username or p.password or p.fragment):
        raise ValueError("허용되지 않은 중계 C 미디어 호스트입니다.")
    prefix = rp.path.rsplit("/", 1)[0] + "/"
    if not p.path.startswith(prefix) or "%" in p.path or "\\" in p.path or "/../" in p.path or "/./" in p.path:
        raise ValueError("안전하지 않은 중계 C 미디어 경로입니다.")
    if not p.path.lower().endswith(ALLOWED_EXT):
        raise ValueError("허용되지 않은 중계 C 미디어 확장자입니다.")
    return url


def issue_ticket(item):
    if item.get("state") != "ING":
        raise ValueError("방송 중인 경기만 재생할 수 있습니다.")
    fixture_id = _safe_fixture(item.get("id"))
    stream_url, _, _ = validate_root_stream_url(item.get("stream_url"))
    referer = _safe_referer(item.get("referer"))
    return _serializer().dumps({
        "kind": "entry", "id": fixture_id, "url": stream_url, "ref": referer
    })


def _read_ticket(token, kind, seconds):
    try:
        data = _serializer().loads(str(token or ""), max_age=seconds)
        if not isinstance(data, dict) or data.get("kind") != kind:
            raise ValueError("서명 토큰 종류가 올바르지 않습니다.")
        return data
    except (BadSignature, SignatureExpired, ValueError, TypeError) as exc:
        raise ValueError("C 재생 토큰이 유효하지 않거나 만료되었습니다.") from exc


def _asset_link(root_url, referer, absolute_url):
    _validate_asset(absolute_url, root_url)
    token = _serializer().dumps({
        "kind": "asset", "root": root_url, "ref": referer, "url": absolute_url
    })
    return ToolUtil.make_apikey_url(BASE + "c_asset?t=" + quote(token, safe=""))


def _rewrite_playlist(text, current_url, root_url, referer):
    output = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if "URI=" in line:
                line = URI_RE.sub(
                    lambda m: m.group(1) + _asset_link(root_url, referer, urljoin(current_url, m.group(2))) + m.group(3),
                    line,
                )
        elif stripped:
            line = _asset_link(root_url, referer, urljoin(current_url, stripped))
        output.append(line)
    return "\n".join(output) + "\n"


def _relay(root_url, url, referer):
    validate_root_stream_url(root_url)
    _validate_asset(url, root_url)
    referer = _safe_referer(referer)

    proxy_url = configured_proxy_url()
    if not proxy_url:
        return Response("중계 C 재생에는 Proxy 설정이 필요합니다.", status=503, mimetype="text/plain")

    upstream_url = _add_site(url)
    headers = {
        "Referer": referer,
        "Origin": _origin_from_referer(referer),
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "User-Agent": "Mozilla/5.0 FF-SportsGuide-C/0.4",
    }
    requested_range = request.headers.get("Range", "")
    if requested_range and re.fullmatch(r"bytes=[0-9]*-[0-9]*", requested_range):
        headers["Range"] = requested_range

    try:
        upstream = requests.get(
            upstream_url,
            headers=headers,
            proxies=requests_proxy_options(proxy_url),
            timeout=(7, 20),
            stream=True,
            allow_redirects=False,
        )
        if upstream.status_code not in (200, 206):
            code = upstream.status_code
            upstream.close()
            return Response("중계 C 원본 응답 오류 (" + str(code) + ")", status=502, mimetype="text/plain")

        ctype = upstream.headers.get("Content-Type", "").lower()
        is_manifest = urlsplit(url).path.endswith(".m3u8") or "mpegurl" in ctype
        if is_manifest:
            try:
                raw = bytearray()
                for part in upstream.iter_content(CHUNK_SIZE):
                    raw.extend(part)
                    if len(raw) > MAX_PLAYLIST:
                        raise ValueError("C 재생목록 허용 크기 초과")
                text = raw.decode("utf-8-sig")
                if not text.lstrip().startswith("#EXTM3U"):
                    raise ValueError("C 원본의 HLS 재생목록 형식 오류")
                result = _rewrite_playlist(text, url, root_url, referer)
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
            response = Response(
                stream_with_context(iter_bytes()),
                status=upstream.status_code,
                mimetype=upstream.headers.get("Content-Type", "application/octet-stream").split(";")[0],
                direct_passthrough=True,
            )
            for key in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if upstream.headers.get(key):
                    response.headers[key] = upstream.headers[key]

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
    except (OSError, ValueError, requests.RequestException) as exc:
        detail = safe_network_error(exc) if isinstance(exc, requests.RequestException) else str(exc)
        return Response("C HLS relay failed: " + detail[:180], status=502, mimetype="text/plain")


def serve_fixture(fixture_id):
    from .c_preview import read_c_preview
    try:
        fixture_id = _safe_fixture(fixture_id)
        preview = read_c_preview()
        if not preview["available"] or preview["stale"]:
            return Response("C listing is not fresh; refresh required", status=503)
        item = next((x for x in preview["items"] if x["id"] == fixture_id), None)
        if item is None:
            return Response("C fixture is no longer listed live", status=404)
        root_url, _, _ = validate_root_stream_url(item.get("stream_url"))
        referer = _safe_referer(item.get("referer"))
        return _relay(root_url, root_url, referer)
    except ValueError:
        return Response("Invalid C fixture", status=404)


def serve_root(token):
    try:
        entry = _read_ticket(token, "entry", ROOT_AGE)
        _safe_fixture(entry.get("id"))
        root_url, _, _ = validate_root_stream_url(entry.get("url"))
        referer = _safe_referer(entry.get("ref"))
        return _relay(root_url, root_url, referer)
    except ValueError:
        return Response("유효하지 않거나 만료된 C 재생 링크입니다.", status=403, mimetype="text/plain")


def serve_asset(token):
    try:
        item = _read_ticket(token, "asset", ASSET_AGE)
        root_url, _, _ = validate_root_stream_url(item.get("root"))
        referer = _safe_referer(item.get("ref"))
        url = _validate_asset(item.get("url"), root_url)
        return _relay(root_url, url, referer)
    except (ValueError, TypeError):
        return Response("유효하지 않거나 만료된 C 미디어 링크입니다.", status=403, mimetype="text/plain")
