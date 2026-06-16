import webview
import ctypes
import sys
import os
import json
import logging
import re
import time
import urllib.parse
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from bottle import Bottle, static_file
import downloader
import local_manager
import database

# ================= 1. 路径与环境核心逻辑 =================

if hasattr(sys, 'frozen'):
    EXE_DIR = os.path.dirname(sys.executable)
    RUNTIME_DIR = sys._MEIPASS
else:
    EXE_DIR = os.path.dirname(os.path.abspath(__file__))
    RUNTIME_DIR = EXE_DIR

# 全局常量定义
CONFIG_FILE = os.path.join(EXE_DIR, "config.json")
FAV_FILE = os.path.join(EXE_DIR, "favorites.json")
CACHE_DIR = os.path.join(EXE_DIR, "cache_covers")
DATA_CACHE_FILE = os.path.join(EXE_DIR, "bgm_cache.json") # 日历数据缓存
SUBJECT_TAGS_CACHE_FILE = os.path.join(EXE_DIR, "subject_tags_cache.json") # 条目标签缓存（迁移用）
DB_PATH = os.path.join(EXE_DIR, "animeasi.db")
WEB_DIR = os.path.join(RUNTIME_DIR, "web")

os.chdir(EXE_DIR)
os.makedirs(CACHE_DIR, exist_ok=True)

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

        # 初始化数据库 & 迁移旧 JSON
        self.db = database.AnimeDB(DB_PATH)
        if self.db.needs_migration():
            self.db.migrate_from_json(
                DATA_CACHE_FILE, SUBJECT_TAGS_CACHE_FILE,
                FAV_FILE, local_manager.WATCH_HISTORY_FILE
            )

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

    def _download_img(self, url, local_path):
        try:
            proxies = None
            if self.config.get("use_proxy") and self.config.get("proxy_address"):
                p = f"http://{self.config['proxy_address']}"
                proxies = {"http": p, "https": p}
            resp = requests.get(url, headers={'User-Agent': 'AnimeAsi/6.6 (github.com/animeasi)'}, proxies=proxies, timeout=10)
            if resp.status_code == 200:
                with open(local_path, 'wb') as f:
                    f.write(resp.content)
        except Exception as e:
            logging.error("_download_img failed: url=%s, error=%s", url, e)

    def _process_image_urls(self, items):
        for item in items:
            imgs = item.get('images')
            if not imgs: continue
            img_url = imgs.get('large') or imgs.get('common')
            if not img_url: continue
            
            parsed_url = urllib.parse.urlparse(img_url)
            filename = os.path.basename(parsed_url.path or img_url)
            if not filename:
                continue
            local_path = os.path.join(self.cache_path, filename)
            
            # 💡 仅当文件存在且大于 20KB 时才认为有效
            if os.path.exists(local_path) and os.path.getsize(local_path) > 20480:
                local_url = f"/covers/{urllib.parse.quote(filename)}"
                item['images']['common'] = local_url
                item['images']['large'] = local_url
            elif parsed_url.scheme in ("http", "https"):
                threading.Thread(target=self._download_img, args=(img_url, local_path), daemon=True).start()

    def get_cache_size(self):
        try:
            total = sum(os.path.getsize(os.path.join(self.cache_path, f)) for f in os.listdir(self.cache_path) if os.path.isfile(os.path.join(self.cache_path, f)))
            return f"{total / (1024 * 1024):.1f} MB"
        except Exception as e:
            logging.error("get_cache_size failed: %s", e)
            return "0.0 MB"

    def clear_cache(self):
        for f in os.listdir(self.cache_path):
            try: os.remove(os.path.join(self.cache_path, f))
            except Exception as e:
                logging.error("clear_cache: failed to remove %s: %s", f, e)
        return {"status": "success"}

    # ─── 条目标签分类（日漫识别） ─────────────────────────

    @staticmethod
    def _classify_by_tags(tags):
        """Bgm.tv 以日漫为主，仅有非日漫才会被打上产地标签。"""
        tag_names = {t.get('name', '') for t in tags}
        non_jp = {"国产", "中国", "中国动画", "欧美", "欧美动画", "韩国", "韩剧", "美国", "法国"}
        return False if (tag_names & non_jp) else True

    @staticmethod
    def _is_season_mainline(tags, platform):
        """Strict seasonal-anime view: Japanese TV series without obvious promo/short noise."""
        if platform != "TV" or not tags:
            return False
        tag_names = {tag.get("name", "") for tag in tags}
        noise_tags = {"短片", "MV", "PV", "CM", "广告", "宣传片", "动态漫画"}
        return "日本" in tag_names and not (tag_names & noise_tags)

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
        rank = row["rank"]
        if rank is None and rating:
            rank = rating.get("rank")
            if rank == 0:
                rank = None
        detail = {
            "id": row["id"], "name": row["name"], "name_cn": row["name_cn"],
            "url": row["url"], "summary": row["summary"] or "",
            "air_date": row["air_date"],
            "rating": rating, "rank": rank,
            "images": {"common": row["image_common"], "large": row["image_large"]},
            "top_tags": self._top_tags_from_cache(row["id"], limit=8),
        }
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
            detail = {
                "id": data["id"], "name": data.get("name"), "name_cn": data.get("name_cn"),
                "url": data.get("url") or f"https://bgm.tv/subject/{subject_id}",
                "summary": data.get("summary") or "",
                "air_date": data.get("date"),
                "rating": data.get("rating"),
                "rank": data.get("rank") or (data.get("rating") or {}).get("rank"),
                "images": data.get("images") or {},
                "top_tags": self._top_tags_from_cache(subject_id, limit=8),
            }
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
            for item in day.get('items', []):
                tags = self.subject_tags_cache.get(item.get('id'))
                if tags is not None:
                    item['is_japanese'] = self._classify_by_tags(tags)
                    item['top_tags'] = self._top_tags_from_cache(item.get('id'))
        return data

    # ─── 赛季浏览 ─────────────────────────────────────

    def _get_proxies(self):
        if self.config.get("use_proxy") and self.config.get("proxy_address"):
            p = f"http://{self.config['proxy_address']}"
            return {"http": p, "https": p}
        return None

    def _fetch_season_month(self, year, month):
        """Fetch one calendar month with exact API pagination."""
        items = []
        offset = 0
        total = None
        while total is None or offset < total:
            params = {
                'type': 2, 'sort': 'date', 'year': year, 'month': month,
                'limit': 100, 'offset': offset,
            }
            payload = self._get_bangumi_json_with_retry(params)
            page = payload.get('data')
            total = payload.get('total')
            if not isinstance(page, list) or not isinstance(total, int):
                raise ValueError(f'Invalid Bangumi response for {year}-{month:02d}')
            items.extend(page)
            if not page:
                if offset < total:
                    raise ValueError(f'Incomplete Bangumi response for {year}-{month:02d}')
                break
            offset += len(page)
        return items

    def _get_bangumi_json_with_retry(self, params, attempts=3):
        """Request Bangumi JSON with short exponential backoff."""
        for attempt in range(1, attempts + 1):
            try:
                response = requests.get(
                    'https://api.bgm.tv/v0/subjects',
                    params=params,
                    headers={'User-Agent': 'AnimeAsi/6.6 (github.com/animeasi)'},
                    proxies=self._get_proxies(),
                    timeout=(10, 30)
                )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as e:
                if attempt >= attempts:
                    raise
                delay = 2 ** (attempt - 1)
                logging.warning(
                    "Bangumi request retry %s/%s: year=%s, month=%s, offset=%s, error=%s",
                    attempt, attempts, params.get('year'), params.get('month'),
                    params.get('offset'), e
                )
                time.sleep(delay)

    def _fetch_season_data(self, year, month):
        """Fetch all three months exactly and cache only a complete result."""
        months = [month, month + 1, month + 2]
        all_items = []
        try:
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {
                    executor.submit(self._fetch_season_month, year, target_month): target_month
                    for target_month in months
                }
                for future in as_completed(futures):
                    all_items.extend(future.result())
        except Exception as e:
            logging.error("_fetch_season_data failed: year=%s, month=%s, error=%s", year, month, e)
            stale = self._get_cached_season_items(year, month)
            if stale:
                return stale
            raise

        unique_items = {item['id']: item for item in all_items if item.get('id')}
        complete_items = sorted(
            unique_items.values(),
            key=lambda item: item.get('date') or '',
            reverse=True
        )
        self.db.save_season_batch(year, month, complete_items)
        uncached = self.db.get_uncached_ids(list(unique_items))
        if uncached:
            if self.config.get("only_show_japanese"):
                self._load_season_tags(uncached)
            else:
                threading.Thread(target=self._preload_season_tags, args=(uncached,), daemon=True).start()

        return self._prepare_season_items(
            [self._item_to_season_dict(item) for item in complete_items]
        )

    def _preload_season_tags(self, ids):
        self._load_season_tags(ids)

    def _load_season_tags(self, ids):
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(self._fetch_single_subject_tags, sid): sid for sid in ids}
            for future in as_completed(futures):
                sid = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        self.db.save_tags(sid, result)
                        self.subject_tags_cache[sid] = result
                except Exception as e:
                    logging.error("_preload_season_tags: id=%s, error=%s", sid, e)

    def _item_to_season_dict(self, item):
        sid = item['id']
        rating = item.get('rating')
        images = item.get('images') or {}
        rank = item.get('rank')
        if rank is None and rating:
            rank = rating.get('rank')
        cached_tags = self.subject_tags_cache.get(sid)
        is_jp = self._classify_by_tags(cached_tags) if cached_tags is not None else None
        is_season_mainline = self._is_season_mainline(cached_tags, item.get('platform'))
        return {
            'id': sid,
            'name': item.get('name'),
            'name_cn': item.get('name_cn'),
            'url': item.get('url') or f'https://bgm.tv/subject/{sid}',
            'summary': item.get('summary') or '',
            'air_date': item.get('date'),
            'rating': rating,
            'rank': rank,
            'collection': item.get('collection'),
            'platform': item.get('platform'),
            'images': {'common': images.get('common'), 'large': images.get('large')},
            'top_tags': self._top_tags_from_cache(sid),
            'is_japanese': is_jp,
            'is_season_mainline': is_season_mainline,
        }

    def get_season_anime(self, year, month):
        """返回某一季的所有番剧列表。首次查询会从 API 拉取并缓存。"""
        import datetime
        year = int(year)
        month = int(month)
        if month not in (1, 4, 7, 10):
            raise ValueError("Season month must be one of 1, 4, 7, 10")
        now = datetime.date.today()
        season_start = datetime.date(year, month, 1)
        current_month = ((now.month - 1) // 3) * 3 + 1
        current_season_start = datetime.date(now.year, current_month, 1)
        max_age_hours = 24 if season_start >= current_season_start else None
        if self.db.has_season_cache(year, month, max_age_hours=max_age_hours):
            return self._get_cached_season_items(year, month)
        return self._fetch_season_data(year, month)

    def _get_cached_season_items(self, year, month):
        ids = self.db.get_season_subject_ids(year, month)
        uncached = self.db.get_uncached_ids(ids)
        if uncached:
            if self.config.get("only_show_japanese"):
                self._load_season_tags(uncached)
            else:
                threading.Thread(target=self._preload_season_tags, args=(uncached,), daemon=True).start()
        items = []
        for sid in ids:
            row = self.db.get_subject(sid)
            if row:
                items.append(self._row_to_season_item(row))
        return self._prepare_season_items(items)

    def _prepare_season_items(self, items):
        items = self._apply_season_japanese_filter(items)
        self._process_image_urls(items)
        return items

    def _apply_season_japanese_filter(self, items):
        if not self.config.get("only_show_japanese"):
            return items
        return [item for item in items if item.get("is_season_mainline") is True]

    def _row_to_season_item(self, row):
        rating = json.loads(row['rating']) if row['rating'] else None
        rank = row['rank']
        if rank is None and rating:
            rank = rating.get('rank')
        collection = json.loads(row['collection']) if row['collection'] else None
        cached_tags = self.subject_tags_cache.get(row['id'])
        if cached_tags is not None:
            is_jp = self._classify_by_tags(cached_tags)
        else:
            is_jp = None
        is_season_mainline = self._is_season_mainline(cached_tags, row['platform'])
        return {
            'id': row['id'],
            'name': row['name'],
            'name_cn': row['name_cn'],
            'url': row['url'],
            'summary': row['summary'] or '',
            'air_date': row['air_date'],
            'rating': rating,
            'rank': rank,
            'collection': collection,
            'platform': row['platform'],
            'images': {'common': row['image_common'], 'large': row['image_large']},
            'top_tags': self._top_tags_from_cache(row['id']),
            'is_japanese': is_jp,
            'is_season_mainline': is_season_mainline,
        }

    def get_favorites(self):
        try:
            favs = self.db.get_favorites()
            for item in favs:
                img = item.get("img", "")
                if img and img.startswith("http"):
                    filename = img.split("/")[-1]
                    local_path = os.path.join(self.cache_path, filename)
                    if os.path.exists(local_path) and os.path.getsize(local_path) > 20480:
                        item["img"] = f"/{filename}"
                    sid = item.get("id")
                    if sid:
                        item["top_tags"] = self._top_tags_from_cache(sid)
            return favs
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
            for r in results:
                tags = self._top_tags_from_cache(r.get('id'))
                if tags:
                    r['top_tags'] = tags
            return {"status": "success", "results": results}
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
