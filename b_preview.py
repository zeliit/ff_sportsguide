"""Isolated B test listing. Never writes Alive or A snapshots."""
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .storage_paths import RUNTIME_DIR
from .b_stream_url import validate_root_stream_url
from .provider_config import provider_page_url, provider_configured

CACHE = RUNTIME_DIR / "b_snapshot.json"
KST = timezone(timedelta(hours=9))


def read_b_preview():
    state = {
        "available": False,
        "error": "",
        "items": [],
        "live": 0,
        "upcoming": 0,
        "total": 0,
        "timestamp": "",
        "age_minutes": None,
        "stale": True,
        "source_url": provider_page_url("b") if provider_configured() else "",
    }
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("중계 B 경기 목록 스냅샷의 형식이 올바르지 않습니다.")
        dt = datetime.fromisoformat(str(data["fetchedAt"]).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("갱신 시각에 시간대 정보가 없습니다.")
        age = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
        clean = []
        for item in data["items"][:500]:
            if not isinstance(item, dict):
                continue
            seq = str(item.get("seq") or "")
            if not re.fullmatch(r"[0-9]{1,10}", seq):
                continue
            stream_url, stream_id = validate_root_stream_url(item.get("streamUrl"), seq)
            row = {
                "seq": seq,
                "state": str(item.get("state") or "")[:20],
                "source_type": str(item.get("sourceType") or "")[:10],
                "league": str(item.get("league") or "")[:90],
                "name": str(item.get("name") or "")[:180],
                "category": str(item.get("category") or "")[:40],
                "thumb_id": str(item.get("thumbId") or "")[:12],
                "stream_id": stream_id,
                "stream_url": stream_url,
                "feeds": max(0, min(10, int(item.get("feeds") or 0))),
            }
            if row["state"] == "ING" and re.fullmatch(r"[0-9]{1,8}", row["thumb_id"]):
                clean.append(row)
        clean.sort(key=lambda x: (x["league"], x["name"]))
        state.update({
            "available": True,
            "items": clean,
            "live": sum(x["state"] == "ING" for x in clean),
            "upcoming": 0,
            "total": len(clean),
            "timestamp": dt.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            "age_minutes": round(age / 60),
            "stale": age > 15 * 60,
        })
    except (OSError, ValueError, TypeError, KeyError) as exc:
        state["error"] = str(exc)[:180]
    return state
