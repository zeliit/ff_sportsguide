"""FF sports listing; independent of MediaMTX and existing Alive/SPOTV plugins."""
import ipaddress
import json
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlencode, quote

import requests
import yaml
from bs4 import BeautifulSoup
from flask import Response, jsonify, redirect, render_template, request
from plugin import F, PluginModuleBase
from tool import ToolUtil
from .setup import P
from .alive_sync import DEFAULT_PATH as DEFAULT_ALIVE_PATH, state as alive_state, sync_alive_desired
from .hls_proxy import relay as hls_relay, parse_asset, validate_proxy_url
from .storage_paths import RUNTIME_DIR, LOCK_DIR
from .portable_lock import locked_file
from .proxy_config import requests_proxy_options, safe_network_error, configured_list_proxy_url
from .provider_config import provider_value, provider_page_url, provider_configured, require_provider_value, set_base_url, migrate_provider_config
from .b_preview import read_b_preview
from .b_refresh import refresh_b_list
from .b_hls_test import issue_ticket as b_issue_ticket, serve_root as b_serve_root, serve_asset as b_serve_asset, serve_channel as b_serve_channel
from .c_preview import read_c_preview
from .c_refresh import refresh_c_list
from .c_hls import issue_ticket as c_issue_ticket, serve_root as c_serve_root, serve_asset as c_serve_asset, serve_fixture as c_serve_fixture
from .merged import build_merged, save_merged, read_merged, expired_fallback_sources

logger = P.logger
ModelSetting = P.ModelSetting
CACHE = RUNTIME_DIR / "channels.json"
LOCK = LOCK_DIR / "refresh.lock"

A_CATEGORY_NAMES = {
    "1": "축구",
    "2": "농구",
    "3": "배구",
    "4": "야구",
    "5": "아이스하키",
    "6": "미식축구",
    "7": "LoL",
    "8": "e스포츠",
    "9": "배드민턴",
    "10": "복싱",
    "11": "테니스",
}
_worker_guard = threading.Lock()
_worker_started = False
_sync_guard = threading.RLock()
_last_all_attempt_ts = 0.0


def _setting(key, default):
    try:
        value = ModelSetting.get(key)
        return str(value).strip() if value not in (None, "") else default
    except Exception:
        return default


def _raw_setting(key, default=""):
    try:
        value = ModelSetting.get(key)
        return default if value is None else str(value).strip()
    except Exception:
        return default


def _channel_name_prefix():
    raw = " ".join(_raw_setting("channel_name_prefix", "Sports").split()).replace("\x00", "")[:40]
    if len(raw) >= 2 and raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    return raw


def _format_channel_name(league, title):
    prefix = _channel_name_prefix()
    parts = []
    if prefix:
        parts.append(f"[{prefix}]")
    if league:
        parts.append(f"[{league}]")
    parts.append(title)
    return " ".join(parts)


def _interval_seconds():
    try:
        return max(2, min(60, int(_setting("refresh_minutes", "5")))) * 60
    except ValueError:
        return 300


def _is_public_https(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("공개 HTTPS 주소만 사용할 수 있습니다.")
    if parsed.port not in (None, 443):
        raise ValueError("HTTPS 기본 포트만 허용합니다.")
    results = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not results:
        raise ValueError("도메인 DNS 응답이 없습니다.")
    for item in results:
        addr = ipaddress.ip_address(item[4][0])
        if not addr.is_global:
            raise ValueError("사설 주소 또는 내부 서버는 사용할 수 없습니다.")
    return url


def _normalize_base_url(value):
    url = _is_public_https(str(value or "").strip())
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("메인 URL은 도메인 루트 주소만 입력해 주세요.")
    return "https://" + parsed.hostname.lower() + "/"


def _get_html(url, referer=None):
    _is_public_https(url)
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"}
    if referer:
        headers["Referer"] = referer
    proxies = requests_proxy_options(_list_proxy_url())
    for attempt in range(2):
        try:
            r = requests.get(url, headers=headers, proxies=proxies,
                             timeout=(7, 15), allow_redirects=False)
            try:
                r.raise_for_status()
                if 300 <= r.status_code < 400:
                    raise ValueError("Set the redirect target explicitly in settings.")
                return r.text
            finally:
                r.close()
        except requests.exceptions.SSLError:
            raise
        except (requests.Timeout, requests.ConnectionError):
            if attempt:
                raise
            time.sleep(0.4)


def read_cache():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(data):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_name(CACHE.name + ".tmp." + str(os.getpid()))
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CACHE)


