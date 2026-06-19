import webview
import ctypes
import sys
import os
import json
import logging
import re
import urllib.parse
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from bottle import Bottle, static_file
from animeasi import database, local_manager
from animeasi.cache import cover_cache
from animeasi.downloads import downloader
from animeasi.season import browser as season_browser
from animeasi.subjects import schema as subject_schema

# ================= 1. 路径与环境核心逻辑 =================

if hasattr(sys, 'frozen'):
    EXE_DIR = os.path.dirname(sys.executable)
    RUNTIME_DIR = sys._MEIPASS
else:
    EXE_DIR = os.path.dirname(os.path.abspath(__file__))
    RUNTIME_DIR = EXE_DIR

# 全局常量定义
CONFIG_FILE = os.path.join(EXE_DIR, "config.json")
CACHE_DIR = os.path.join(EXE_DIR, "cache_covers")
DB_PATH = os.path.join(EXE_DIR, "animeasi.db")
WEB_DIR = os.path.join(RUNTIME_DIR, "web")

os.chdir(EXE_DIR)

LOG_FILE = os.path.join(EXE_DIR, "error.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

try:
    myappid = 'mycompany.animeasi.v5'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except Exception as e:
    logging.error("SetCurrentProcessExplicitAppUserModelID failed: %s", e)

# ================= 2. 建立本地虚拟服务器 =================
server = Bottle()

@server.route('/')
def serve_index():
    return static_file('index.html', root=WEB_DIR)

@server.route('/covers/<filename:path>')
def serve_cover(filename):
    return static_file(filename, root=CACHE_DIR)

@server.route('/<filepath:path>')
def serve_static(filepath):
    # 优先找 Web 资源，找不到再找缓存图片
    if os.path.exists(os.path.join(WEB_DIR, filepath)):
        return static_file(filepath, root=WEB_DIR)
    return static_file(filepath, root=CACHE_DIR)

# ================= 3. 核心 API 类 =================
class AnimeProAPI:
    def __init__(self):
        self.config_path = CONFIG_FILE
        self.cache_path = CACHE_DIR

        self.config = self.load_config()
        self.cover_cache = self._new_cover_cache()

        self.db = database.AnimeDB(DB_PATH)

        # 内存缓存 — 启动时从 DB 加载
        self.cached_bgm_data = self.db.get_calendar() or []
        self.subject_tags_cache = self.db.get_all_tags_map()

        threading.Thread(target=self._preload_bgm, daemon=True).start()

    def load_config(self):
        default_config = {
            "theme": "dark",
            "only_show_japanese": False,
            "use_proxy": False,
            "proxy_address": "127.0.0.1:7890",
            "local_anime_path": "",
            "qbt_host": "127.0.0.1:8080",
            "qbt_password": "",
            "qbt_auto_launch": True,
            "qbt_exe_path": "",
            "rss_sources": [
                {"name": "蜜柑计划", "url_template": "https://mikanani.me/RSS/Search?searchstr={keyword}", "enabled": True},
                {"name": "Nyaa.si", "url_template": "https://nyaa.si/?page=rss&q={keyword}&c=0_0&f=0", "enabled": True},
                {"name": "动漫花园", "url_template": "https://dmhy.org/topics/rss/rss.xml?keyword={keyword}", "enabled": False},
            ],
        }
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return {**default_config, **json.load(f)}
            except Exception as e:
                logging.error("load_config: failed to read config.json: %s", e)
        
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, ensure_ascii=False, indent=4)
        return default_config

    def save_config(self, new_config):
        self.config.update(new_config)
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
            return {"status": "success"}
        except Exception as e:
            logging.error("save_config failed: %s", e)
            return {"status": "error", "message": str(e)}

    def get_init_config(self): return self.config

    def _new_cover_cache(self):
        return cover_cache.CoverCache(self.cache_path, self._get_proxies)

    def _get_cover_cache(self):
        if not hasattr(self, "cover_cache") or self.cover_cache.cache_dir != self.cache_path:
            self.cover_cache = self._new_cover_cache()
        return self.cover_cache

    def _process_image_urls(self, items):
        self._get_cover_cache().process_items(items)

    def get_cache_size(self):
        return self._get_cover_cache().get_size()

    def clear_cache(self):
        return self._get_cover_cache().clear()

    # ─── 条目标签分类（日漫识别） ─────────────────────────

    @staticmethod
    def _classify_by_tags(tags):
        """Bgm.tv 以日漫为主，仅有非日漫才会被打上产地标签。"""
        tag_names = {t.get('name', '') for t in tags}
        non_jp = {"国产", "中国", "中国动画", "欧美", "欧美动画", "韩国", "韩剧", "美国", "法国"}
        return False if (tag_names & non_jp) else True

    @staticmethod
    def _is_season_mainline(tags, platform):
        return season_browser.is_season_mainline(tags, platform)

    def _fetch_single_subject_tags(self, subject_id):
        try:
            proxies = None
            if self.config.get("use_proxy") and self.config.get("proxy_address"):
                p = f"http://{self.config['proxy_address']}"
                proxies = {"http": p, "https": p}
            resp = requests.get(
                f"https://api.bgm.tv/v0/subjects/{subject_id}",
                headers={'User-Agent': 'AnimeAsi/6.6 (github.com/animeasi)'},
                proxies=proxies, timeout=10
            )
            data = resp.json()
            return data.get('tags', [])
        except Exception as e:
            logging.error("_fetch_single_subject_tags: id=%s, error=%s", subject_id, e)
            return None

    def _fetch_and_save_subject(self, subject_id):
        """后台拉取完整 subject 数据并存入 DB（收藏时触发）。"""
        try:
            proxies = None
            if self.config.get("use_proxy") and self.config.get("proxy_address"):
                p = f"http://{self.config['proxy_address']}"
                proxies = {"http": p, "https": p}
            resp = requests.get(
                f"https://api.bgm.tv/v0/subjects/{subject_id}",
                headers={'User-Agent': 'AnimeAsi/6.6 (github.com/animeasi)'},
                proxies=proxies, timeout=10
            )
            data = resp.json()
            self.db.save_subject_full(data)
            tags = data.get('tags', [])
            if tags:
                self.subject_tags_cache[subject_id] = tags
        except Exception as e:
            logging.error("_fetch_and_save_subject: id=%s, error=%s", subject_id, e)

    def _preload_bgm(self):
        url = "https://api.bgm.tv/calendar"
        proxies = None
        if self.config.get("use_proxy") and self.config.get("proxy_address"):
            p = f"http://{self.config['proxy_address']}"
            proxies = {"http": p, "https": p}

        try:
            resp = requests.get(url, headers={'User-Agent': 'AnimeAsi/6.6 (github.com/animeasi)'}, proxies=proxies, timeout=10)
            data = resp.json()

            all_items = []
            for day in data: all_items.extend(day.get('items', []))
            self._process_image_urls(all_items)

            self.cached_bgm_data = data
            self.db.save_calendar(data)
        except Exception as e:
            logging.error("_preload_bgm: failed to fetch bgm data: %s", e)
            pass

        # 拉取所有未缓存的条目标签
        if self.cached_bgm_data:
            all_ids = set()
            for day in self.cached_bgm_data:
                for item in day.get('items', []):
                    if item.get('id'):
                        all_ids.add(item['id'])

            uncached_ids = self.db.get_uncached_ids(list(all_ids))
            if uncached_ids:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = {executor.submit(self._fetch_single_subject_tags, sid): sid for sid in uncached_ids}
                    for future in as_completed(futures):
                        sid = futures[future]
                        try:
                            result = future.result()
                            if result is not None:
                                self.db.save_tags(sid, result)
                                self.subject_tags_cache[sid] = result
                        except Exception as e:
                            logging.error("_preload_bgm: tag fetch failed for id=%s: %s", sid, e)

    def _top_tags_from_cache(self, subject_id, limit=3):
        """从内存缓存获取标签，最多 1 个日期标签（取最具体），其余按 count 降序。"""
        tags = self.subject_tags_cache.get(subject_id)
        if not tags:
            return None
        def _is_date(t):
            name = t.get('name', '')
            if re.match(r'\d{4}\s*-\s*\d{4}', name):  # 年代范围 "2020-2029"
                return False
            if '年代' in name:  # "2020年代"
                return False
            return bool(re.match(r'^\d{4}', name)) or '月' in name or '年' in name
        date_tags = [t for t in tags if _is_date(t)]
        other_tags = [t for t in tags if not _is_date(t)]
        date_tags.sort(key=lambda t: -len(t.get('name', '')))
        other_tags.sort(key=lambda t: -t.get('count', 0))
        result = date_tags[:1] + other_tags
        return [t['name'] for t in result[:limit]]

    def _row_to_detail(self, row):
        rating = json.loads(row["rating"]) if row["rating"] else None
        collection = json.loads(row["collection"]) if row["collection"] else None
        detail = subject_schema.subject_from_row(
            row,
            rating=rating,
            collection=collection,
            top_tags=self._top_tags_from_cache(row["id"], limit=8),
        )
        self._process_image_urls([detail])
        return detail

    def get_subject_detail(self, subject_id):
        """返回番剧完整详情，DB 有简介时直接返回，否则从 API 拉取并存储。"""
        row = self.db.get_subject(subject_id)
        if row and row["summary"]:
            return self._row_to_detail(row)

        try:
            proxies = None
            if self.config.get("use_proxy") and self.config.get("proxy_address"):
                p = f"http://{self.config['proxy_address']}"
                proxies = {"http": p, "https": p}
            resp = requests.get(
                f"https://api.bgm.tv/v0/subjects/{subject_id}",
                headers={'User-Agent': 'AnimeAsi/6.6 (github.com/animeasi)'},
                proxies=proxies, timeout=10
            )
            data = resp.json()
            self.db.save_subject_full(data)
            tags = data.get("tags", [])
            if tags:
                self.subject_tags_cache[subject_id] = tags
            detail = subject_schema.normalize_subject(
                data,
                top_tags=self._top_tags_from_cache(subject_id, limit=8),
            )
            self._process_image_urls([detail])
            return detail
        except Exception as e:
            logging.error("get_subject_detail: id=%s, error=%s", subject_id, e)
            if row:
                return self._row_to_detail(row)
            return {"id": subject_id, "error": str(e)}

    def get_bgm_data(self):
        data = self.cached_bgm_data
        if not data:
            return data
        all_items = []
        for day in data:
            all_items.extend(day.get('items', []))
        self._process_image_urls(all_items)
        for day in data:
            normalized_items = []
            for item in day.get('items', []):
                tags = self.subject_tags_cache.get(item.get('id'))
                is_jp = item.get("is_japanese")
                if tags is not None:
                    is_jp = self._classify_by_tags(tags)
                normalized_items.append(subject_schema.normalize_subject(
                    item,
                    top_tags=self._top_tags_from_cache(item.get('id')),
                    is_japanese=is_jp,
                ))
            day["items"] = normalized_items
        return data

    # ─── 赛季浏览 ─────────────────────────────────────

    def _get_proxies(self):
        if self.config.get("use_proxy") and self.config.get("proxy_address"):
            p = f"http://{self.config['proxy_address']}"
            return {"http": p, "https": p}
        return None

    def _fetch_season_month(self, year, month):
        """Fetch one calendar month with exact API pagination."""
        return season_browser.fetch_season_month(self, year, month)

    def _get_bangumi_json_with_retry(self, params, attempts=3):
        """Request Bangumi JSON with short exponential backoff."""
        return season_browser.get_bangumi_json_with_retry(self, params, attempts)

    def _fetch_season_data(self, year, month):
        """Fetch all three months exactly and cache only a complete result."""
        return season_browser.fetch_season_data(self, year, month)

    def _preload_season_tags(self, ids):
        return season_browser.preload_season_tags(self, ids)

    def _load_season_tags(self, ids):
        return season_browser.load_season_tags(self, ids)

    def _item_to_season_dict(self, item):
        return season_browser.item_to_season_dict(self, item)

    def get_season_anime(self, year, month):
        """返回某一季的所有番剧列表。首次查询会从 API 拉取并缓存。"""
        return season_browser.get_season_anime(self, year, month)

    def _get_cached_season_items(self, year, month):
        return season_browser.get_cached_season_items(self, year, month)

    def _prepare_season_items(self, items):
        return season_browser.prepare_season_items(self, items)

    def _apply_season_japanese_filter(self, items):
        return season_browser.apply_season_japanese_filter(self, items)

    def _row_to_season_item(self, row):
        return season_browser.row_to_season_item(self, row)

    def get_favorites(self):
        try:
            favs = self.db.get_favorites()
            normalized = []
            for item in favs:
                img = item.get("img", "")
                if img and img.startswith("http"):
                    local_url = self._get_cover_cache().cached_url_for_image(img)
                    if local_url:
                        item["img"] = local_url
                sid = item.get("id")
                top_tags = None
                if sid:
                    top_tags = self._top_tags_from_cache(sid)
                normalized.append(subject_schema.normalize_subject(
                    item,
                    top_tags=top_tags,
                    include_legacy_img=True,
                ))
            return normalized
        except Exception as e:
            logging.error("get_favorites: %s", e)
            return []

    def toggle_favorite(self, anime_data):
        try:
            is_add = self.db.toggle_favorite(anime_data)
            if is_add and anime_data.get("id", 0):
                sid = anime_data["id"]
                if not self.db.has_subject(sid):
                    threading.Thread(
                        target=self._fetch_and_save_subject,
                        args=(sid,),
                        daemon=True
                    ).start()
            return {"status": "success", "is_favorite": is_add}
        except Exception as e:
            logging.error("toggle_favorite: %s", e)
            return {"status": "error", "message": str(e)}

    def select_folder(self):
        window = webview.active_window()
        if not window:
            return ""
        result = window.create_file_dialog(webview.FileDialog.FOLDER)
        return result[0] if result else ""

    def select_file(self):
        window = webview.active_window()
        if not window:
            return ""
        result = window.create_file_dialog(
            webview.FileDialog.OPEN,
            file_types=("可执行文件 (*.exe)", "所有文件 (*.*)")
        )
        return result[0] if result else ""

    def search_anime(self, keyword):
        url = f"https://api.bgm.tv/search/subject/{urllib.parse.quote(keyword)}?type=2&responseGroup=large"
        proxies = None
        if self.config.get("use_proxy") and self.config.get("proxy_address"):
            p = f"http://{self.config['proxy_address']}"
            proxies = {"http": p, "https": p}
        try:
            resp = requests.get(url, headers={'User-Agent': 'AnimeAsi/6.6 (github.com/animeasi)'}, proxies=proxies, timeout=10)
            results = resp.json().get('list', [])
            self._process_image_urls(results)
            normalized = []
            for r in results:
                normalized.append(subject_schema.normalize_subject(
                    r,
                    top_tags=self._top_tags_from_cache(r.get('id')),
                ))
            return {"status": "success", "results": normalized}
        except Exception as e:
            logging.error("search_anime failed: keyword=%s, error=%s", keyword, e)
            return {"status": "error", "results": []}

    def search_torrents(self, kw):
        proxies = None
        if self.config.get("use_proxy") and self.config.get("proxy_address"):
            p = f"http://{self.config['proxy_address']}"
            proxies = {"http": p, "https": p}
        s, r = downloader.search_torrents(kw, self.config.get("rss_sources", []), proxies)
        return {"status": s, "results": r}

    def push_download(self, url, name, path):
        conf = {
            "host": self.config.get("qbt_host"),
            "password": self.config.get("qbt_password"),
            "save_path": path,
        }
        s, m = downloader.push_to_qbittorrent(
            url, conf,
            auto_launch=self.config.get("qbt_auto_launch", True),
            qbt_exe_path=self.config.get("qbt_exe_path", ""),
        )
        return {"status": s, "message": m}

    # ─── RSS 订阅源管理 ────────────────────────────────

    def get_rss_sources(self):
        return self.config.get("rss_sources", [])

    def get_rss_presets(self):
        return downloader.PRESET_SOURCES

    def save_rss_sources(self, sources):
        self.config["rss_sources"] = sources
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
            return {"status": "success"}
        except Exception as e:
            logging.error("save_rss_sources failed: %s", e)
            return {"status": "error", "message": str(e)}

    # ─── 本地动画管理 ────────────────────────────────

    def open_local_folder(self):
        path = self.config.get("local_anime_path", "")
        if path and os.path.isdir(path):
            os.startfile(path)
            return {"status": "success"}
        return {"status": "error", "message": "路径不存在或未配置"}

    def get_anime_episodes(self, anime_name):
        return local_manager.get_anime_episodes(anime_name, self.config.get("local_anime_path", ""), self.db)

    def play_episode(self, anime_name, episode, file_path):
        return local_manager.play_episode(
            anime_name, episode, file_path,
            self.config.get("local_anime_path", ""), self.db
        )

    def get_watch_history(self):
        return local_manager.get_watch_history(self.db)

# ================= 4. 启动容器 =================
if __name__ == '__main__':
    api = AnimeProAPI()
    
    window = webview.create_window(
        'AnimeAsi',
        server,
        js_api=api, 
        width=1100, 
        height=800, 
        background_color='#0d0f1a'
    )
    
    logo_img = os.path.join(RUNTIME_DIR, "logo.ico") if hasattr(sys, '_MEIPASS') else "logo.ico"
    
    webview.start(icon=logo_img, private_mode=True, debug=False)
