"""
多站点 Torrent RSS 搜索 + qBittorrent 推送
支持动态配置多个 RSS 订阅源
"""

import feedparser
import requests
import re
import urllib.parse
import logging
import subprocess
import time
import os
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

def _is_download_url(url: str, link_meta: dict = None) -> bool:
    if not url:
        return False
    if url.startswith("magnet:"):
        return True
    parsed = urllib.parse.urlparse(url)
    if parsed.path.lower().endswith(".torrent"):
        return True
    mime_type = (link_meta or {}).get("type", "").lower()
    return "bittorrent" in mime_type


def _download_url_score(url: str) -> int:
    if not url:
        return 0
    if url.startswith("magnet:"):
        return 3
    if urllib.parse.urlparse(url).path.lower().endswith(".torrent"):
        return 2
    return 1


def _extract_torrent_url(entry) -> Optional[str]:
    """从 feed 条目中提取可直接交给下载器的种子/磁力链接。"""
    candidates = []
    for enc in entry.get("enclosures", []):
        candidates.append(enc)
    for link in entry.get("links", []):
        candidates.append(link)

    best_url = None
    best_score = 0
    for item in candidates:
        href = (item.get("href", "") or "").strip()
        if not _is_download_url(href, item):
            continue
        score = _download_url_score(href)
        if score > best_score:
            best_url = href
            best_score = score

    return best_url


def _clean_token(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" []【】()（）")


def _format_size(value) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ""
    if size < 1024 * 1024:
        return ""
    units = ["B", "KB", "MB", "GB"]
    num = float(size)
    for unit in units:
        if num < 1024 or unit == units[-1]:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return ""


def _extract_entry_size(entry) -> str:
    for enc in entry.get("enclosures", []):
        size = _format_size(enc.get("length"))
        if size:
            return size
    for key in ("length", "size", "nyaa_size"):
        size = _format_size(entry.get(key))
        if size:
            return size
    title = entry.get("title", "")
    match = re.search(r"(?i)(\d+(?:\.\d+)?)\s*(GB|GiB|MB|MiB)", title)
    if match:
        return f"{match.group(1)} {match.group(2).upper().replace('IB', 'B')}"
    return ""


def parse_torrent_title(title: str) -> dict:
    """Extract readable resource metadata from common anime torrent titles."""
    text = title or ""
    meta = {
        "group": "",
        "episode": "",
        "episode_range": "",
        "is_batch": False,
        "resolution": "",
        "subtitle": "",
        "codec": "",
        "container": "",
    }

    group = re.match(r"^\s*[\[【]([^\]】]{1,48})[\]】]", text)
    if group:
        meta["group"] = _clean_token(group.group(1))

    range_match = re.search(
        r"(?<!\d)(\d{1,3})\s*(?:-|~|～|至|到)\s*(\d{1,3})(?!\d)", text
    )
    if range_match:
        start, end = range_match.groups()
        meta["episode_range"] = f"{int(start):02d}-{int(end):02d}"
        meta["is_batch"] = True

    batch_match = re.search(
        r"(?i)(合集|全集|全\s*\d{1,3}\s*[话話集]?|batch|complete|complete\s+series)",
        text,
    )
    if batch_match:
        meta["is_batch"] = True

    if not meta["episode_range"]:
        episode_patterns = [
            r"(?i)(?:第|ep(?:isode)?\.?\s*)\s*(\d{1,3})(?:\s*[话話集])?",
            r"(?:^|[\s_\-\[\(【])(\d{1,3})(?:v\d+)?(?:$|[\s_\-\]\)】.])",
        ]
        for pattern in episode_patterns:
            candidates = []
            for match in re.finditer(pattern, text):
                value = int(match.group(1))
                if 0 < value < 200 and value not in (264, 265):
                    candidates.append(value)
            if candidates:
                meta["episode"] = f"{candidates[-1]:02d}"
                break

    resolution = re.search(r"(?i)(2160p|1080p|720p|480p|4k)", text)
    if resolution:
        value = resolution.group(1).lower()
        meta["resolution"] = "2160p" if value == "4k" else value

    if re.search(r"(?i)(简繁|繁简|CHS\s*[&+/]\s*CHT|CHT\s*[&+/]\s*CHS)", text):
        meta["subtitle"] = "简繁"
    elif re.search(r"(?i)(简中|简体|CHS|GB)", text):
        meta["subtitle"] = "简中"
    elif re.search(r"(?i)(繁中|繁体|CHT|BIG5)", text):
        meta["subtitle"] = "繁中"

    codec = re.search(r"(?i)(hevc|h\.?265|x265|avc|h\.?264|x264|av1)", text)
    if codec:
        raw = codec.group(1).lower().replace(".", "")
        if raw in ("hevc", "h265", "x265"):
            meta["codec"] = "HEVC"
        elif raw in ("avc", "h264", "x264"):
            meta["codec"] = "AVC"
        else:
            meta["codec"] = "AV1"

    container = re.search(r"(?i)\b(mkv|mp4)\b", text)
    if container:
        meta["container"] = container.group(1).upper()

    return meta