def collect():
    page_url = provider_page_url("a")
    html = _get_html(page_url)
    soup = BeautifulSoup(html, "html.parser")
    iframe_path = require_provider_value("a_iframe_path")
    frame_candidates = []
    for iframe in soup.select("iframe[src]"):
        u = urljoin(page_url, iframe.get("src"))
        if urlsplit(u).path == iframe_path:
            frame_candidates.append(u)
    if not frame_candidates:
        raise ValueError("중계 목록 iframe을 찾지 못했습니다.")
    listing = _get_html(frame_candidates[0], referer=page_url)
    rows = BeautifulSoup(listing, "html.parser").select("tr[data-media-row]")
    if not rows:
        raise ValueError("중계 목록 테이블을 찾지 못했습니다.")
    channels, seen = [], set()
    upcoming = 0
    for row in rows:
        buttons = row.select('button[data-action="select-channel"][data-channel]')
        if not buttons:
            upcoming += 1
            continue
        def txt(css):
            node = row.select_one(css)
            return node.get_text(" ", strip=True) if node else ""
        league = txt(".media-league")[:80]
        started = txt(".media-time")[:12]
        fallback = txt(".media-title")[:180]
        category_id = str(row.get("data-category") or "").strip()
        category = A_CATEGORY_NAMES.get(category_id, "")
        for btn in buttons:
            channel = (btn.get("data-channel") or "").strip()
            slot = (btn.get("data-slot") or "1").strip()
            if not re.fullmatch(r"live[0-9]{1,6}", channel):
                continue
            if (channel, slot) in seen:
                continue
            seen.add((channel, slot))
            channels.append({
                "seq": len(channels) + 1,
                "channel": channel,
                "slot": slot,
                "program": (btn.get("data-title") or fallback).strip()[:180],
                "league": league,
                "category_id": category_id,
                "category": category,
                "start_time": started,
                "status": "방송 중",
            })
    return {
        "source_url": page_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fetched_ts": time.time(),
        "live_count": len(channels),
        "upcoming_count": upcoming,
        "rows_total": len(rows),
        "channels": channels,
        "last_error": "",
    }


def _alive_enabled():
    return _setting("alive_enable", "False").lower() in ("true", "1", "yes", "on")


def _list_proxy():
    # Before this option existed, a configured upstream proxy was always used
    # for A/B list collection. Keep that as the upgrade fallback.
    default = "True" if _setting("upstream_proxy_url", "") else "False"
    return _setting("list_proxy", default).lower() in ("true", "1", "yes", "on")


def _playback_proxy():
    return _setting("playback_proxy", "False").lower() in ("true", "1", "yes", "on")


def _proxy_url():
    return _setting("upstream_proxy_url", "")


def _list_proxy_url():
    return _proxy_url() if _list_proxy() else ""


def _playback_proxy_url():
    return _proxy_url() if _playback_proxy() else ""


def _public_ff_url():
    value = (F.SystemModelSetting.get("ddns") or "").strip().rstrip("/")
    if value and urlsplit(value).scheme == "https":
        return value
    raise ValueError("FF 시스템 설정의 DDNS에 공개 HTTPS 주소를 설정해야 합니다.")


def _selected_source():
    value = _setting("preferred_source", "A").upper()
    return value if value in ("A", "B", "C") else "A"


def _merged_item_url(item):
    source = str(item.get("chosen_source") or "").upper()
    ident = str(item.get("selected_id") or "")
    if source == "A":
        if not re.fullmatch(r"live[0-9]{1,6}", ident):
            raise ValueError("통합 A 채널 ID가 올바르지 않습니다.")
        return _stream_link(ident) if _playback_proxy() else hls_url(ident)
    if source == "B":
        if not re.fullmatch(r"[0-9]{1,10}", ident):
            raise ValueError("통합 B 경기번호가 올바르지 않습니다.")
        return ToolUtil.make_apikey_url(
            "/ff_sportsguide/api/b_live.m3u8?" + urlencode({"seq": ident})
        )
    if source == "C":
        if not re.fullmatch(r"[0-9]{1,12}", ident):
            raise ValueError("통합 C 경기번호가 올바르지 않습니다.")
        return ToolUtil.make_apikey_url(
            "/ff_sportsguide/api/c_live.m3u8?" + urlencode({"id": ident})
        )
    raise ValueError("통합 경기의 선택 소스가 올바르지 않습니다.")


def _merged_alive_desired(merged):
    if not isinstance(merged, dict) or not isinstance(merged.get("items"), list):
        raise ValueError("통합 경기 목록이 없습니다. A/B/C 목록을 먼저 갱신해 주세요.")
    try:
        age = time.time() - float(merged.get("fetched_ts") or 0)
    except (TypeError, ValueError):
        raise ValueError("통합 경기 목록의 갱신 시각이 없습니다.")
    if not -60 < age <= 900:
        raise ValueError("통합 경기 목록이 15분 이상 오래되었습니다.")

    desired = {}
    for item in merged["items"]:
        key = str(item.get("key") or "")
        ident = str(item.get("selected_id") or "")
        if not re.fullmatch(r"sgu_[0-9a-f]{12}", key):
            logger.warning("[SportsGuide] Alive item skipped: invalid key=%s", key[:40])
            continue
        try:
            url = _merged_item_url(item)
        except Exception as exc:
            logger.warning(
                "[SportsGuide] Alive item skipped key=%s source=%s id=%s: %s",
                key, item.get("chosen_source"), ident[:30], exc,
            )
            continue

        league = " ".join(str(item.get("league") or "").split()).replace("\x00", "")[:100]
        title = " ".join(str(item.get("title") or ident).split()).replace("\x00", "")[:180]
        name = _format_channel_name(league, title)
        desired[key] = {"name": name, "url": url}
    if merged["items"] and not desired:
        raise ValueError("통합 경기의 재생 URL을 하나도 생성하지 못했습니다.")
    return desired


