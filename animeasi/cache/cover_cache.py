import logging
import os
import threading
import urllib.parse

import requests


MIN_VALID_COVER_SIZE = 20 * 1024
USER_AGENT = "AnimeAsi/6.6 (github.com/animeasi)"


def filename_from_url(image_url: str) -> str:
    parsed_url = urllib.parse.urlparse(image_url or "")
    return os.path.basename(parsed_url.path or image_url or "")


class CoverCache:
    def __init__(self, cache_dir: str, get_proxies=None):
        self.cache_dir = cache_dir
        self.get_proxies = get_proxies or (lambda: None)
        os.makedirs(self.cache_dir, exist_ok=True)

    def local_path(self, filename: str) -> str:
        return os.path.join(self.cache_dir, filename)

    def local_url(self, filename: str) -> str:
        return f"/covers/{urllib.parse.quote(filename)}"

    def is_valid_cached_file(self, filename: str) -> bool:
        if not filename:
            return False
        path = self.local_path(filename)
        return os.path.exists(path) and os.path.getsize(path) > MIN_VALID_COVER_SIZE

    def cached_url_for_image(self, image_url: str) -> str:
        filename = filename_from_url(image_url)
        if self.is_valid_cached_file(filename):
            return self.local_url(filename)
        return ""

    def _download_image(self, url: str, local_path: str):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                proxies=self.get_proxies(),
                timeout=10,
            )
            if resp.status_code == 200:
                with open(local_path, "wb") as f:
                    f.write(resp.content)
        except Exception as e:
            logging.error("_download_image failed: url=%s, error=%s", url, e)

    def process_items(self, items):
        for item in items:
            imgs = item.get("images")
            if not imgs:
                continue

            img_url = imgs.get("large") or imgs.get("common")
            if not img_url:
                continue

            filename = filename_from_url(img_url)
            if not filename:
                continue

            if self.is_valid_cached_file(filename):
                local_url = self.local_url(filename)
                item["images"]["common"] = local_url
                item["images"]["large"] = local_url
                continue

            parsed_url = urllib.parse.urlparse(img_url)
            if parsed_url.scheme in ("http", "https"):
                threading.Thread(
                    target=self._download_image,
                    args=(img_url, self.local_path(filename)),
                    daemon=True,
                ).start()

    def get_size(self) -> str:
        try:
            total = 0
            for filename in os.listdir(self.cache_dir):
                path = self.local_path(filename)
                if os.path.isfile(path):
                    total += os.path.getsize(path)
            return f"{total / (1024 * 1024):.1f} MB"
        except Exception as e:
            logging.error("get_cache_size failed: %s", e)
            return "0.0 MB"

    def clear(self):
        for filename in os.listdir(self.cache_dir):
            path = self.local_path(filename)
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception as e:
                logging.error("clear_cache: failed to remove %s: %s", filename, e)
        return {"status": "success"}