def build_resource_tags(meta: dict, source: str = "", size: str = "") -> list[str]:
    tags = []
    if meta.get("group"):
        tags.append(meta["group"])
    elif source:
        tags.append(source)

    if meta.get("is_batch"):
        tags.append("合集")
        if meta.get("episode_range"):
            tags.append(meta["episode_range"])
    elif meta.get("episode"):
        tags.append(f"EP {meta['episode']}")
    else:
        tags.append("集数未知")

    for key in ("resolution", "subtitle", "codec", "container"):
        if meta.get(key):
            tags.append(meta[key])
    if size:
        tags.append(size)
    return tags


def _normalize_title_for_dedup(title: str) -> str:
    text = (title or "").lower()
    text = re.sub(r"(?i)\b\d+(?:\.\d+)?\s*(?:gb|gib|mb|mib)\b", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _dedup_signature(result: dict) -> tuple:
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
        _normalize_title_for_dedup(result.get("title") or ""),
    )


def _dedupe_torrent_results(results: list[dict]) -> list[dict]:
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

        signature = _dedup_signature(result)
        if signature in positions:
            existing_index = positions[signature]
            existing = deduped[existing_index]
            existing_score = _download_url_score(existing.get("url") or "")
            result_score = _download_url_score(result.get("url") or "")
            if result_score > existing_score:
                deduped[existing_index] = result
            elif result_score == existing_score and not existing.get("size") and result.get("size"):
                deduped[existing_index] = result
            continue

        positions[signature] = len(deduped)
        deduped.append(result)

    return deduped


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

        url = _extract_torrent_url(entry)
        if not url:
            continue
        meta = parse_torrent_title(title)
        size = _extract_entry_size(entry)
        results.append({
            "title": title,
            "url": url,
            "source": source.name,
            "size": size,
            "meta": meta,
            "resource_tags": build_resource_tags(meta, source.name, size),
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

    all_results = _dedupe_torrent_results(all_results)

    if not all_results:
        return "error", []

    return "success", all_results


def find_qbt_exe(user_path: str = "") -> Optional[str]:
    """查找 qBittorrent 可执行文件路径。"""
    if user_path and os.path.isfile(user_path):
        return user_path

    candidates = [
        os.path.expandvars(r"%PROGRAMFILES%\qBittorrent\qbittorrent.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\qBittorrent\qbittorrent.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\qBittorrent\qbittorrent.exe"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _try_qbt_connect(qbt_config: dict):
    """尝试连接 qBittorrent Web API，成功返回 client，失败返回 None。"""
    import qbittorrentapi
    try:
        client = qbittorrentapi.Client(
            host=qbt_config.get("host", "127.0.0.1:8080"),
            username=qbt_config.get("username", "admin"),
            password=qbt_config.get("password", ""),
        )
        client.auth_log_in()
        return client
    except Exception:
        return None


def push_to_qbittorrent(torrent_url: str, qbt_config: dict,
                        auto_launch: bool = False,
                        qbt_exe_path: str = "") -> tuple[str, str]:
    """
    下载引擎：接收种子/磁力链接，推送到 qBittorrent。
    若 auto_launch 为 True 且连接失败，尝试自动启动 qBittorrent 后重试。
    返回 ("success", msg) 或 ("error", msg)
    """
    import qbittorrentapi

    client = _try_qbt_connect(qbt_config)
    if client is not None:
        try:
            save_path = qbt_config.get("save_path", "")
            client.torrents_add(urls=torrent_url, save_path=save_path)
            return "success", "任务已成功添加到 qBittorrent！"
        except Exception as e:
            return "error", f"推送到下载器失败:\n{e}"

    if not auto_launch:
        return "error", "无法连接到 qBittorrent，请确保它正在运行且已开启 WebUI。\n\n可在设置中开启「自动启动 qBittorrent」。"

    exe_path = find_qbt_exe(qbt_exe_path)
    if not exe_path:
        return "error", (
            "无法连接到 qBittorrent，且未找到 qbittorrent.exe。\n\n"
            "请在设置中手动指定 qBittorrent 的安装路径。"
        )

    try:
        subprocess.Popen(exe_path, creationflags=0x08000000 if os.name == 'nt' else 0)
    except Exception as e:
        return "error", f"启动 qBittorrent 失败:\n{e}"

    for _ in range(20):
        time.sleep(0.5)
        client = _try_qbt_connect(qbt_config)
        if client is not None:
            try:
                save_path = qbt_config.get("save_path", "")
                client.torrents_add(urls=torrent_url, save_path=save_path)
                return "success", "已自动启动 qBittorrent，任务添加成功！"
            except Exception as e:
                return "error", f"qBittorrent 已启动，但推送失败:\n{e}"

    return "error", "qBittorrent 已启动，但 WebUI 在 10 秒内未就绪，请检查 WebUI 设置。"