ALIVE_CACHE_INVALIDATE_PENDING = RUNTIME_DIR / "alive_cache_invalidate.pending"


def _invalidate_alive_channel_cache(retries=3):
    """Expire Alive's full channel cache after an actual alive.yaml change."""
    last_error = None
    for attempt in range(1, max(1, int(retries)) + 1):
        try:
            AliveP = None
            try:
                AliveP = F.PluginManager.get_plugin_instance("alive")
            except Exception:
                AliveP = None
            if AliveP is None:
                from alive.setup import P as AliveP
            expired = "1970-01-01T00:00:00"
            AliveP.ModelSetting.set("channel_list_updated_at", expired)
            current = AliveP.ModelSetting.get("channel_list_updated_at")
            if str(current or "") != expired:
                raise RuntimeError("Alive cache timestamp verification failed")
            try:
                ALIVE_CACHE_INVALIDATE_PENDING.unlink(missing_ok=True)
            except OSError:
                pass
            logger.info("[SportsGuide] Alive channel cache invalidated attempt=%s", attempt)
            return {"cache_invalidated": True, "cache_invalidate_attempts": attempt}
        except Exception as exc:
            last_error = exc
            logger.warning(
                "[SportsGuide] Alive channel cache invalidation failed attempt=%s/%s: %s",
                attempt, retries, exc,
            )
            if attempt < retries:
                time.sleep(0.5 * attempt)

    try:
        ALIVE_CACHE_INVALIDATE_PENDING.write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
    except OSError as marker_exc:
        logger.warning("[SportsGuide] Alive cache retry marker write failed: %s", marker_exc)
    return {
        "cache_invalidated": False,
        "cache_invalidate_attempts": max(1, int(retries)),
        "cache_invalidate_error": str(last_error)[:180] if last_error else "unknown error",
    }


def run_alive_sync(merged=None, require_enabled=True, dry_run=False):
    with _sync_guard:
        if require_enabled and not _alive_enabled():
            return {"ret": "skipped", "message": "Alive integration is off"}
        try:
            if merged is None:
                merged = read_merged()
                if not merged:
                    merged = build_merged(_selected_source(), enabled_sources=_enabled_sources())
                    save_merged(merged)
            desired = _merged_alive_desired(merged)
            result = sync_alive_desired(
                desired,
                _setting("alive_yaml_path", DEFAULT_ALIVE_PATH),
                dry_run=dry_run,
            )
            cache_result = {}
            pending_retry = (not dry_run and ALIVE_CACHE_INVALIDATE_PENDING.exists())
            if not dry_run and (bool(result.get("changed")) or pending_retry):
                cache_result = _invalidate_alive_channel_cache(retries=3)
            if not dry_run:
                logger.info(
                    "[SportsGuide] Alive merged priority=%s raw=%s duplicate=%s merged=%s result=%s",
                    _selected_source(),
                    merged.get("raw_live_count", 0),
                    merged.get("duplicate_count", 0),
                    merged.get("live_count", 0),
                    result,
                )
            cache_failed = bool(cache_result) and not cache_result.get("cache_invalidated", False)
            response = {
                "ret": "error" if cache_failed else "success",
                "priority": _selected_source(),
                "raw_live_count": merged.get("raw_live_count", 0),
                "duplicate_count": merged.get("duplicate_count", 0),
                "merged_live_count": merged.get("live_count", 0),
                **result,
                **cache_result,
            }
            if cache_failed:
                response["message"] = "Alive 채널 캐시 무효화에 실패했습니다. 다음 동기화에서 재시도합니다."
            return response
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
            logger.warning("[SportsGuide] merged Alive sync skipped: %s", exc)
            return {"ret": "error", "message": str(exc)}


def _enabled_sources():
    return ("A", "B", "C") if configured_list_proxy_url() else ("A", "B")


def _refresh_after_settings_save():
    try:
        refresh_all_synced(force=True)
    except Exception:
        logger.exception("[SportsGuide] background refresh after settings save failed")


def _sync_current_if_ready(fallback_sources=None):
    if not _alive_enabled():
        return {"ret": "skipped", "message": "Alive integration is off"}
    fallback = {str(x).upper() for x in (fallback_sources or ())}
    try:
        merged = build_merged(
            _selected_source(),
            enabled_sources=_enabled_sources(),
            allow_stale_sources=fallback,
        )
        save_merged(merged)
        return run_alive_sync(merged=merged)
    except Exception as exc:
        logger.info("[SportsGuide] merged sync deferred: %s", exc)
        return {"ret": "skipped", "message": str(exc)[:180]}

