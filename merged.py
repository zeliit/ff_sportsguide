"""Merge live A/B/C SportsGuide fixtures with conservative duplicate filtering."""
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .storage_paths import RUNTIME_DIR
from .b_preview import read_b_preview
from .c_preview import read_c_preview

MERGED_CACHE = RUNTIME_DIR / "merged.json"
A_CACHE = RUNTIME_DIR / "channels.json"
B_CACHE = RUNTIME_DIR / "b_snapshot.json"
C_CACHE = RUNTIME_DIR / "c_snapshot.json"
MAX_AGE = 15 * 60
FALLBACK_MAX_AGE = 6 * 60 * 60

SPORT_MAP = {
    "축구": "축구", "soccer": "축구", "football": "축구",
    "농구": "농구", "basketball": "농구", "basket": "농구",
    "배구": "배구", "volleyball": "배구", "volley": "배구",
    "야구": "야구", "baseball": "야구",
    "테니스": "테니스", "tennis": "테니스",
    "탁구": "탁구", "table tennis": "탁구",
    "배드민턴": "배드민턴", "badminton": "배드민턴",
    "아이스하키": "아이스하키", "ice hockey": "아이스하키",
    "하키": "하키", "hockey": "하키",
    "미식축구": "미식축구", "american football": "미식축구",
    "골프": "골프", "golf": "골프",
    "복싱": "복싱", "boxing": "복싱",
    "격투기": "격투기", "mma": "격투기", "ufc": "격투기",
    "e스포츠": "e스포츠", "esports": "e스포츠", "e-sports": "e스포츠",
    "lol": "e스포츠",
}
LEAGUE_ALIAS = {
    "WAT": "WTA",
}


def _dt(value):
    d = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("snapshot timestamp missing timezone")
    return d


def _age(ts):
    return time.time() - float(ts)


def _sport(value):
    raw = " ".join(str(value or "").split()).strip()
    low = raw.casefold()
    return SPORT_MAP.get(low, SPORT_MAP.get(raw, raw or "기타"))


def _league(value):
    raw = unicodedata.normalize("NFKC", str(value or "")).upper().strip()
    raw = re.sub(r"\b20[0-9]{2}\b", "", raw)
    raw = re.sub(r"[^0-9A-Z가-힣]+", "", raw)
    return LEAGUE_ALIAS.get(raw, raw)


def _title(value):
    raw = unicodedata.normalize("NFKC", str(value or "")).casefold()
    raw = re.sub(r"\b20[0-9]{2}\b", " ", raw)
    raw = raw.replace("versus", " vs ")
    raw = re.sub(r"\s+v\.?\s+", " vs ", raw)
    raw = re.sub(r"[^0-9a-z가-힣]+", " ", raw)
    return " ".join(raw.split())


def _script(value):
    s = str(value or "")
    has_ko = bool(re.search(r"[가-힣]", s))
    has_lat = bool(re.search(r"[A-Za-z]", s))
    if has_ko and not has_lat:
        return "ko"
    if has_lat and not has_ko:
        return "latin"
    return "mixed"


