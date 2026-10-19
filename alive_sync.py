"""Managed Alive YAML integration for unified FF SportsGuide channels.

New channels use only reserved ``sgu_`` keys. Legacy ``sga_``, ``sgb_`` and
``sgc_`` keys are recognized solely so a successful unified sync can remove
them while preserving unrelated user YAML entries.
"""
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml
from .storage_paths import DB_DIR, LOCK_DIR
from .portable_lock import locked_file

LEGACY_A_KEY_RE = re.compile(r"^sga_(live[0-9]{1,6})$")
LEGACY_B_KEY_RE = re.compile(r"^sgb_[0-9]{1,10}$")
LEGACY_C_KEY_RE = re.compile(r"^sgc_[0-9]{1,12}$")
UNIFIED_KEY_RE = re.compile(r"^sgu_[0-9a-f]{12}$")
# Recognize old A/B/C keys only for migration cleanup. New channels are always sgu_.
MANAGED_KEY_RE = re.compile(r"^(?:sga_live[0-9]{1,6}|sgb_[0-9]{1,10}|sgc_[0-9]{1,12}|sgu_[0-9a-f]{12})$")
ENTRY_RE = re.compile(r"^([ ]{4})([^\s#].*?):\s*(?:#.*)?$")

DEFAULT_PATH = str(DB_DIR / "alive.yaml")
LOCK_PATH = LOCK_DIR / "alive.lock"
BACKUP_KEEP_COUNT = 7


def _prune_alive_backups(path, keep=BACKUP_KEEP_COUNT):
    """Keep only the newest dated SportsGuide backups for alive.yaml."""
    path = Path(path)
    prefix = path.name + ".bak-sportsguide-"
    backups = []
    for item in path.parent.glob(prefix + "*"):
        suffix = item.name[len(prefix):]
        if item.is_file() and re.fullmatch(r"[0-9]{8}", suffix):
            backups.append(item)
    backups.sort(key=lambda item: item.name, reverse=True)
    removed = 0
    for old_backup in backups[max(1, int(keep)):]:
        try:
            old_backup.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _line_body(line):
    return line.rstrip("\r\n")


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _section(lines, parent, child, parent_indent=0, child_indent=2):
    p = next((i for i, line in enumerate(lines)
              if re.match(r"^" + " " * parent_indent + re.escape(parent) + r":\s*(?:#.*)?$", _line_body(line))), None)
    if p is None:
        raise ValueError(f"alive.yaml에 {parent} 구역이 없습니다.")
    end = len(lines)
    for i in range(p + 1, len(lines)):
        body = _line_body(lines[i])
        if body.strip() and not body.lstrip().startswith("#") and _indent(body) <= parent_indent:
            end = i
            break
    c = next((i for i in range(p + 1, end)
              if re.match(r"^" + " " * child_indent + re.escape(child) + r":\s*(?:#.*)?$", _line_body(lines[i]))), None)
    if c is None:
        raise ValueError(f"alive.yaml에 {parent}.{child} 구역이 없습니다.")
    stop = end
    for i in range(c + 1, end):
        body = _line_body(lines[i])
        # In Alive, the next source may be only a commented placeholder.
        if body.strip() and (not body.lstrip().startswith("#") and _indent(body) <= child_indent
                             or body.strip().lower().startswith("# streamlink") and _indent(body) <= child_indent):
            stop = i
            break
    return c, stop