def refresh_b_synced(force=False):
    result = refresh_b_list(force=force)
    fallback = {"B"} if result.get("fallback") else set()
    return {**result, "alive": _sync_current_if_ready(fallback_sources=fallback)}

def refresh_c_synced(force=False):
    result = refresh_c_list(force=force)
    fallback = {"C"} if result.get("fallback") else set()
    return {**result, "alive": _sync_current_if_ready(fallback_sources=fallback)}

def refresh(force=False):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with locked_file(LOCK, blocking=False):
            cached = read_cache()
            current_source = provider_page_url("a")
            age = time.time() - float(cached.get("fetched_ts") or 0)
            if cached.get("source_url") == current_source and age < (30 if force else _interval_seconds()):
                return cached
            try:
                fresh = collect()
                save_cache(fresh)
                logger.info("[SportsGuide A] updated live=%s upcoming=%s",
                            fresh["live_count"], fresh["upcoming_count"])
                return fresh
            except Exception as exc:
                detail = safe_network_error(exc) if isinstance(exc, requests.RequestException) else str(exc)
                logger.warning("[SportsGuide A] refresh failed: %s", detail)
                cached["last_error"] = detail[:220]
                cached["last_error_at"] = datetime.now(timezone.utc).isoformat()
                save_cache(cached)
                return cached
    except BlockingIOError:
        return read_cache()


