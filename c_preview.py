"""Read the isolated C live-only snapshot."""
import json
import re
from datetime import datetime, timezone, timedelta

from .storage_paths import RUNTIME_DIR
from .c_stream_url import validate_root_stream_url
from .provider_config import provider_page_url, provider_configured, require_provider_value

CACHE = RUNTIME_DIR / "c_snapshot.json"
KST = timezone(timedelta(hours=9))


def _valid_referer(value):
    from urllib.parse import urlsplit
    url = str(value or "").strip()
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    allowed_host = require_provider_value("c_referer_host").lower()
    path_prefix = require_provider_value("c_referer_path_prefix")
    if (p.scheme != "https" or p.port not in (None, 443)
            or not (host == allowed_host or host.endswith("." + allowed_host))
            or not p.path.startswith(path_prefix)):
        raise ValueError("중계 C iframe 주소 형식 오류")
    return url


def read_c_preview():
    state = {
        "available": False,
        "pending": not CACHE.exists(),
        "error": "",
        "items": [],
        "live": 0,
        "upcoming": 0,
        "total": 0,
        "timestamp": "",
        "age_minutes": None,
        "stale": True,
        "source_url": provider_page_url("c") if provider_configured() else "",
    }
    if not CACHE.exists():
        state["error"] = "중계 C 경기 목록을 처음 갱신하는 중입니다."
        return state
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("중계 C 경기 목록 스냅샷의 형식이 올바르지 않습니다.")
        dt = datetime.fromisoformat(str(data["fetchedAt"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("갱신 시각에 시간대 정보가 없습니다.")
        age = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
        clean = []
        for item in data["items"][:300]:
            if not isinstance(item, dict):
                continue
            fixture_id = str(item.get("id") or "")
            if not re.fullmatch(r"[0-9]{1,12}", fixture_id):
                continue
            stream_url, stream_id, stream_host = validate_root_stream_url(item.get("streamUrl"))
            referer = _valid_referer(item.get("referer"))
            clean.append({
                "id": fixture_id,
                "state": "ING",
                "league": str(item.get("league") or "")[:100],
                "name": str(item.get("name") or "")[:220],
                "category": str(item.get("category") or "")[:60],
                "schedule_date": str(item.get("scheduleDate") or "")[:20],
                "schedule_time": str(item.get("scheduleTime") or "")[:20],
                "stream_id": stream_id,
                "stream_host": stream_host,
                "stream_url": stream_url,
                "referer": referer,
            })
        clean.sort(key=lambda x: (x["league"], x["name"], x["id"]))
        state.update({
            "available": True,
            "items": clean,
            "live": len(clean),
            "upcoming": 0,
            "total": len(clean),
            "timestamp": dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            "age_minutes": round(age / 60),
            "stale": age > 15 * 60,
        })
    except (OSError, ValueError, TypeError, KeyError) as exc:
        state["error"] = str(exc)[:180]
    return state