def _ensure_fix_url(lines, parsed, newline, empty=False):
    """Create channel_source.fix_url only when it is absent.

    Keep the existing YAML text, other channel sources, and comments intact.
    Never try to repair invalid YAML automatically.
    """
    source = parsed.get("channel_source")
    if source is not None and not isinstance(source, dict):
        raise ValueError("channel_source는 YAML 매핑이어야 합니다.")
    if isinstance(source, dict) and "fix_url" in source:
        fix = source["fix_url"]
        if fix is not None and not isinstance(fix, dict):
            raise ValueError("channel_source.fix_url은 YAML 매핑이어야 합니다.")
        # A flow-style empty map is valid YAML, but needs a block-style
        # container so that indented sportsguide entries can be added.
        if isinstance(fix, dict) and not fix:
            for i, line in enumerate(lines):
                m = re.match(r"^(  fix_url:)\s*\{\}\s*(#.*)?$", _line_body(line))
                if m:
                    ending = line[len(_line_body(line)):]
                    lines[i] = m.group(1) + (" " + m.group(2) if m.group(2) else "") + (ending or newline)
                    break
        return lines

    location = next((i for i, line in enumerate(lines)
                     if re.match(r"^channel_source:\s*", _line_body(line))), None)
    if location is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        lines.extend([newline, "channel_source:" + newline, ("  fix_url: {}" if empty else "  fix_url:") + newline])
        return lines

    # Both 'channel_source:' and 'channel_source: {}' are common defaults.
    current = _line_body(lines[location])
    if source == {} and re.match(r"^channel_source:\s*\{\}\s*(#.*)?$", current):
        comment = re.match(r"^channel_source:\s*\{\}\s*(#.*)?$", current).group(1)
        eol = lines[location][len(current):] or newline
        lines[location] = "channel_source:" + (" " + comment if comment else "") + eol
    elif current.split("#", 1)[0].strip() not in ("channel_source:", "channel_source: null", "channel_source: ~"):
        raise ValueError("channel_source 인라인 매핑은 수동으로 블록 형태로 변경해 주세요.")
    elif re.match(r"^channel_source:\s*(?:null|~)\s*(#.*)?$", current):
        comment = re.match(r"^channel_source:\s*(?:null|~)\s*(#.*)?$", current).group(1)
        eol = lines[location][len(current):] or newline
        lines[location] = "channel_source:" + (" " + comment if comment else "") + eol

    lines.insert(location + 1, ("  fix_url: {}" if empty else "  fix_url:") + newline)
    return lines


def _fix_blocks(lines):
    start, end = _section(lines, "channel_source", "fix_url")
    starts = []
    for i in range(start + 1, end):
        match = ENTRY_RE.match(_line_body(lines[i]))
        if match:
            starts.append((i, match.group(2)))
    blocks = []
    for idx, (i, key) in enumerate(starts):
        nxt = starts[idx + 1][0] if idx + 1 < len(starts) else end
        blocks.append((i, nxt, key))
    return start, end, blocks


def _quoted(value):
    return "'" + value.replace("'", "''") + "'"


def _updated_text(original, desired):
    # One migration-aware writer: remove legacy sga_/sgb_/sgc_ entries and
    # replace the previous unified sgu_ set with the current desired set.
    key_re = MANAGED_KEY_RE
    newline = "\r\n" if "\r\n" in original else "\n"
    lines = original.splitlines(keepends=True)
    parsed = yaml.safe_load(original)
    if not isinstance(parsed, dict):
        raise ValueError("alive.yaml 최상위 구조는 YAML 매핑이어야 합니다.")
    if desired:
        lines = _ensure_fix_url(lines, parsed, newline)
    elif not isinstance(parsed.get("channel_source"), dict) or "fix_url" not in parsed["channel_source"]:
        # Even when no matches are on air, initialize the empty mapping so
        # Alive can load it and future broadcasts can be registered safely.
        lines = _ensure_fix_url(lines, parsed, newline, empty=True)
        proposed = "".join(lines)
        checked = yaml.safe_load(proposed)
        if not isinstance(checked.get("channel_source", {}).get("fix_url"), dict):
            raise ValueError("새 channel_source.fix_url 섹션 검증 실패")
        return proposed, {"added": 0, "removed": 0, "existing": 0, "total": 0, "changed": proposed != original}
    elif parsed["channel_source"]["fix_url"] == {}:
        return original, {"added": 0, "removed": 0, "existing": 0, "total": 0, "changed": False}
    start, end, blocks = _fix_blocks(lines)
    existing = {key for _, _, key in blocks if key_re.fullmatch(key)}
    added = len(set(desired) - existing)
    removed = len(existing - set(desired))
    updated = len(existing & set(desired))
    # Preserve all non-managed blocks verbatim, including user comments and blank lines.
    for first, last, key in reversed(blocks):
        if key_re.fullmatch(key):
            del lines[first:last]
    start, end, blocks = _fix_blocks(lines)
    # A YAML key without children parses to null, but Alive expects a map.
    # When the last broadcast finishes and no other channels remain, write
    # an explicit empty dictionary instead of leaving fix_url as null.
    if not desired and not blocks:
        raw_header = _line_body(lines[start])
        eol = lines[start][len(raw_header):]
        lines[start] = "  fix_url: {}" + raw_header[len("  fix_url:"):] + eol
    # Append managed entries to the existing fix_url block.
    entries = []
    for key, item in desired.items():
        entries += [
            f"    {key}:{newline}",
            f"      name: {_quoted(item['name'])}{newline}",
            f"      url: {_quoted(item['url'])}{newline}",
        ]
    if entries:
        if end and lines[end - 1].strip():
            entries.insert(0, newline)
        lines[end:end] = entries
    # channel_group is user-managed. SportsGuide only manages fix_url entries.
    proposed = "".join(lines)
    # Empty desired: preserve a preexisting group; it simply contains no channels.
    checked = yaml.safe_load(proposed)
    if not isinstance(checked, dict) or "channel_source" not in checked:
        raise ValueError("생성된 Alive 설정의 YAML 형식이 올바르지 않습니다.")
    resulting = checked["channel_source"]["fix_url"]
    for key, item in desired.items():
        if resulting.get(key, {}) != item:
            raise ValueError(f"생성된 채널 {key} 검증 실패")
    if {k for k in resulting if key_re.fullmatch(k)} != set(desired):
        raise ValueError("관리 채널 정리 결과 검증 실패")
    return proposed, {"added": added, "removed": removed, "existing": updated,
                      "total": len(desired), "changed": proposed != original}