def refresh_all_synced(force=True):
    global _last_all_attempt_ts
    if not provider_configured():
        return {"ret": "pending", "message": "메인 URL 설정 대기 중", "sources": {}}
    _last_all_attempt_ts = time.time()
    errors = {}
    sources = {}
    fallback_sources = set()
    requested_sources = set(_enabled_sources())

    a = refresh(force=force)
    sources["A"] = {"live": int(a.get("live_count") or 0)}
    if a.get("last_error"):
        errors["A"] = str(a.get("last_error"))[:240]
        fallback_sources.add("A")
        sources["A"]["fallback"] = True

    try:
        b = refresh_b_list(force=force)
        sources["B"] = {"live": int(b.get("live") or 0), "fallback": bool(b.get("fallback"))}
        if b.get("fallback"):
            fallback_sources.add("B")
            errors["B"] = str(b.get("error") or b.get("message") or "B refresh failed")[:240]
            sources["B"]["cache_age_seconds"] = b.get("cache_age_seconds")
    except Exception as exc:
        errors["B"] = str(exc)[:240]
        sources["B"] = {"live": 0, "error": errors["B"]}

    if "C" in requested_sources:
        try:
            c = refresh_c_list(force=force)
            sources["C"] = {"live": int(c.get("live") or 0), "fallback": bool(c.get("fallback"))}
            if c.get("fallback"):
                fallback_sources.add("C")
                errors["C"] = str(c.get("error") or c.get("message") or "C refresh failed")[:240]
                sources["C"]["cache_age_seconds"] = c.get("cache_age_seconds")
        except Exception as exc:
            errors["C"] = str(exc)[:240]
            sources["C"] = {"live": 0, "error": errors["C"]}
    else:
        sources["C"] = {"live": 0, "skipped": True, "reason": "proxy_not_configured"}

    usable_sources = set()
    for source in sorted(requested_sources):
        try:
            build_merged(
                _selected_source(),
                enabled_sources=(source,),
                allow_stale_sources=({source} if source in fallback_sources else ()),
            )
            usable_sources.add(source)
        except Exception as exc:
            errors.setdefault(source, str(exc)[:240])
            if source in sources:
                sources[source]["omitted"] = True
                sources[source]["error"] = errors[source]

    omitted_sources = requested_sources - usable_sources
    expired_omitted = expired_fallback_sources(omitted_sources, fallback_sources, sources)
    preserve_omitted = omitted_sources - expired_omitted

    if expired_omitted:
        logger.warning(
            "[SportsGuide] stale fallback expired; dropping source(s)=%s and continuing with=%s",
            sorted(expired_omitted), sorted(usable_sources),
        )

    if preserve_omitted:
        old = read_merged()
        if isinstance(old, dict) and isinstance(old.get("items"), list):
            message = "일부 소스 갱신 실패로 마지막 정상 통합 목록과 Alive를 유지합니다."
            logger.warning(
                "[SportsGuide] partial refresh preserved omitted=%s errors=%s",
                sorted(preserve_omitted), errors,
            )
            return {
                "ret": "success", "degraded": True, "preserved": True,
                "message": message, "sources": sources, "errors": errors,
                "fallback_sources": sorted(fallback_sources & usable_sources),
                "omitted_sources": sorted(omitted_sources),
                "raw_live_count": old.get("raw_live_count", 0),
                "duplicate_count": old.get("duplicate_count", 0),
                "merged_live_count": old.get("live_count", 0),
                "alive": {"ret": "preserved", "message": "기존 Alive 목록을 유지합니다."},
            }

    if not usable_sources:
        old = read_merged()
        if isinstance(old, dict) and isinstance(old.get("items"), list):
            message = "모든 소스 갱신에 실패해 마지막 통합 캐시를 유지합니다."
            logger.warning("[SportsGuide] %s errors=%s", message, errors)
            return {
                "ret": "success", "degraded": True, "preserved": True,
                "message": message, "sources": sources, "errors": errors,
                "raw_live_count": old.get("raw_live_count", 0),
                "duplicate_count": old.get("duplicate_count", 0),
                "merged_live_count": old.get("live_count", 0),
                "alive": {"ret": "preserved", "message": "기존 Alive 목록을 유지합니다."},
            }
        return {
            "ret": "error", "message": "사용 가능한 중계 소스와 이전 통합 캐시가 없습니다.",
            "sources": sources, "errors": errors,
        }

    try:
        merged = build_merged(
            _selected_source(),
            enabled_sources=usable_sources,
            allow_stale_sources=fallback_sources & usable_sources,
        )
        degraded = bool(errors or (fallback_sources & usable_sources) or (usable_sources != requested_sources))
        merged["degraded"] = degraded
        merged["fallback_sources"] = sorted(fallback_sources & usable_sources)
        merged["omitted_sources"] = sorted(requested_sources - usable_sources)
        merged["source_errors"] = errors
        save_merged(merged)
    except Exception as exc:
        old = read_merged()
        if isinstance(old, dict) and isinstance(old.get("items"), list):
            logger.warning("[SportsGuide] merge failed; last merged cache preserved: %s", exc)
            return {
                "ret": "success", "degraded": True, "preserved": True,
                "message": "통합 처리 실패로 마지막 통합 캐시를 유지합니다.",
                "sources": sources, "errors": {**errors, "MERGE": str(exc)[:240]},
                "raw_live_count": old.get("raw_live_count", 0),
                "duplicate_count": old.get("duplicate_count", 0),
                "merged_live_count": old.get("live_count", 0),
                "alive": {"ret": "preserved", "message": "기존 Alive 목록을 유지합니다."},
            }
        return {"ret": "error", "message": str(exc)[:240], "sources": sources, "errors": errors}

    alive = run_alive_sync(merged=merged) if _alive_enabled() else {
        "ret": "skipped", "message": "Alive integration is off"
    }
    log_method = logger.warning if merged.get("degraded") else logger.info
    log_method(
        "[SportsGuide] A/B/C refreshed raw=%s duplicate=%s merged=%s priority=%s fallback=%s omitted=%s",
        merged.get("raw_live_count", 0), merged.get("duplicate_count", 0),
        merged.get("live_count", 0), _selected_source(),
        merged.get("fallback_sources", []), merged.get("omitted_sources", []),
    )
    return {
        "ret": "success",
        "degraded": bool(merged.get("degraded")),
        "message": "일부 소스는 이전 정상 캐시를 사용 중입니다." if merged.get("degraded") else "갱신 완료",
        "sources": sources,
        "errors": errors,
        "fallback_sources": merged.get("fallback_sources", []),
        "omitted_sources": merged.get("omitted_sources", []),
        "raw_live_count": merged.get("raw_live_count", 0),
        "duplicate_count": merged.get("duplicate_count", 0),
        "merged_live_count": merged.get("live_count", 0),
        "alive": alive,
    }

def refresh_selected_now():
    result = refresh_all_synced(force=True)
    if result.get("ret") != "success":
        return result
    alive = result.get("alive") or {}
    if alive.get("ret") != "success":
        return alive
    return {
        **alive,
        "raw_live_count": result.get("raw_live_count", 0),
        "duplicate_count": result.get("duplicate_count", 0),
        "merged_live_count": result.get("merged_live_count", 0),
    }


def periodic_once():
    global _last_all_attempt_ts
    if not provider_configured():
        return {"ret": "pending", "message": "메인 URL 설정 대기 중"}
    now = time.time()
    if _last_all_attempt_ts <= 0 or now - _last_all_attempt_ts >= _interval_seconds():
        return refresh_all_synced(force=True)
    return None


def periodic():
    while True:
        try:
            periodic_once()
        except Exception:
            logger.exception("[SportsGuide] A/B/C periodic refresh error")
        time.sleep(60)


def start_periodic():
    global _worker_started
    with _worker_guard:
        if not _worker_started:
            _worker_started = True
            threading.Thread(target=periodic, daemon=True, name="ff-sportsguide").start()


