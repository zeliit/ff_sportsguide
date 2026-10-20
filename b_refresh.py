"""Server-side B listing collector. Independent from A/Alive.

Uses a real Chromium engine to render the source site's public fixture iframe,
stores only live match metadata, and atomically replaces the B-only cache.
"""
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from .storage_paths import RUNTIME_DIR, LOCK_DIR
from .portable_lock import locked_file
from .proxy_config import configured_list_proxy_url, browser_proxy_options
from .b_stream_url import validate_root_stream_url
from .provider_config import provider_page_url, require_provider_value

CACHE = RUNTIME_DIR / "b_snapshot.json"
LOCK = LOCK_DIR / "b_refresh.lock"
BROWSER_ROOT = "/data/browsers"
PLAYWRIGHT_VERSION = "1.58.0"
PLAYWRIGHT_PYTHON_ROOT = str(RUNTIME_DIR / "python")
PLAYWRIGHT_INSTALL_LOCK = LOCK_DIR / "playwright_install.lock"
CHROMIUM_INSTALL_LOCK = LOCK_DIR / "chromium_install.lock"
PLAYWRIGHT_DEPS_INSTALL_LOCK = LOCK_DIR / "playwright_deps_install.lock"
MIN_REFRESH_SECONDS = 45
BROWSER_DIAGNOSTIC = RUNTIME_DIR / "browser_last_error.json"
BROWSER_PREPARE_PID = RUNTIME_DIR / "browser_prepare.pid"
BROWSER_PREPARE_STATE = RUNTIME_DIR / "browser_prepare.json"
BROWSER_PREPARE_LOG = RUNTIME_DIR / "browser_prepare.log"
BROWSER_PREPARE_START_LOCK = LOCK_DIR / "browser_prepare_start.lock"
BROWSER_PREPARE_WORK_LOCK = LOCK_DIR / "browser_prepare_work.lock"


def _prepend_playwright_path():
    if os.name == "nt":
        return
    root = Path(PLAYWRIGHT_PYTHON_ROOT)
    if root.is_dir():
        value = str(root)
        if value not in sys.path:
            sys.path.insert(0, value)


def _private_playwright_version():
    root = Path(PLAYWRIGHT_PYTHON_ROOT)
    if not root.is_dir():
        return ""
    try:
        from importlib import metadata as importlib_metadata
        for dist in importlib_metadata.distributions(path=[str(root)]):
            name = str(dist.metadata.get("Name") or "").lower()
            if name == "playwright":
                return str(dist.version or "")
    except Exception:
        pass
    return ""


def _purge_nonprivate_playwright():
    root = str(Path(PLAYWRIGHT_PYTHON_ROOT).resolve())
    module = sys.modules.get("playwright")
    location = str(getattr(module, "__file__", "") or "") if module else ""
    if module is None or location.startswith(root + os.sep):
        return
    for name in list(sys.modules):
        # Some FF plugin loaders may keep non-string objects in sys.modules.
        # Never assume every key implements string methods.
        if not isinstance(name, str):
            continue
        if name == "playwright" or name.startswith("playwright."):
            sys.modules.pop(name, None)


def _import_sync_playwright():
    _prepend_playwright_path()
    if os.name != "nt":
        _purge_nonprivate_playwright()
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        return None


def _ensure_playwright():
    """Use a SportsGuide-private pinned Playwright runtime on Linux."""
    if os.name == "nt":
        sync_playwright = _import_sync_playwright()
        if sync_playwright is None:
            raise RuntimeError("Playwright가 설치되어 있지 않습니다.")
        return sync_playwright

    Path(PLAYWRIGHT_PYTHON_ROOT).parent.mkdir(parents=True, exist_ok=True)
    with locked_file(PLAYWRIGHT_INSTALL_LOCK):
        installed = _private_playwright_version()
        if installed != PLAYWRIGHT_VERSION:
            try:
                from .setup import P
                P.logger.warning(
                    "[SportsGuide] preparing private Playwright %s (current=%s)",
                    PLAYWRIGHT_VERSION, installed or "none",
                )
            except Exception:
                pass
            shutil.rmtree(PLAYWRIGHT_PYTHON_ROOT, ignore_errors=True)
            Path(PLAYWRIGHT_PYTHON_ROOT).mkdir(parents=True, exist_ok=True)
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                     "--no-input", "--upgrade", "--target", PLAYWRIGHT_PYTHON_ROOT,
                     "playwright==" + PLAYWRIGHT_VERSION],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=300,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("Playwright Python 자동 설치를 실행하지 못했습니다.") from exc
            if result.returncode != 0:
                detail = (result.stdout or "").strip().splitlines()
                tail = detail[-1] if detail else "unknown error"
                raise RuntimeError("Playwright Python 자동 설치에 실패했습니다: " + tail[:240])
            importlib.invalidate_caches()

        _prepend_playwright_path()
        sync_playwright = _import_sync_playwright()
        if sync_playwright is None or _private_playwright_version() != PLAYWRIGHT_VERSION:
            raise RuntimeError("SportsGuide 전용 Playwright 1.58.0을 불러오지 못했습니다.")
        return sync_playwright