def sync_alive_desired(desired, yaml_path=DEFAULT_PATH, dry_run=False):
    if not isinstance(desired, dict):
        raise ValueError("통합 Alive 채널 데이터 형식이 올바르지 않습니다.")
    for key, item in desired.items():
        if not UNIFIED_KEY_RE.fullmatch(str(key)):
            raise ValueError("통합 Alive 채널 키 형식이 올바르지 않습니다.")
        if not isinstance(item, dict) or not item.get("name") or not item.get("url"):
            raise ValueError("통합 Alive 채널 항목이 올바르지 않습니다.")
    path = Path(yaml_path)
    if not path.is_absolute() or path.name != "alive.yaml":
        raise ValueError("Alive 설정 경로는 alive.yaml의 절대 경로여야 합니다.")
    if not path.is_file():
        raise FileNotFoundError(f"Alive 설정 파일이 없습니다: {path}")
    lock = Path(LOCK_PATH)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(lock):
        original = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(original)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("channel_group"), list):
            raise ValueError("기존 Alive 설정의 channel_group 형식이 올바르지 않습니다.")
        if parsed.get("channel_source") is not None and not isinstance(parsed["channel_source"], dict):
            raise ValueError("기존 Alive 설정의 channel_source 형식이 올바르지 않습니다.")
        new_text, result = _updated_text(original, desired)
        result["path"] = str(path)
        if dry_run or not result["changed"]:
            return result
        backup = path.with_name(path.name + ".bak-sportsguide-" + datetime.now().strftime("%Y%m%d"))
        if not backup.exists():
            shutil.copy2(path, backup)
        backups_pruned = _prune_alive_backups(path)
        fd, temp_name = tempfile.mkstemp(prefix=".alive.sportsguide.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as t:
                t.write(new_text.encode("utf-8"))
                t.flush()
                os.fsync(t.fileno())
            stat = path.stat()
            os.chmod(temp_name, stat.st_mode)
            try:
                os.chown(temp_name, stat.st_uid, stat.st_gid)
            except (PermissionError, AttributeError):
                pass
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        result["backup"] = str(backup)
        if backups_pruned:
            result["backups_pruned"] = backups_pruned
        return result


def state(yaml_path=DEFAULT_PATH, source="U"):
    path = Path(yaml_path)
    status = {"exists": path.is_file(), "count": 0, "path": str(path)}
    if not path.is_file():
        return status

    parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fix = (parsed.get("channel_source") or {}).get("fix_url") or {}
    status["count_a"] = sum(1 for k in fix if LEGACY_A_KEY_RE.fullmatch(str(k)))
    status["count_b"] = sum(1 for k in fix if LEGACY_B_KEY_RE.fullmatch(str(k)))
    status["count_c"] = sum(1 for k in fix if LEGACY_C_KEY_RE.fullmatch(str(k)))
    status["count_u"] = sum(1 for k in fix if UNIFIED_KEY_RE.fullmatch(str(k)))
    status["legacy_count"] = status["count_a"] + status["count_b"] + status["count_c"]
    if source == "A":
        status["count"] = status["count_a"]
    elif source == "B":
        status["count"] = status["count_b"]
    elif source == "C":
        status["count"] = status["count_c"]
    else:
        status["count"] = status["count_u"]
    status["group"] = False
    return status