def hls_url(channel, validate_dns=True):
    pattern = require_provider_value("a_hls_template")
    if not re.fullmatch(r"live[0-9]{1,6}", channel):
        raise ValueError("채널 ID 형식이 올바르지 않습니다.")
    url = pattern.replace("{channel}", channel)
    if not urlsplit(url).path.endswith(".m3u8"):
        raise ValueError("HLS 주소 형식에 .m3u8이 필요합니다.")
    if validate_dns:
        _is_public_https(url)
    return url


def known_channel(channel):
    cached = read_cache()
    return next((item for item in cached.get("channels", []) if item.get("channel") == channel), None)


def _stream_link(channel):
    """Build a fresh authenticated FF URL; never rely on cached API key URLs."""
    if not re.fullmatch(r"live[0-9]{1,6}", channel):
        raise ValueError("채널 ID 형식이 올바르지 않습니다.")
    return ToolUtil.make_apikey_url("/ff_sportsguide/api/stream.m3u8?ch=" + quote(channel, safe=""))


def playlist():
    if not provider_configured():
        return Response("#EXTM3U\n", content_type="audio/x-mpegurl")

    data = read_merged()
    if not data or not isinstance(data.get("items"), list):
        try:
            data = build_merged(_selected_source(), enabled_sources=_enabled_sources())
            save_merged(data)
        except Exception as exc:
            logger.warning("[SportsGuide] M3U build failed; serving empty/previous cache: %s", exc)
            data = read_merged()

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return Response("#EXTM3U\n", content_type="audio/x-mpegurl")

    entries = ["#EXTM3U"]
    for item in data.get("items", []):
        key = str(item.get("key") or "")
        if not re.fullmatch(r"sgu_[0-9a-f]{12}", key):
            logger.warning("[SportsGuide] M3U item skipped: invalid key=%s", key[:40])
            continue
        try:
            sport = " ".join(str(item.get("sport") or "기타").split()).replace('"', "'").replace("\n", " ")[:40]
            title = " ".join(str(item.get("title") or item.get("selected_id") or key).split()).replace('"', "'").replace("\n", " ")[:180]
            league = " ".join(str(item.get("league") or "").split()).replace('"', "'").replace("\n", " ")[:100]
            group = league or sport
            name = _format_channel_name(league, title)
            url = _merged_item_url(item)
            entries.append(f'#EXTINF:-1 tvg-id="{key}" group-title="{group}",{name}')
            entries.append(url)
        except Exception as exc:
            logger.warning(
                "[SportsGuide] M3U item skipped key=%s source=%s id=%s: %s",
                key, item.get("chosen_source"), str(item.get("selected_id") or "")[:30], exc,
            )
            continue
    return Response("\n".join(entries) + "\n", content_type="audio/x-mpegurl")

