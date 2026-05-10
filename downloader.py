"""
多站点 Torrent RSS 搜索 + qBittorrent 推送
支持动态配置多个 RSS 订阅源
"""

import feedparser
import requests
import re
import urllib.parse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from typing import Optional

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

# ─── 预设 RSS 订阅源（供前端快捷添加） ─────────────────

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


# ─── 通用 RSS 解析 ───────────────────────────────────────

def _extract_torrent_url(entry) -> Optional[str]:
    """从 feed 条目中智能提取种子/磁力链接。"""
    # 1. enclosures（标准 RSS 2.0）
    for enc in entry.get("enclosures", []):
        href = enc.get("href", "")
        if href.endswith(".torrent") or href.startswith("magnet:"):
            return href

    # 2. links 列表
    for ln in entry.get("links", []):
        href = ln.get("href", "")
        if href.endswith(".torrent"):
            return href

    # 3. magnet links
    for ln in entry.get("links", []):
        href = ln.get("href", "")
        if href.startswith("magnet:"):
            return href

    return None


def _search_single_source(keyword: str, source: RSSSource, proxies: dict = None) -> list[dict]:
    """搜索单个 RSS 源，返回统一格式的结果列表。"""
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

        url = _extract_torrent_url(entry) or entry.get("link", "")
        results.append({
            "title": title,
            "url": url,
            "source": source.name,
        })

    return results


# ─── 对外接口 ────────────────────────────────────────────

def search_torrents(anime_name: str, sources: Optional[list[dict]] = None, proxies: dict = None) -> tuple[str, list]:
    """
    搜索引擎：遍历所有启用的 RSS 订阅源，返回聚合结果。
    返回 ("success", list) 或 ("error", msg)
    """
    if sources:
        source_objs = [RSSSource(**s) if isinstance(s, dict) else s for s in sources]
    else:
        # 向后兼容：无配置时默认只搜蜜柑
        source_objs = [
            RSSSource(
                name="蜜柑计划",
                url_template="https://mikanani.me/RSS/Search?searchstr={keyword}",
            )
        ]

    all_results = []
    with ThreadPoolExecutor(max_workers=len(source_objs)) as executor:
        futures = {executor.submit(_search_single_source, anime_name, src, proxies): src for src in source_objs}
        for future in as_completed(futures):
            src = futures[future]
            try:
                all_results.extend(future.result())
            except Exception as e:
                logging.error("search_torrents: source=%s, error=%s", src.name, e)

    if not all_results:
        return "error", []

    return "success", all_results


def push_to_qbittorrent(torrent_url: str, qbt_config: dict) -> tuple[str, str]:
    """
    下载引擎：接收种子/磁力链接，推送到 qBittorrent。
    返回 ("success", msg) 或 ("error", msg)
    """
    import qbittorrentapi

    try:
        qbt_client = qbittorrentapi.Client(
            host=qbt_config.get("host", "127.0.0.1:8080"),
            username=qbt_config.get("username", "admin"),
            password=qbt_config.get("password", ""),
        )
        qbt_client.auth_log_in()

        save_path = qbt_config.get("save_path", "")
        qbt_client.torrents_add(urls=torrent_url, save_path=save_path)

        return "success", "任务已成功添加到 qBittorrent！"
    except Exception as e:
        return "error", f"推送到下载器失败:\n{e}"
