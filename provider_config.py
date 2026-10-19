"""Local provider base-URL configuration with built-in integration defaults."""
import json
import os
from urllib.parse import urljoin, urlsplit

from .provider_defaults import DEFAULTS
from .storage_paths import RUNTIME_DIR

CONFIG_PATH = RUNTIME_DIR / "provider.json"


def load_provider_config():
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_provider_config(data):
    if not isinstance(data, dict):
        raise ValueError("provider config must be an object")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp." + str(os.getpid()))
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, CONFIG_PATH)
    finally:
        tmp.unlink(missing_ok=True)


def provider_value(key, default=""):
    local = load_provider_config()
    if key == "c_site_query_value":
        base = str(local.get("base_url") or "").strip()
        host = (urlsplit(base).hostname or "").lower()
        return host or default
    value = local.get(key, DEFAULTS.get(key, default))
    if value is None:
        return default
    return str(value).strip()


def provider_list(key):
    local = load_provider_config()
    value = local.get(key, DEFAULTS.get(key, []))
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def require_provider_value(key):
    value = provider_value(key)
    if not value:
        if key == "base_url":
            raise RuntimeError("메인 URL을 먼저 설정해 주세요.")
        raise RuntimeError("지원 중인 provider 구성값이 없습니다: " + key)
    return value


def set_base_url(base_url):
    # Keep the local runtime file intentionally minimal. Provider layout values
    # ship with the plugin and are updated through normal plugin updates.
    save_provider_config({"base_url": str(base_url or "").strip()})


def migrate_provider_config():
    data = load_provider_config()
    if not data:
        return False
    base = str(data.get("base_url") or "").strip()
    minimal = {"base_url": base} if base else {}
    if data != minimal:
        save_provider_config(minimal)
        return True
    return False


def provider_page_url(source):
    source = str(source or "").lower()
    if source not in ("a", "b", "c"):
        raise ValueError("Unknown provider source")
    base = require_provider_value("base_url").rstrip("/") + "/"
    path = require_provider_value(source + "_page_path").lstrip("/")
    return urljoin(base, path)


def provider_configured():
    return bool(provider_value("base_url"))