def _matchup(value):
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    parts = re.split(r"\s+(?:vs\.?|versus|v\.?)\s+", raw, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    left, right = (_title(parts[0]), _title(parts[1]))
    if not left or not right:
        return None
    return left, right


def _minutes(value):
    m = re.fullmatch(r"\s*([0-2]?[0-9]):([0-5][0-9])\s*", str(value or ""))
    if not m:
        return None
    h, minute = int(m.group(1)), int(m.group(2))
    if h > 23:
        return None
    return h * 60 + minute


def _time_diff(a, b):
    ma, mb = _minutes(a.get("start_time")), _minutes(b.get("start_time"))
    if ma is None or mb is None:
        return None
    diff = abs(ma - mb)
    return min(diff, 1440 - diff)


def _bwf_stream_number(value):
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    m = re.search(r"(?:court|코트)\s*([0-9]{1,2})\b", raw, flags=re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"\s([0-9]{1,2})\s*$", raw)
    return int(m.group(1)) if m else None


def _bwf_base_title(value):
    raw = _title(value)
    raw = re.sub(r"\bcourt\s*[0-9]{1,2}\b", " ", raw, flags=re.I)
    raw = re.sub(r"\s+[0-9]{1,2}\s*$", " ", raw)
    return " ".join(raw.split())


def _league_conflict(a, b):
    la, lb = a.get("league_norm") or "", b.get("league_norm") or ""
    return bool(la and lb and la != lb)


def _strong_match(a, b):
    # Duplicate identity is title-first. Upstream sport/category values are not
    # reliable enough to block a merge (e.g. NFL may arrive as football/Rugby).
    if a["source"] == b["source"]:
        return False

    # BWF feeds sometimes name the same court as "Court 1" vs just "1".
    # Different court numbers are always different events.
    if a.get("league_norm", "").startswith("BWF") and b.get("league_norm", "").startswith("BWF"):
        na, nb = _bwf_stream_number(a.get("title")), _bwf_stream_number(b.get("title"))
        if na is not None and nb is not None:
            if na != nb:
                return False
            ba, bb = _bwf_base_title(a.get("title")), _bwf_base_title(b.get("title"))
            if ba and bb and SequenceMatcher(None, ba, bb).ratio() >= 0.75:
                return True

    ta, tb = a["title_norm"], b["title_norm"]
    if not ta or not tb:
        return False

    # Exact normalized title wins even when upstream category/league metadata is
    # inconsistent. These are live fixtures from different sources.
    if ta == tb:
        return True

    if _script(a["title"]) != _script(b["title"]):
        return False

    # Fuzzy title matching is more conservative: if both feeds provide a league
    # and disagree, keep them separate. Known times that are far apart also block.
    if _league_conflict(a, b):
        return False
    diff = _time_diff(a, b)
    if diff is not None and diff > 90:
        return False

    ratio = SequenceMatcher(None, ta, tb).ratio()
    if ratio >= 0.86:
        return True
    pa, pb = a.get("matchup"), b.get("matchup")
    if pa and pb:
        direct = (SequenceMatcher(None, pa[0], pb[0]).ratio() +
                  SequenceMatcher(None, pa[1], pb[1]).ratio()) / 2
        reverse = (SequenceMatcher(None, pa[0], pb[1]).ratio() +
                   SequenceMatcher(None, pa[1], pb[0]).ratio()) / 2
        return max(direct, reverse) >= 0.86
    return False


def _cross_candidate(a, b):
    if a["source"] == b["source"]:
        return False
    if not a.get("matchup") or not b.get("matchup"):
        return False
    if _script(a["title"]) == _script(b["title"]):
        return False
    if _league_conflict(a, b):
        return False
    # For cross-script titles, same/unknown league plus a unique matchup is used.
    # If both sources have times, require them to be close; a missing B time must
    # not prevent an otherwise unique match.
    diff = _time_diff(a, b)
    return diff is None or diff <= 30


def _source_event(source, ident, title, category, league="", start_time="", extra=None):
    league_norm = _league(league)
    sport = _sport(category)
    # B/C occasionally mislabel BWF badminton events as Tennis.
    # Keep this display/metadata correction separate from duplicate matching.
    if league_norm.startswith("BWF"):
        sport = "배드민턴"
    return {
        "source": source,
        "id": str(ident),
        "title": " ".join(str(title or "").split())[:220],
        "sport": sport,
        "league": " ".join(str(league or "").split())[:100],
        "league_norm": league_norm,
        "start_time": str(start_time or "")[:20],
        "title_norm": _title(title),
        "matchup": _matchup(title),
        "extra": dict(extra or {}),
    }


def load_sources(enabled_sources=None, allow_stale_sources=None):
    enabled = {str(x).upper() for x in (enabled_sources or ("A", "B", "C"))}
    enabled &= {"A", "B", "C"}
    if not enabled:
        raise ValueError("활성화된 중계 소스가 없습니다.")
    allow_stale = {str(x).upper() for x in (allow_stale_sources or ())} & enabled

    now = time.time()
    status = {}
    events = []

    if "A" in enabled:
        a = json.loads(A_CACHE.read_text(encoding="utf-8"))
        a_age = now - float(a.get("fetched_ts") or 0)
        a_fallback = "A" in allow_stale
        if int(a.get("rows_total") or 0) <= 0:
            raise ValueError("A 목록 데이터가 없습니다.")
        if a_fallback:
            if not -60 < a_age <= FALLBACK_MAX_AGE:
                raise ValueError("A 이전 정상 캐시가 너무 오래되었습니다.")
        elif a.get("last_error") or not -60 < a_age <= MAX_AGE:
            raise ValueError("A 목록이 오래되었거나 조회 오류가 있습니다.")
        for x in a.get("channels", []):
            if x.get("status") != "방송 중":
                continue
            ch = str(x.get("channel") or "")
            if not re.fullmatch(r"live[0-9]{1,6}", ch):
                continue
            events.append(_source_event(
                "A", ch, x.get("program"), x.get("category"), x.get("league"), x.get("start_time"),
                {"channel": ch},
            ))
        status["A"] = {"live": len([x for x in events if x["source"] == "A"]), "age": round(a_age, 1),
                       "fallback": a_fallback, "stale": a_age > MAX_AGE or bool(a.get("last_error"))}

    if "B" in enabled:
        b_raw = json.loads(B_CACHE.read_text(encoding="utf-8"))
        b_dt = _dt(b_raw.get("fetchedAt"))
        b_age = now - b_dt.timestamp()
        b = read_b_preview()
        b_fallback = "B" in allow_stale
        if not b.get("available") or int(b_raw.get("domRowCount") or 0) <= 0:
            raise ValueError("B 목록 데이터가 없습니다.")
        if b_fallback:
            if not -60 < b_age <= FALLBACK_MAX_AGE:
                raise ValueError("B 이전 정상 캐시가 너무 오래되었습니다.")
        elif b.get("stale") or not -60 < b_age <= MAX_AGE:
            raise ValueError("B 목록이 오래되었거나 조회 오류가 있습니다.")
        for x in b.get("items", []):
            if x.get("state") != "ING":
                continue
            events.append(_source_event(
                "B", x.get("seq"), x.get("name"), x.get("category"), x.get("league"), "",
                {"seq": str(x.get("seq") or "")},
            ))
        status["B"] = {"live": len([x for x in events if x["source"] == "B"]), "age": round(b_age, 1),
                       "fallback": b_fallback, "stale": b_age > MAX_AGE or bool(b.get("stale"))}

    if "C" in enabled:
        c_raw = json.loads(C_CACHE.read_text(encoding="utf-8"))
        c_dt = _dt(c_raw.get("fetchedAt"))
        c_age = now - c_dt.timestamp()
        c = read_c_preview()
        source_total = int(c_raw.get("sourceTotal") or 0)
        c_listing_ok = source_total > 0 or bool(c_raw.get("items"))
        c_fallback = "C" in allow_stale
        if not c.get("available") or not c_listing_ok:
            raise ValueError("C 목록 데이터가 없습니다.")
        if c_fallback:
            if not -60 < c_age <= FALLBACK_MAX_AGE:
                raise ValueError("C 이전 정상 캐시가 너무 오래되었습니다.")
        elif c.get("stale") or not -60 < c_age <= MAX_AGE:
            raise ValueError("C 목록이 오래되었거나 조회 오류가 있습니다.")
        for x in c.get("items", []):
            if x.get("state") != "ING":
                continue
            events.append(_source_event(
                "C", x.get("id"), x.get("name"), x.get("category"), x.get("league"), x.get("schedule_time"),
                {"id": str(x.get("id") or "")},
            ))
        status["C"] = {"live": len([x for x in events if x["source"] == "C"]), "age": round(c_age, 1),
                       "source_total": source_total, "fallback": c_fallback,
                       "stale": c_age > MAX_AGE or bool(c.get("stale"))}

    return events, status


def merge_events(events, preferred="A"):
    preferred = str(preferred or "A").upper()
    if preferred not in ("A", "B", "C"):
        preferred = "A"
    order = [preferred] + [x for x in ("A", "B", "C") if x != preferred]

    n = len(events)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Exact / high-confidence same-script matches first.
    for i in range(n):
        for j in range(i + 1, n):
            if _strong_match(events[i], events[j]):
                union(i, j)

    # Cross-language matchup matching is deliberately conservative.
    # For each source pair, only join candidates that are unique on both sides.
    pairs = (("A", "B"), ("A", "C"), ("B", "C"))
    for sa, sb in pairs:
        ia = [i for i, e in enumerate(events) if e["source"] == sa]
        ib = [i for i, e in enumerate(events) if e["source"] == sb]
        ca = {i: [j for j in ib if _cross_candidate(events[i], events[j])] for i in ia}
        cb = {j: [i for i in ia if _cross_candidate(events[i], events[j])] for j in ib}
        for i, js in ca.items():
            if len(js) != 1:
                continue
            j = js[0]
            if len(cb.get(j, [])) == 1:
                union(i, j)

    groups = {}
    for i, event in enumerate(events):
        groups.setdefault(find(i), []).append(event)

    items = []
    duplicate_count = 0
    for members in groups.values():
        by_source = {}
        for m in members:
            by_source.setdefault(m["source"], []).append(m)
        chosen_source = next((s for s in order if s in by_source), members[0]["source"])
        chosen = sorted(by_source[chosen_source], key=lambda x: x["id"])[0]
        source_tokens = sorted(f"{m['source']}:{m['id']}" for m in members)
        key_seed = "|".join(source_tokens)
        key = "sgu_" + hashlib.sha1(key_seed.encode("utf-8")).hexdigest()[:12]
        duplicate_count += max(0, len(members) - 1)
        items.append({
            "key": key,
            "sport": chosen.get("sport") or "기타",
            "title": chosen.get("title") or chosen["id"],
            "league": chosen.get("league") or "",
            "start_time": chosen.get("start_time") or "",
            "chosen_source": chosen_source,
            "selected_id": chosen["id"],
            "selected_extra": chosen.get("extra") or {},
            "available_sources": [s for s in order if s in by_source],
            "member_count": len(members),
            "members": [{"source": m["source"], "id": m["id"], "title": m["title"],
                         "sport": m["sport"], "league": m["league"], "start_time": m["start_time"]}
                        for m in members],
        })

    items.sort(key=lambda x: (x["sport"], x["league"], x["title"], x["key"]))
    return items, duplicate_count


def build_merged(preferred="A", enabled_sources=None, allow_stale_sources=None):
    events, status = load_sources(enabled_sources=enabled_sources, allow_stale_sources=allow_stale_sources)
    items, duplicate_count = merge_events(events, preferred)
    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "fetched_ts": time.time(),
        "preferred_source": str(preferred or "A").upper(),
        "source_status": status,
        "raw_live_count": len(events),
        "duplicate_count": duplicate_count,
        "live_count": len(items),
        "items": items,
    }


def save_merged(data):
    MERGED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MERGED_CACHE.with_name(MERGED_CACHE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(MERGED_CACHE)


def read_merged():
    try:
        return json.loads(MERGED_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