def _expected_browser_revision():
    if _private_playwright_version() != PLAYWRIGHT_VERSION:
        return ""
    metadata = Path(PLAYWRIGHT_PYTHON_ROOT) / "playwright" / "driver" / "package" / "browsers.json"
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        for item in payload.get("browsers", []):
            if item.get("name") == "chromium-headless-shell":
                return str(item.get("revision") or "")
    except Exception:
        pass
    return ""


def _browser_assets_ready():
    revision = _expected_browser_revision()
    if not revision:
        return False
    base = Path(BROWSER_ROOT) / ("chromium_headless_shell-" + revision)
    if not base.is_dir():
        return False
    for name in ("headless_shell", "chrome-headless-shell"):
        for candidate in base.rglob(name):
            if candidate.is_file() and os.access(str(candidate), os.X_OK):
                return True
    return False


def _prepare_pid_running():
    try:
        pid = int(BROWSER_PREPARE_PID.read_text(encoding="utf-8").strip())
        if pid <= 1:
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        try:
            BROWSER_PREPARE_PID.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _write_prepare_state(status, message="", pid=None):
    try:
        BROWSER_PREPARE_STATE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "status": str(status),
            "message": str(message or "")[:500],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if pid:
            payload["pid"] = int(pid)
        tmp = BROWSER_PREPARE_STATE.with_name(BROWSER_PREPARE_STATE.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, BROWSER_PREPARE_STATE)
    except Exception:
        pass


def _start_browser_prepare(force=False):
    """Start heavy Playwright/Chromium/dependency preparation outside FF."""
    if os.name == "nt":
        return False
    if not force and _browser_assets_ready():
        return False
    try:
        with locked_file(BROWSER_PREPARE_START_LOCK, blocking=False):
            if _prepare_pid_running():
                return False
            if not force and _browser_assets_ready():
                return False
            BROWSER_PREPARE_LOG.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            plugin_root = str(Path(__file__).resolve().parent.parent)
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = plugin_root + (os.pathsep + existing if existing else "")
            cmd = [sys.executable, "-m", "ff_sportsguide.browser_prepare"]
            nice = shutil.which("nice")
            if nice:
                cmd = [nice, "-n", "10"] + cmd
            log_handle = BROWSER_PREPARE_LOG.open("ab", buffering=0)
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=plugin_root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            finally:
                log_handle.close()
            BROWSER_PREPARE_PID.write_text(str(proc.pid), encoding="utf-8")
            _write_prepare_state("starting", "Playwright/Chromium 백그라운드 준비를 시작했습니다.", proc.pid)
            return True
    except BlockingIOError:
        return False
    except Exception as exc:
        _write_prepare_state("error", "브라우저 백그라운드 준비 시작 실패: " + _browser_error_text(exc))
        return False


def _browser_runtime_ready_or_start():
    if os.name == "nt":
        return True
    if _private_playwright_version() == PLAYWRIGHT_VERSION and _browser_assets_ready():
        return True
    _start_browser_prepare()
    return False


