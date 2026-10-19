"""Server-side collector for provider C.

Only currently live fixtures are persisted. Upcoming fixtures are intentionally ignored.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .storage_paths import RUNTIME_DIR, LOCK_DIR
from .portable_lock import locked_file
from .proxy_config import configured_list_proxy_url, browser_proxy_options
from .b_refresh import _ensure_playwright, _launch_chromium, BROWSER_ROOT, _browser_error_text
from .c_stream_url import validate_root_stream_url
from .provider_config import provider_page_url, require_provider_value

CACHE = RUNTIME_DIR / "c_snapshot.json"
LOCK = LOCK_DIR / "c_refresh.lock"
MIN_REFRESH_SECONDS = 45

INIT_SCRIPT = r"""
(() => {
  try { console.table = () => {}; } catch (e) {}
  try { console.clear = () => {}; } catch (e) {}
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  const originalParse = JSON.parse.bind(JSON);
  window.__sportsGuideCData = null;
  JSON.parse = function(value, ...rest) {
    const parsed = originalParse(value, ...rest);
    try {
      if (parsed && parsed.success === true && Array.isArray(parsed.liveNow) && Array.isArray(parsed.liveSoon)) {
        window.__sportsGuideCData = parsed;
      }
    } catch (e) {}
    return parsed;
  };
})();
"""


def _valid_referer(value):
    from urllib.parse import urlsplit
    url = str(value or "").strip()
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    allowed_host = require_provider_value("c_referer_host").lower()
    path_prefix = require_provider_value("c_referer_path_prefix")
    if (p.scheme != "https" or p.port not in (None, 443) or p.username or p.password
            or not (host == allowed_host or host.endswith("." + allowed_host))
            or not p.path.startswith(path_prefix)):
        raise ValueError("중계 C iframe 주소 형식이 올바르지 않습니다.")
    return url


def _cached_c_fallback(exc):
    try:
        current = json.loads(CACHE.read_text(encoding="utf-8"))
        if not isinstance(current, dict) or not isinstance(current.get("summary"), dict):
            return None
        live = int(current.get("summary", {}).get("ING", 0))
        detail = _browser_error_text(exc)
        return {
            "updated": False,
            "live": live,
            "fallback": True,
            "error": detail,
            "cache_age_seconds": round(max(0.0, time.time() - CACHE.stat().st_mtime), 1),
            "message": "중계 C 갱신 실패 - 이전 정상 캐시를 사용합니다: " + detail[:220],
        }
    except Exception:
        return None


def refresh_c_list(force=False):
    try:
        proxy_url = configured_list_proxy_url()
        if not proxy_url:
            raise RuntimeError("중계 C 목록은 Proxy가 필요합니다. 설정에서 목록 조회 Proxy를 켜 주세요.")

        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with locked_file(LOCK, blocking=False):
            if CACHE.exists() and not force:
                age = time.time() - CACHE.stat().st_mtime
                current = json.loads(CACHE.read_text(encoding="utf-8"))
                if age < MIN_REFRESH_SECONDS and current.get("sourceUrl") == provider_page_url("c"):
                    return {
                        "updated": False,
                        "live": int(current.get("summary", {}).get("ING", 0)),
                        "message": "방금 갱신한 C 목록을 사용합니다.",
                    }

            payload = _collect_c(proxy_url)
            tmp = CACHE.with_name(CACHE.name + ".tmp." + str(os.getpid()))
            try:
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, CACHE)
            finally:
                tmp.unlink(missing_ok=True)
            return {
                "updated": True,
                "live": payload["summary"]["ING"],
                "message": "중계 C 방송 중 경기 목록을 갱신했습니다.",
            }
    except BlockingIOError as exc:
        fallback = _cached_c_fallback(exc)
        if fallback is not None:
            return fallback
        raise RuntimeError("중계 C 목록 갱신이 이미 진행 중입니다. 잠시 후 다시 시도하세요.") from exc
    except Exception as exc:
        fallback = _cached_c_fallback(exc)
        if fallback is not None:
            try:
                from .setup import P
                P.logger.warning("[SportsGuide C] refresh failed; using previous cache: %s", _browser_error_text(exc))
            except Exception:
                pass
            return fallback
        raise


def _collect_c(proxy_url):
    sync_playwright = _ensure_playwright()

    if os.name != "nt":
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSER_ROOT

    page_url = provider_page_url("c")
    start = time.monotonic()
    with sync_playwright() as playwright:
        browser = _launch_chromium(playwright)
        try:
            context_options = {
                "viewport": {"width": 1280, "height": 850},
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150 Safari/537.36",
                "ignore_https_errors": True,
            }
            proxy = browser_proxy_options(proxy_url)
            if proxy:
                context_options["proxy"] = proxy

            context = browser.new_context(**context_options)
            context.add_init_script(INIT_SCRIPT)
            page = context.new_page()
            page.set_default_timeout(7000)
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

            sports_data = None
            frame_url = ""
            for _ in range(36):
                for frame in page.frames:
                    try:
                        candidate_ref = _valid_referer(frame.url)
                    except ValueError:
                        continue
                    try:
                        data = frame.evaluate("() => window.__sportsGuideCData || null")
                    except Exception:
                        data = None
                    if isinstance(data, dict) and isinstance(data.get("liveNow"), list):
                        sports_data = data
                        frame_url = candidate_ref
                        break
                if sports_data is not None:
                    break
                page.wait_for_timeout(500)

            if sports_data is None:
                raise RuntimeError("중계 C 경기 데이터가 브라우저에서 로드되지 않았습니다.")

            referer = _valid_referer(frame_url)
            live = []
            for item in sports_data.get("liveNow", []):
                if not isinstance(item, dict):
                    continue
                fixture_id = str(item.get("id") or "")
                if not re.fullmatch(r"[0-9]{1,12}", fixture_id):
                    continue
                try:
                    stream_url, stream_id, stream_host = validate_root_stream_url(item.get("stream_url"))
                except ValueError:
                    continue
                live.append({
                    "id": fixture_id,
                    "state": "ING",
                    "league": str(item.get("league") or "")[:100],
                    "name": str(item.get("title") or "")[:220],
                    "category": str(item.get("categoryName") or item.get("category") or "")[:60],
                    "scheduleDate": str(item.get("schedule_date") or "")[:20],
                    "scheduleTime": str(item.get("schedule_time") or "")[:20],
                    "streamId": stream_id,
                    "streamHost": stream_host,
                    "streamUrl": stream_url,
                    "referer": referer,
                })

            live.sort(key=lambda x: (x["league"], x["name"], x["id"]))
            live_now = sports_data.get("liveNow") if isinstance(sports_data.get("liveNow"), list) else []
            live_soon = sports_data.get("liveSoon") if isinstance(sports_data.get("liveSoon"), list) else []
            return {
                "source": "스포츠 중계 C",
                "sourceUrl": page_url,
                "frameUrl": referer,
                "fetchedAt": datetime.now(timezone.utc).isoformat(),
                "method": "FF server Chromium + decrypted liveNow capture",
                "summary": {"ING": len(live), "PREP": 0},
                "sourceTotal": len(live_now) + len(live_soon),
                "items": live,
                "elapsedSeconds": round(time.monotonic() - start, 1),
            }
        finally:
            browser.close()
