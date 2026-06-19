import re
import urllib.parse


def is_download_url(url: str, link_meta: dict = None) -> bool:
    if not url:
        return False
    if url.startswith("magnet:"):
        return True
    parsed = urllib.parse.urlparse(url)
    if parsed.path.lower().endswith(".torrent"):
        return True
    mime_type = (link_meta or {}).get("type", "").lower()
    return "bittorrent" in mime_type


def download_url_score(url: str) -> int:
    if not url:
        return 0
    if url.startswith("magnet:"):
        return 3
    if urllib.parse.urlparse(url).path.lower().endswith(".torrent"):
        return 2
    return 1


def result_quality(result: dict) -> tuple:
    return (
        1 if result.get("size") else 0,
        download_url_score(result.get("url") or ""),
    )


def normalize_title_for_dedup(title: str) -> str:
    text = (title or "").lower()
    text = re.sub(r"(?i)\b\d+(?:\.\d+)?\s*(?:gb|gib|mb|mib)\b", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def dedup_signature(result: dict) -> tuple:
    meta = result.get("meta") or {}
    group = meta.get("group") or ""
    episode = meta.get("episode_range") or meta.get("episode") or ""
    if group and episode:
        return (
            group,
            episode,
            bool(meta.get("is_batch")),
            meta.get("resolution") or "",
            meta.get("subtitle") or "",
            meta.get("codec") or "",
            meta.get("container") or "",
        )
    return (
        normalize_title_for_dedup(result.get("title") or ""),
    )


def dedupe_torrent_results(results: list[dict]) -> list[dict]:
    deduped = []
    positions = {}
    seen_urls = set()

    for result in results:
        url = result.get("url") or ""
        normalized_url = url.split("#", 1)[0]
        if normalized_url and normalized_url in seen_urls:
            continue
        if normalized_url:
            seen_urls.add(normalized_url)

        signature = dedup_signature(result)
        if signature in positions:
            existing_index = positions[signature]
            existing = deduped[existing_index]
            if result_quality(result) > result_quality(existing):
                deduped[existing_index] = result
            continue

        positions[signature] = len(deduped)
        deduped.append(result)

    return deduped