def _browser_preparing_result(cache_path, label):
    message = "중계 %s 브라우저를 백그라운드에서 준비 중입니다. FF는 계속 사용할 수 있으며 기존 목록을 유지합니다." % label
    try:
        current = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        live = int(current.get("summary", {}).get("ING", 0))
        age = round(max(0.0, time.time() - Path(cache_path).stat().st_mtime), 1)
    except Exception:
        live = 0
        age = None
    result = {
        "updated": False,
        "live": live,
        "fallback": True,
        "preparing": True,
        "error": "browser preparing",
        "message": message,
    }
    if age is not None:
        result["cache_age_seconds"] = age
    return result


def _playwright_runtime_env():
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = BROWSER_ROOT
    runtime_path = str(Path(PLAYWRIGHT_PYTHON_ROOT))
    existing = env.get("PYTHONPATH", "")
    if Path(runtime_path).is_dir():
        env["PYTHONPATH"] = runtime_path + (os.pathsep + existing if existing else "")
    return env


def _install_chromium():
    """Install the Playwright Chromium build into persistent /data storage."""
    if os.name == "nt":
        return
    _ensure_playwright()
    Path(BROWSER_ROOT).mkdir(parents=True, exist_ok=True)
    with locked_file(CHROMIUM_INSTALL_LOCK):
        env = _playwright_runtime_env()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=420,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Playwright Chromium 자동 설치를 실행하지 못했습니다.") from exc
        if result.returncode != 0:
            detail = (result.stdout or "").strip().splitlines()
            tail = detail[-1] if detail else "unknown error"
            raise RuntimeError("Playwright Chromium 자동 설치에 실패했습니다: " + tail[:240])


def _install_browser_dependencies():
    """Install OS packages required by Playwright Chromium when the container permits it."""
    if os.name == "nt":
        return
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise RuntimeError(
            "Playwright Chromium 시스템 의존성 자동 설치에는 FF 컨테이너의 root 권한이 필요합니다."
        )
    _ensure_playwright()
    with locked_file(PLAYWRIGHT_DEPS_INSTALL_LOCK):
        env = _playwright_runtime_env()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install-deps", "chromium"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Playwright Chromium 시스템 의존성 자동 설치를 실행하지 못했습니다.") from exc
        if result.returncode != 0:
            lines = (result.stdout or "").strip().splitlines()
            tail = " | ".join(lines[-4:]) if lines else "unknown error"
            raise RuntimeError("Playwright Chromium 시스템 의존성 자동 설치에 실패했습니다: " + tail[:400])


def _browser_error_kind(message):
    lower = str(message or "").lower()
    if (
        "host system is missing dependencies to run browsers" in lower
        or "missing libraries" in lower
        or "playwright install-deps" in lower
        or "error while loading shared libraries" in lower
        or "cannot open shared object file" in lower
    ):
        return "deps"
    if (
        "executable doesn't exist" in lower
        or "browser executable" in lower
        or "please run the following command to download new browsers" in lower
    ):
        return "browser"
    return None


def _browser_error_text(exc):
    text = " ".join(str(exc or "unknown error").replace("\r", " ").replace("\n", " | ").split())
    if not text:
        return "unknown error"
    if len(text) <= 700:
        return text
    return text[:340] + " ... " + text[-340:]


def _system_chromium_paths():
    if os.name == "nt":
        return []
    found = []
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        path = shutil.which(name)
        if path and path not in found:
            found.append(path)
    return found


def _save_browser_diagnostic(exc, system_candidates=None):
    try:
        BROWSER_DIAGNOSTIC.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "error": _browser_error_text(exc),
            "system_candidates": list(system_candidates or []),
        }
        tmp = BROWSER_DIAGNOSTIC.with_name(BROWSER_DIAGNOSTIC.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, BROWSER_DIAGNOSTIC)
    except Exception:
        pass


def _clear_browser_diagnostic():
    try:
        BROWSER_DIAGNOSTIC.unlink(missing_ok=True)
    except Exception:
        pass


