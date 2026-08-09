"""Extract safe torrent-search aliases from Bangumi subject metadata."""

import re


_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def extract_search_aliases(subject: dict) -> list[str]:
    """Return Chinese and Latin aliases while excluding Japanese kana names."""
    original = str(subject.get("name") or "").strip()
    candidates = [subject.get("name_cn")]
    for item in subject.get("infobox") or []:
        if str(item.get("key") or "").strip() not in {"别名", "英文名", "罗马字", "罗马音"}:
            continue
        value = item.get("value")
        values = value if isinstance(value, list) else [value]
        for entry in values:
            candidates.append(entry.get("v") if isinstance(entry, dict) else entry)

    result = []
    seen = set()
    for value in candidates:
        alias = re.sub(r"\s+", " ", str(value or "")).strip()
        key = alias.casefold()
        if not alias or alias == original or _KANA_RE.search(alias):
            continue
        if not (_CJK_RE.search(alias) or _LATIN_RE.search(alias)) or key in seen:
            continue
        seen.add(key)
        result.append(alias)
    return result


def select_search_keywords(primary: str, aliases, limit: int = 4) -> list[str]:
    result = []
    seen = set()
    for value in [primary, *(aliases or [])]:
        keyword = re.sub(r"\s+", " ", str(value or "")).strip()
        key = keyword.casefold()
        if not keyword or key in seen:
            continue
        seen.add(key)
        result.append(keyword)
        if len(result) >= limit:
            break
    return result