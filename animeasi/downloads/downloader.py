"""
多站点 Torrent RSS 搜索 + qBittorrent 推送。

Compatibility facade: public callers can keep importing downloader while the
implementation lives in smaller modules.
"""

from .torrent_filter import (
    dedupe_torrent_results as _dedupe_torrent_results,
    download_url_score as _download_url_score,
    is_download_url as _is_download_url,
    normalize_title_for_dedup as _normalize_title_for_dedup,
)
import feedparser
import requests
from . import torrent_sources
from .torrent_metadata import (
    build_resource_tags,
    extract_entry_size as _extract_entry_size,
    format_size as _format_size,
    parse_torrent_title,
)
from .torrent_sources import (
    PRESET_SOURCES,
    REQUEST_HEADERS,
    RSSSource,
    extract_torrent_url as _extract_torrent_url,
)
from .qbt_client import (
    find_qbt_exe,
    push_to_qbittorrent,
    try_qbt_connect as _try_qbt_connect,
)


def _search_single_source(keyword: str, source: RSSSource, proxies: dict = None) -> list[dict]:
    torrent_sources.requests = requests
    torrent_sources.feedparser = feedparser
    return torrent_sources.search_single_source(keyword, source, proxies)


def search_torrents(anime_name: str, sources: list[dict] = None, proxies: dict = None) -> tuple[str, list]:
    torrent_sources.requests = requests
    torrent_sources.feedparser = feedparser
    return torrent_sources.search_torrents(anime_name, sources, proxies)


__all__ = [
    "PRESET_SOURCES",
    "REQUEST_HEADERS",
    "RSSSource",
    "_dedupe_torrent_results",
    "_download_url_score",
    "_extract_entry_size",
    "_extract_torrent_url",
    "_format_size",
    "_is_download_url",
    "_normalize_title_for_dedup",
    "_search_single_source",
    "_try_qbt_connect",
    "build_resource_tags",
    "feedparser",
    "find_qbt_exe",
    "parse_torrent_title",
    "push_to_qbittorrent",
    "requests",
    "search_torrents",
]