def _launch_chromium(playwright):
    options = {
        "headless": True,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"] if os.name != "nt" else [],
        "timeout": 15000,
    }
    last_exc = None
    try:
        browser = playwright.chromium.launch(**options)
        _clear_browser_diagnostic()
        return browser
    except Exception as exc:
        last_exc = exc

    system_candidates = _system_chromium_paths()
    for executable in system_candidates:
        try:
            browser = playwright.chromium.launch(executable_path=executable, **options)
            try:
                from .setup import P
                P.logger.warning("[SportsGuide] bundled Chromium failed; using system browser: %s", executable)
            except Exception:
                pass
            _clear_browser_diagnostic()
            return browser
        except Exception as exc:
            last_exc = exc

    kind = _browser_error_kind(str(last_exc))
    if kind == "browser":
        _start_browser_prepare()
    _save_browser_diagnostic(last_exc, system_candidates)
    detail = _browser_error_text(last_exc)
    if kind == "deps":
        _start_browser_prepare(force=True)
        raise RuntimeError(
            "Chromium OS 의존성을 백그라운드에서 준비 중입니다. FF와 기존 목록은 계속 유지됩니다: "
            + detail
        ) from last_exc
    if kind == "browser":
        raise RuntimeError("Chromium을 백그라운드에서 준비 중입니다: " + detail) from last_exc
    raise RuntimeError("Chromium 실행 실패: " + detail) from last_exc


def _cached_b_fallback(exc):
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
            "message": "중계 B 갱신 실패 - 이전 정상 캐시를 사용합니다: " + detail[:220],
        }
    except Exception:
        return None


def refresh_b_list(force=False):
    try:
        if not _browser_runtime_ready_or_start():
            return _browser_preparing_result(CACHE, "B")
        _ensure_playwright()
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        with locked_file(LOCK, blocking=False):
            if CACHE.exists() and not force:
                age = time.time() - CACHE.stat().st_mtime
                current = json.loads(CACHE.read_text(encoding="utf-8"))
                if age < MIN_REFRESH_SECONDS and current.get("sourceUrl") == provider_page_url("b"):
                    return {"updated": False, "live": int(current.get("summary", {}).get("ING", 0)),
                            "message": "방금 갱신한 목록을 사용합니다."}
            payload = _collect_b()
            current_path = CACHE.with_name(CACHE.name + ".tmp." + str(os.getpid()))
            try:
                current_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(current_path, CACHE)
            finally:
                current_path.unlink(missing_ok=True)
            return {"updated": True, "live": payload["summary"]["ING"],
                    "message": "중계 B 방송 중 경기 목록을 갱신했습니다."}
    except BlockingIOError as exc:
        fallback = _cached_b_fallback(exc)
        if fallback is not None:
            return fallback
        raise RuntimeError("중계 B 목록 갱신이 이미 진행 중입니다. 잠시 후 다시 시도하세요.") from exc
    except Exception as exc:
        fallback = _cached_b_fallback(exc)
        if fallback is not None:
            try:
                from .setup import P
                P.logger.warning("[SportsGuide B] refresh failed; using previous cache: %s", _browser_error_text(exc))
            except Exception:
                pass
            return fallback
        raise


