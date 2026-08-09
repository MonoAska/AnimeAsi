import logging
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import feedparser
import requests

from .torrent_filter import (
    dedupe_torrent_results,
    download_url_score,
    is_download_url,
    is_probable_anime_video_title,
)
from .torrent_metadata import build_resource_tags, extract_entry_size, parse_torrent_title


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}


PRESET_SOURCES = [
    {
        "name": "蜜柑计划",
        "url_template": "https://mikanani.me/RSS/Search?searchstr={keyword}",
    },
    {
        "name": "Nyaa.si",
        "url_template": "https://nyaa.si/?page=rss&q={keyword}&c=1_0&f=0",
    },
    {
        "name": "动漫花园",
        "url_template": "https://dmhy.org/topics/rss/rss.xml?keyword={keyword}",
    },
    {
        "name": "TokyoTosho",
        "url_template": "https://www.tokyotosho.info/rss.php?filter=1&search={keyword}",
    },
    {
        "name": "ACG.RIP",
        "url_template": "https://acg.rip/rss.xml?name={keyword}",
    },
]


@dataclass
class RSSSource:
    name: str
    url_template: str
    enabled: bool = True


def extract_torrent_url(entry) -> Optional[str]:
    """Extract a torrent/magnet URL that can be sent directly to a downloader."""
    candidates = []
    for enc in entry.get("enclosures", []):
        candidates.append(enc)
    for link in entry.get("links", []):
        candidates.append(link)

    best_url = None
    best_score = 0
    for item in candidates:
        href = (item.get("href", "") or "").strip()
        if not is_download_url(href, item):
            continue
        score = download_url_score(href)
        if score > best_score:
            best_url = href
            best_score = score

    return best_url


def search_single_source(keyword: str, source: RSSSource, proxies: dict = None) -> list[dict]:
    """Search one RSS source and return normalized torrent results."""
    results, _ = search_single_source_with_error(keyword, source, proxies)
    return results


def search_single_source_with_error(
    keyword: str,
    source: RSSSource,
    proxies: dict = None,
) -> tuple[list[dict], str]:
    """Search one RSS source and return normalized results plus an error message."""
    if not source.enabled:
        return [], ""

    template = source.url_template
    if source.name.lower().startswith("nyaa"):
        template = template.replace("c=0_0", "c=1_0")
    url = template.replace("{keyword}", urllib.parse.quote(keyword, safe=""))

    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, proxies=proxies, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logging.error("_search_single_source: source=%s, url=%s, error=%s", source.name, url, e)
        return [], str(e)

    feed = feedparser.parse(resp.content)
    results = []

    for entry in feed.entries:
        title = entry.get("title", "").strip()
        if not title or not is_probable_anime_video_title(title):
            continue

        torrent_url = extract_torrent_url(entry)
        if not torrent_url:
            continue

        meta = parse_torrent_title(title)
        size = extract_entry_size(entry)
        results.append({
            "title": title,
            "url": torrent_url,
            "source": source.name,
            "size": size,
            "meta": meta,
            "resource_tags": build_resource_tags(meta, source.name, size),
        })

    return results, ""


def search_torrents(keywords, sources: Optional[list[dict]] = None, proxies: dict = None) -> tuple[str, list, list]:
    """Search enabled RSS sources for multiple aliases and merge the results.

    Returns ``(status, results, source_stats)`` where ``source_stats`` is a
    per-source report: ``name``, ``ok``, ``total_queries``, ``failed_queries``,
    ``result_count`` and the first ``error`` message (if any).
    """
    if isinstance(keywords, str):
        keywords = [keywords]
    normalized_keywords = []
    seen_keywords = set()
    for value in keywords or []:
        keyword = str(value or "").strip()
        key = keyword.casefold()
        if keyword and key not in seen_keywords:
            seen_keywords.add(key)
            normalized_keywords.append(keyword)
    if not normalized_keywords:
        return "empty", [], []

    if sources:
        source_objs = [RSSSource(**s) if isinstance(s, dict) else s for s in sources]
    else:
        source_objs = [
            RSSSource(
                name="蜜柑计划",
                url_template="https://mikanani.me/RSS/Search?searchstr={keyword}",
            )
        ]
    active_sources = [source for source in source_objs if source.enabled]
    if not active_sources:
        return "empty", [], []

    batches = {}
    failed_requests = 0
    source_failures = {
        source.name: {"failed_queries": 0, "first_error": ""}
        for source in active_sources
    }
    source_query_counts = {source.name: 0 for source in active_sources}
    source_result_counts = {source.name: 0 for source in active_sources}
    task_count = len(normalized_keywords) * len(active_sources)
    max_workers = min(12, task_count)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(search_single_source_with_error, keyword, source, proxies): (
                keyword_index,
                source_index,
                keyword,
                source,
            )
            for keyword_index, keyword in enumerate(normalized_keywords)
            for source_index, source in enumerate(active_sources)
        }
        for future in as_completed(futures):
            keyword_index, source_index, keyword, source = futures[future]
            try:
                results, error = future.result()
                if error:
                    failed_requests += 1
                    source_failures[source.name]["failed_queries"] += 1
                    if not source_failures[source.name]["first_error"]:
                        source_failures[source.name]["first_error"] = error
                source_query_counts[source.name] += 1
                source_result_counts[source.name] += len(results)
                batches[(keyword_index, source_index)] = [
                    {**result, "matched_keyword": keyword} for result in results
                ]
            except Exception as e:
                logging.error(
                    "search_torrents: keyword=%s, source=%s, error=%s",
                    keyword,
                    source.name,
                    e,
                )
                failed_requests += 1
                source_failures[source.name]["failed_queries"] += 1
                if not source_failures[source.name]["first_error"]:
                    source_failures[source.name]["first_error"] = str(e)
                source_query_counts[source.name] += 1
                batches[(keyword_index, source_index)] = []

    all_results = []
    for key in sorted(batches):
        all_results.extend(batches[key])
    all_results = dedupe_torrent_results(all_results)

    source_stats = [
        {
            "name": source.name,
            "ok": source_failures[source.name]["failed_queries"] == 0,
            "total_queries": source_query_counts[source.name],
            "failed_queries": source_failures[source.name]["failed_queries"],
            "result_count": source_result_counts[source.name],
            "error": source_failures[source.name]["first_error"],
        }
        for source in active_sources
    ]
    if not all_results:
        return ("error" if failed_requests else "empty"), [], source_stats
    status = "partial" if any(not stat["ok"] for stat in source_stats) else "success"
    return status, all_results, source_stats
