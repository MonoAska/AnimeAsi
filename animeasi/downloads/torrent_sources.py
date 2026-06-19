import logging
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Optional

import feedparser
import requests

from .torrent_filter import dedupe_torrent_results, is_download_url, download_url_score
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
        "url_template": "https://nyaa.si/?page=rss&q={keyword}&c=0_0&f=0",
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
    if not source.enabled:
        return []

    url = source.url_template.replace("{keyword}", urllib.parse.quote(keyword, safe=""))

    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, proxies=proxies, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logging.error("_search_single_source: source=%s, url=%s, error=%s", source.name, url, e)
        return []

    feed = feedparser.parse(resp.content)
    results = []

    for entry in feed.entries:
        title = entry.get("title", "").strip()
        if not title:
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

    return results


def search_torrents(anime_name: str, sources: Optional[list[dict]] = None, proxies: dict = None) -> tuple[str, list]:
    """
    Search all enabled RSS sources.
    Returns ("success", list) or ("error", []).
    """
    if sources:
        source_objs = [RSSSource(**s) if isinstance(s, dict) else s for s in sources]
    else:
        source_objs = [
            RSSSource(
                name="蜜柑计划",
                url_template="https://mikanani.me/RSS/Search?searchstr={keyword}",
            )
        ]

    all_results = []
    with ThreadPoolExecutor(max_workers=len(source_objs)) as executor:
        futures = {executor.submit(search_single_source, anime_name, src, proxies): src for src in source_objs}
        for future in as_completed(futures):
            src = futures[future]
            try:
                all_results.extend(future.result())
            except Exception as e:
                logging.error("search_torrents: source=%s, error=%s", src.name, e)

    all_results = dedupe_torrent_results(all_results)

    if not all_results:
        return "error", []

    return "success", all_results