def _collect_b():
    sync_playwright = _ensure_playwright()
    # Browser assets are mounted on /data and survive FF container restart.
    # Recreating the container may still require OS shared libraries.
    if os.name != "nt":
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSER_ROOT
    page_url = provider_page_url("b")
    frame_match = require_provider_value("b_frame_match")
    start = time.monotonic()
    with sync_playwright() as playwright:
        browser = _launch_chromium(playwright)
        try:
            context_options = {"viewport": {"width": 1280, "height": 850}}
            proxy = browser_proxy_options(configured_list_proxy_url())
            if proxy:
                context_options["proxy"] = proxy
            context = browser.new_context(**context_options)
            page = context.new_page()
            page.set_default_timeout(6000)
            page.goto(page_url, wait_until="domcontentloaded", timeout=25000)
            frame = None
            for _ in range(24):
                frame = next((f for f in page.frames if frame_match in f.url), None)
                if frame is not None:
                    rows = frame.locator("tr[seq]")
                    if rows.count() > 0:
                        break
                page.wait_for_timeout(600)
            if frame is None or frame.locator("tr[seq]").count() == 0:
                raise RuntimeError("중계 B 경기 목록이 브라우저에서 로드되지 않았습니다.")

            items = frame.evaluate("""async () => {
              const hexBytes = value => {
                const out = new Uint8Array(value.length / 2);
                for (let i = 0; i < out.length; i++) out[i] = parseInt(value.substr(i * 2, 2), 16);
                return out;
              };
              const decryptPlayerV = async value => {
                if (!/^[0-9a-f]+$/i.test(value) || value.length < 64 || value.length % 2 !== 0)
                  throw new Error('invalid player token');
                const iv = new TextEncoder().encode(value.slice(0, 16));
                const keyRaw = new TextEncoder().encode(value.slice(-16));
                const cipher = hexBytes(value.slice(16, -16));
                const key = await crypto.subtle.importKey('raw', keyRaw, {name: 'AES-CBC'}, false, ['decrypt']);
                const plain = await crypto.subtle.decrypt({name: 'AES-CBC', iv}, key, cipher);
                return new TextDecoder().decode(plain).replace(/[\x00-\x1f]+$/g, '');
              };
              const result = [];
              for (const tr of Array.from(document.querySelectorAll('tr[seq]'))) {
                const thumb = tr.querySelector('.thumb img')?.getAttribute('src') || '';
                const m = thumb.match(/\/thumbnails\/(\d{1,8})(?:\D|$)/);
                const state = tr.getAttribute('states') || '';
                let streamUrl = '';
                let streamError = '';
                if (state === 'ING') {
                  try {
                    const anchor = tr.querySelector('a[onclick^="openVideo"]');
                    if (!anchor) throw new Error('missing live feed');
                    const encrypted = anchor.getAttribute('data') || '';
                    const html = getDecryptData(encrypted);
                    const holder = document.createElement('div');
                    holder.innerHTML = html;
                    const playerUrl = holder.querySelector('iframe')?.getAttribute('src') || '';
                    const token = new URL(playerUrl, location.href).searchParams.get('v') || '';
                    streamUrl = await decryptPlayerV(token);
                  } catch (error) {
                    streamError = String(error).slice(0, 180);
                  }
                }
                result.push({
                  seq: tr.getAttribute('seq') || '',
                  state,
                  sourceType: tr.getAttribute('sourcetype') || '',
                  category: tr.getAttribute('cate') || '',
                  league: tr.querySelector('.league_name')?.innerText?.trim() || '',
                  name: tr.querySelector('.subject')?.innerText?.trim() || '',
                  thumbId: m ? m[1] : '',
                  feeds: tr.querySelectorAll('a[onclick^="openVideo"]').length,
                  streamUrl,
                  streamError,
                });
              }
              return result;
            }""")
            if not isinstance(items, list) or not items:
                raise RuntimeError("중계 B 경기 목록 파싱 결과가 없습니다.")
            valid_states = [x for x in items if x.get("state") in ("ING", "PREP")]
            if not valid_states:
                raise RuntimeError("중계 B 경기 상태 확인에 실패했습니다.")
            live = []
            for item in valid_states:
                if item["state"] != "ING":
                    continue
                if not re.fullmatch(r"[0-9]{1,10}", str(item.get("seq") or "")):
                    raise RuntimeError("Live B fixture ID invalid; retaining old list")
                if not re.fullmatch(r"[0-9]{1,8}", str(item.get("thumbId") or "")):
                    raise RuntimeError("Live B media ID invalid; retaining old list")
                try:
                    stream_url, stream_id = validate_root_stream_url(item.get("streamUrl"), str(item["seq"]))
                except ValueError as exc:
                    detail = str(item.get("streamError") or exc)[:160]
                    raise RuntimeError("Live B stream URL invalid; retaining old list: " + detail) from exc
                live.append({
                    "seq": str(item["seq"]),
                    "state": "ING",
                    "sourceType": str(item.get("sourceType") or "")[:10],
                    "league": str(item.get("league") or "")[:90],
                    "name": str(item.get("name") or "")[:180],
                    "category": str(item.get("category") or "")[:40],
                    "thumbId": str(item["thumbId"]),
                    "streamId": stream_id,
                    "streamUrl": stream_url,
                    "feeds": min(10, int(item.get("feeds") or 0)),
                })
            return {
                "source": "스포츠 중계 B",
                "sourceUrl": page_url,
                "fetchedAt": datetime.now(timezone.utc).isoformat(),
                "method": "FF server Chromium + player token decrypt",
                "summary": {"ING": len(live), "PREP": 0},
                "domRowCount": len(items),
                "items": live,
                "elapsedSeconds": round(time.monotonic() - start, 1),
            }
        finally:
            browser.close()