class Logic(PluginModuleBase):
    db_default = {
        "refresh_minutes": "5",
        "alive_enable": "False",
        "alive_yaml_path": DEFAULT_ALIVE_PATH,
        "list_proxy": "False",
        "playback_proxy": "False",
        "upstream_proxy_url": "",
        "preferred_source": "A",
        "channel_name_prefix": "Sports",
    }

    def __init__(self, PM):
        super().__init__(PM, None)
        self.name = "list"

    def plugin_load(self):
        if migrate_provider_config():
            logger.info("[SportsGuide] provider 설정을 메인 URL 전용 형식으로 정리했습니다.")
        # One-time migration for the configurable channel prefix introduced in 0.4.12.
        # After this marker is set, user changes are preserved across restarts.
        if not ModelSetting.has_key("channel_prefix_migrated_v0412"):
            ModelSetting.set("channel_name_prefix", "Sports")
            ModelSetting.set("channel_prefix_migrated_v0412", "True")
            logger.info("[SportsGuide] migrated channel prefix setting")
        # FlaskFarm's setting_save route cannot add new keys introduced in upgrades.
        # Register the A/B choice for users upgrading from older versions.
        if not ModelSetting.has_key("preferred_source"):
            ModelSetting.set("preferred_source", "A")
            logger.info("[SportsGuide] added missing preferred_source setting")
        if not ModelSetting.has_key("list_proxy"):
            ModelSetting.set("list_proxy", "True" if _proxy_url() else "False")
            logger.info("[SportsGuide] added missing list_proxy setting")
        if not ModelSetting.has_key("channel_name_prefix"):
            ModelSetting.set("channel_name_prefix", "Sports")
            logger.info("[SportsGuide] added missing channel_name_prefix setting")
        start_periodic()

    def process_menu(self, sub, req):
        arg = {
            "base_url": provider_value("base_url", ""),
            "refresh_minutes": _setting("refresh_minutes", "5"),
            "alive_enable": _alive_enabled(),
            "alive_yaml_path": _setting("alive_yaml_path", DEFAULT_ALIVE_PATH),
            "list_proxy": _list_proxy(),
            "playback_proxy": _playback_proxy(),
            "upstream_proxy_url": _proxy_url(),
            "preferred_source": _setting("preferred_source", "A"),
            "channel_name_prefix": _channel_name_prefix(),
            "api_m3u": ToolUtil.make_apikey_url("/ff_sportsguide/api/m3u"),
        }
        # Keep the original A channel route independent of the saved preference.
        if sub == "b_test":
            preview = read_b_preview()
            if preview["available"] and not preview["stale"]:
                for item in preview["items"]:
                    try:
                        item["ticket"] = b_issue_ticket(item)
                        item["stream_url"] = ToolUtil.make_apikey_url("/ff_sportsguide/api/b_stream.m3u8?t=" + quote(item["ticket"], safe=""))
                    except ValueError:
                        item["ticket"] = ""
            arg["b_preview"] = preview
            return render_template("ff_sportsguide_b_test.html", arg=arg)
        if sub == "c_list":
            preview = read_c_preview()
            if preview["available"] and not preview["stale"]:
                for item in preview["items"]:
                    try:
                        item["ticket"] = c_issue_ticket(item)
                        item["stream_url"] = ToolUtil.make_apikey_url("/ff_sportsguide/api/c_stream.m3u8?t=" + quote(item["ticket"], safe=""))
                    except ValueError:
                        item["ticket"] = ""
            arg["c_preview"] = preview
            return render_template("ff_sportsguide_c.html", arg=arg)
        if sub == "a_list":
            return render_template("ff_sportsguide_list.html", arg=arg)
        if sub == "alive":
            try:
                arg["alive_state"] = alive_state(arg["alive_yaml_path"], source="U")
                arg["merged_state"] = read_merged()
            except Exception as exc:
                arg["alive_state"] = {"exists": False, "count": 0, "error": str(exc)}
            return render_template("ff_sportsguide_alive.html", arg=arg)
        if sub == "setting":
            return render_template("ff_sportsguide_setting.html", arg=arg)
        return render_template("ff_sportsguide_list.html", arg=arg)

    def process_ajax(self, sub, req):
        if sub in ("c_refresh", "b_refresh", "channel_list", "alive_sync") and not provider_configured():
            return jsonify({"ret": "error", "message": "메인 URL을 먼저 설정해 주세요."}), 400
        if sub == "c_refresh":
            try:
                result = refresh_c_synced(force=True)
                return jsonify({"ret": "success", **result})
            except Exception as exc:
                logger.warning("[SportsGuide C] browser refresh failed: %s", exc)
                return jsonify({"ret": "error", "message": "중계 C 갱신 실패: " + str(exc)[:180]}), 503
        if sub == "b_refresh":
            try:
                result = refresh_b_synced()
                return jsonify({"ret": "success", **result})
            except Exception as exc:
                logger.warning("[SportsGuide B] browser refresh failed: %s", exc)
                return jsonify({"ret": "error", "message": "중계 B 갱신 실패: " + str(exc)[:180]}), 503
        if sub == "channel_list":
            force = str(req.form.get("refresh") or "").lower() in ("1", "true", "yes")
            data = refresh(force=force) if force else read_cache()
            if not data:
                data = refresh()
            if force:
                _sync_current_if_ready()
            channels = []
            for row in data.get("channels", []):
                item = dict(row)
                item["url"] = _stream_link(str(item.get("channel") or ""))
                channels.append(item)
            return jsonify({
                "ret": "success",
                "channels": channels,
                "live_count": data.get("live_count", 0),
                "upcoming_count": data.get("upcoming_count", 0),
                "updated_at": data.get("updated_at"),
                "last_error": data.get("last_error") or "",
                "source_url": data.get("source_url") or "",
            })
        if sub == "alive_save":
            try:
                path = (req.form.get("alive_yaml_path") or "").strip()
                if not Path(path).is_absolute() or Path(path).name != "alive.yaml":
                    raise ValueError("alive.yaml의 절대 경로를 입력하세요.")
                enabled = str(req.form.get("alive_enable") or "").lower() in ("true", "1", "yes", "on")
                if enabled:
                    # Validate the target before enabling automatic writes.
                    current = alive_state(path)
                    if not current["exists"]:
                        raise FileNotFoundError("Alive 설정 파일을 찾지 못했습니다.")
                preferred = str(req.form.get("preferred_source") or _selected_source()).upper()
                if preferred not in ("A", "B", "C"):
                    raise ValueError("중계 우선순위는 A, B, C 중에서 선택해 주세요.")
                with _sync_guard:
                    ModelSetting.set("alive_yaml_path", path)
                    ModelSetting.set("alive_enable", "True" if enabled else "False")
                    ModelSetting.set("preferred_source", preferred)
                msg = "Alive 연동 설정을 저장했습니다."
                if enabled:
                    result = refresh_selected_now()
                    if result.get("ret") != "success":
                        msg += " 동기화는 다음 정상 목록 갱신 때 진행됩니다: " + result.get("message", "")
                    else:
                        msg += " 현재 채널을 동기화했습니다."
                return jsonify({"ret": "success", "message": msg})
            except Exception as exc:
                return jsonify({"ret": "error", "message": str(exc)}), 400
        if sub == "alive_sync":
            try:
                result = refresh_selected_now()
                return jsonify(result), 200 if result.get("ret") == "success" else 400
            except Exception as exc:
                return jsonify({"ret": "error", "message": str(exc)[:180]}), 503
        # The built-in /ajax/setting_save is intercepted by FlaskFarm before
        # process_ajax is called. Use an independent route for validation.
        if sub == "sports_save":
            try:
                base_url = _normalize_base_url(req.form.get("base_url") or "")
                base_changed = base_url != provider_value("base_url", "")
                minutes = int(req.form.get("refresh_minutes") or "5")
                if not (2 <= minutes <= 60):
                    raise ValueError("갱신 주기는 2~60분으로 설정해 주세요.")
                list_proxy_enabled = str(req.form.get("list_proxy") or "").lower() in ("true", "1", "yes", "on")
                proxy_enabled = str(req.form.get("playback_proxy") or "").lower() in ("true", "1", "yes", "on")
                upstream = validate_proxy_url((req.form.get("upstream_proxy_url") or "").strip())
                prefix = " ".join(str(req.form.get("channel_name_prefix") or "").split()).replace("\x00", "")[:40]
                if len(prefix) >= 2 and prefix.startswith("[") and prefix.endswith("]"):
                    prefix = prefix[1:-1].strip()
                preferred = _selected_source()
                if preferred not in ("A", "B", "C"):
                    raise ValueError("중계 우선순위는 A, B, C 중에서 선택해 주세요.")
                if proxy_enabled:
                    _public_ff_url()
                playback_changed = proxy_enabled != _playback_proxy()
                prefix_changed = prefix != _channel_name_prefix()
                set_base_url(base_url)
                ModelSetting.set("refresh_minutes", str(minutes))
                ModelSetting.set("list_proxy", "True" if list_proxy_enabled else "False")
                ModelSetting.set("playback_proxy", "True" if proxy_enabled else "False")
                ModelSetting.set("upstream_proxy_url", upstream)
                ModelSetting.set("channel_name_prefix", prefix)
                message = "설정을 저장했습니다. Alive 연동 대상은 Alive 연동 탭에서 선택합니다."
                if base_changed:
                    threading.Thread(target=_refresh_after_settings_save, daemon=True,
                                     name="ff-sportsguide-settings-refresh").start()
                    message += " 메인 URL을 적용하고 경기 목록 갱신을 시작했습니다."
                if _alive_enabled() and (playback_changed or prefix_changed):
                    result = run_alive_sync(merged=read_merged())
                    if result.get("ret") == "success":
                        message += " Alive 채널 재생주소도 갱신했습니다."
                    else:
                        message += " Alive 채널 재생주소는 다음 정상 목록 갱신 때 반영됩니다."
                return jsonify({"ret": "success", "message": message,
                                "next_url": ("/ff_sportsguide/c_list" if preferred == "C" else
                                             "/ff_sportsguide/b_test" if preferred == "B" else
                                             "/ff_sportsguide/list")})
            except (ValueError, OSError) as exc:
                return jsonify({"ret": "error", "message": str(exc)}), 400
        return jsonify({"ret": "error", "message": "Unknown operation"}), 404

    def process_api(self, sub, req):
        if sub == "c_live.m3u8":
            return c_serve_fixture(req.args.get("id") or "")
        if sub == "c_stream.m3u8":
            return c_serve_root(req.args.get("t") or "")
        if sub == "c_asset":
            return c_serve_asset(req.args.get("t") or "")
        if sub == "b_live.m3u8":
            return b_serve_channel(req.args.get("seq") or "")
        if sub == "b_stream.m3u8":
            return b_serve_root(req.args.get("t") or "")
        if sub == "b_asset":
            return b_serve_asset(req.args.get("t") or "")
        if sub == "m3u":
            return playlist()
        if sub == "channels.json":
            return jsonify(read_cache())
        if sub in ("stream.m3u8", "proxy_asset"):
            channel = (req.args.get("ch") or "").strip()
            if not known_channel(channel):
                return Response("채널이 목록에 없습니다.", status=404, mimetype="text/plain")
            try:
                root = hls_url(channel, validate_dns=(sub != "proxy_asset"))
                if sub == "proxy_asset":
                    if not _playback_proxy():
                        return Response("Proxy가 꺼져 있습니다.", status=404)
                    url = parse_asset(channel, req.args.get("t") or "", root)
                    return hls_relay(channel, url, root, _playback_proxy_url())
                if _playback_proxy():
                    return hls_relay(channel, root, root, _playback_proxy_url())
                return redirect(root, code=302)
            except Exception as exc:
                logger.warning("[SportsGuide] invalid HLS config: %s", exc)
                return Response("HLS 설정을 확인해 주세요.", status=503, mimetype="text/plain")
        return Response("Not found", status=404)
