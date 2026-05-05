import webview
import ctypes
import sys
import os
import json
import urllib.parse
import requests
import threading
from tkinter import filedialog, Tk
from bottle import Bottle, static_file
import downloader 

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
DATA_CACHE_FILE = os.path.join(EXE_DIR, "bgm_cache.json") # 💡 新增：日历数据缓存
WEB_DIR = os.path.join(RUNTIME_DIR, "web")

os.chdir(EXE_DIR)
os.makedirs(CACHE_DIR, exist_ok=True)

try:
    myappid = 'mycompany.animeasi.v5'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
except:
    pass

# ================= 2. 建立本地虚拟服务器 =================
server = Bottle()

@server.route('/')
def serve_index():
    return static_file('index.html', root=WEB_DIR)

@server.route('/<filepath:path>')
def serve_static(filepath):
    # 优先找 Web 资源，找不到再找缓存图片
    if os.path.exists(os.path.join(WEB_DIR, filepath)):
        return static_file(filepath, root=WEB_DIR)
    return static_file(filepath, root=CACHE_DIR)

# ================= 3. 核心 API 类 =================
class AnimeProAPI:
    def __init__(self):
        # 💡 将全局路径绑定到 self，防止作用域报错
        self.config_path = CONFIG_FILE
        self.fav_path = FAV_FILE
        self.cache_path = CACHE_DIR
        self.data_cache_path = DATA_CACHE_FILE
        
        # 💡 自动创建缺失的配置文件
        self._ensure_files_exist()
        
        self.config = self.load_config()
        self.cached_bgm_data = self._load_local_data_cache() # 💡 优先加载本地缓存数据
        
        threading.Thread(target=self._preload_bgm, daemon=True).start()

    def _ensure_files_exist(self):
        """确保所有持久化文件在启动时即存在"""
        for file_path, default_content in [
            (self.fav_path, []),
            (self.data_cache_path, [])
        ]:
            if not os.path.exists(file_path):
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(default_content, f, ensure_ascii=False, indent=4)

    def load_config(self):
        default_config = {
            "theme": "dark",
            "only_show_japanese": False,
            "use_proxy": False,
            "proxy_address": "127.0.0.1:7890",
            "local_anime_path": "E:\\ANIME",
            "qbt_host": "127.0.0.1:8080",
            "qbt_password": "adminadmin",
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
            except: pass
        
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, ensure_ascii=False, indent=4)
        return default_config

    def save_config(self, new_config):
        self.config.update(new_config)
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, ensure_ascii=False, indent=4)
        return True

    def get_init_config(self): return self.config

    def _download_img(self, url, local_path):
        try:
            resp = requests.get(url, headers={'User-Agent': 'AnimeAsi/5.0'}, timeout=10)
            if resp.status_code == 200:
                with open(local_path, 'wb') as f:
                    f.write(resp.content)
        except: pass

    def _process_image_urls(self, items):
        for item in items:
            imgs = item.get('images')
            if not imgs: continue
            img_url = imgs.get('large') or imgs.get('common')
            if not img_url: continue
            
            filename = img_url.split('/')[-1]
            local_path = os.path.join(self.cache_path, filename)
            
            # 💡 仅当文件存在且大于 20KB 时才认为有效
            if os.path.exists(local_path) and os.path.getsize(local_path) > 20480:
                item['images']['common'] = f"/{filename}"
                item['images']['large'] = f"/{filename}"
            else:
                threading.Thread(target=self._download_img, args=(img_url, local_path), daemon=True).start()

    def get_cache_size(self):
        total = sum(os.path.getsize(os.path.join(self.cache_path, f)) for f in os.listdir(self.cache_path) if os.path.isfile(os.path.join(self.cache_path, f)))
        return f"{total / (1024 * 1024):.1f} MB"

    def clear_cache(self):
        for f in os.listdir(self.cache_path):
            try: os.remove(os.path.join(self.cache_path, f))
            except: pass
        return {"status": "success"}

    def _load_local_data_cache(self):
        """从硬盘读取上次缓存的日历数据"""
        if os.path.exists(self.data_cache_path):
            try:
                with open(self.data_cache_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except: pass
        return []

    def _preload_bgm(self):
        url = "https://api.bgm.tv/calendar"
        # 💡 加固代理逻辑
        proxies = None
        if self.config.get("use_proxy") and self.config.get("proxy_address"):
            p = f"http://{self.config['proxy_address']}"
            proxies = {"http": p, "https": p}
            
        try:
            resp = requests.get(url, headers={'User-Agent': 'AnimeAsi/5.0'}, proxies=proxies, timeout=10)
            data = resp.json()
            
            # 💡 预处理图片
            all_items = []
            for day in data: all_items.extend(day.get('items', []))
            self._process_image_urls(all_items)
            
            # 💡 存入缓存文件实现“秒开”
            self.cached_bgm_data = data
            with open(self.data_cache_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except:
            # 失败则保持现有的缓存数据
            pass

    def get_bgm_data(self):
        return self.cached_bgm_data

    def get_favorites(self):
        if os.path.exists(self.fav_path):
            with open(self.fav_path, 'r', encoding='utf-8') as f: 
                try: return json.load(f)
                except: return []
        return []

    def toggle_favorite(self, anime_data):
        favs = self.get_favorites()
        name = anime_data.get('name')
        new_favs = [f for f in favs if f.get('name') != name]
        is_add = len(new_favs) == len(favs)
        if is_add: new_favs.insert(0, anime_data)
        
        with open(self.fav_path, 'w', encoding='utf-8') as f:
            json.dump(new_favs, f, ensure_ascii=False, indent=4)
        return {"status": "success", "is_favorite": is_add}

    def select_folder(self):
        root = Tk(); root.withdraw(); path = filedialog.askdirectory(); root.destroy()
        return path

    def search_anime(self, keyword):
        url = f"https://api.bgm.tv/search/subject/{urllib.parse.quote(keyword)}?type=2&responseGroup=large"
        proxies = None
        if self.config.get("use_proxy") and self.config.get("proxy_address"):
            p = f"http://{self.config['proxy_address']}"
            proxies = {"http": p, "https": p}
        try:
            resp = requests.get(url, headers={'User-Agent': 'AnimeAsi/5.0'}, proxies=proxies, timeout=10)
            results = resp.json().get('list', [])
            self._process_image_urls(results)
            return {"status": "success", "results": results}
        except: return {"status": "error", "results": []}

    def search_torrents(self, kw):
        s, r = downloader.search_torrents(kw, self.config.get("rss_sources", []))
        return {"status": s, "results": r}

    def push_download(self, url, name, path):
        conf = {"host": self.config.get("qbt_host"), "password": self.config.get("qbt_password"), "save_path": path}
        s, m = downloader.push_to_qbittorrent(url, conf)
        return {"status": s, "message": m}

    # ─── RSS 订阅源管理 ────────────────────────────────

    def get_rss_sources(self):
        return self.config.get("rss_sources", [])

    def get_rss_presets(self):
        return downloader.PRESET_SOURCES

    def save_rss_sources(self, sources):
        self.config["rss_sources"] = sources
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, ensure_ascii=False, indent=4)
        return {"status": "success"}

# ================= 4. 启动容器 =================
if __name__ == '__main__':
    api = AnimeProAPI()
    
    window = webview.create_window(
        'AnimeAsi v5.0', 
        server, 
        js_api=api, 
        width=1100, 
        height=800, 
        background_color='#0d0f1a'
    )
    
    logo_img = os.path.join(RUNTIME_DIR, "logo.ico") if hasattr(sys, '_MEIPASS') else "logo.ico"
    
    webview.start(icon=logo_img, private_mode=True, debug=False)