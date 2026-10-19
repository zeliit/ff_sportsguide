"""Detached first-run Playwright/Chromium/OS dependency preparation.

All potentially heavy installation work runs in this separate process so FF
request workers and the periodic worker never wait for pip/apt/browser downloads.
"""
import os
import traceback

from .portable_lock import locked_file
from .b_refresh import (
    BROWSER_PREPARE_PID,
    BROWSER_PREPARE_WORK_LOCK,
    BROWSER_ROOT,
    PLAYWRIGHT_VERSION,
    _browser_assets_ready,
    _browser_error_kind,
    _browser_error_text,
    _ensure_playwright,
    _install_browser_dependencies,
    _install_chromium,
    _private_playwright_version,
    _write_prepare_state,
)


def _probe_chromium(sync_playwright):
    if os.name != "nt":
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSER_ROOT
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"] if os.name != "nt" else [],
            timeout=15000,
        )
        try:
            page = browser.new_page()
            page.set_content("<title>sports-guide-ready</title>")
            if page.title() != "sports-guide-ready":
                raise RuntimeError("Chromium 실행 테스트 응답이 올바르지 않습니다.")
        finally:
            browser.close()


def main():
    try:
        try:
            os.nice(10)
        except Exception:
            pass

        with locked_file(BROWSER_PREPARE_WORK_LOCK, blocking=False):
            _write_prepare_state(
                "running",
                "SportsGuide Playwright/Chromium을 백그라운드에서 준비하고 있습니다.",
                os.getpid(),
            )

            sync_playwright = _ensure_playwright()
            if not _browser_assets_ready():
                _write_prepare_state(
                    "installing_browser",
                    "Chromium을 백그라운드에서 설치하고 있습니다.",
                    os.getpid(),
                )
                _install_chromium()

            if _private_playwright_version() != PLAYWRIGHT_VERSION or not _browser_assets_ready():
                raise RuntimeError("Playwright/Chromium 준비 완료 상태를 확인하지 못했습니다.")

            _write_prepare_state(
                "testing",
                "Chromium 실행 환경을 확인하고 있습니다.",
                os.getpid(),
            )
            try:
                _probe_chromium(sync_playwright)
            except Exception as exc:
                kind = _browser_error_kind(str(exc))
                if kind == "browser":
                    _write_prepare_state(
                        "installing_browser",
                        "Chromium을 다시 준비하고 있습니다.",
                        os.getpid(),
                    )
                    _install_chromium()
                    sync_playwright = _ensure_playwright()
                    _probe_chromium(sync_playwright)
                elif kind == "deps":
                    _write_prepare_state(
                        "installing_deps",
                        "Chromium OS 의존성을 백그라운드에서 설치하고 있습니다.",
                        os.getpid(),
                    )
                    _install_browser_dependencies()
                    sync_playwright = _ensure_playwright()
                    _probe_chromium(sync_playwright)
                else:
                    raise RuntimeError("Chromium 실행 테스트 실패: " + _browser_error_text(exc)) from exc

            _write_prepare_state(
                "ready",
                "SportsGuide 브라우저 준비가 완료되었습니다.",
                os.getpid(),
            )
            return 0

    except BlockingIOError:
        return 0
    except Exception as exc:
        _write_prepare_state("error", str(exc)[:500], os.getpid())
        try:
            print(traceback.format_exc(), flush=True)
        except Exception:
            pass
        return 1
    finally:
        try:
            BROWSER_PREPARE_PID.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
