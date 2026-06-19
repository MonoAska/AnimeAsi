import datetime
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from animeasi.subjects import schema as subject_schema


USER_AGENT = "AnimeAsi/6.6 (github.com/animeasi)"


def is_season_mainline(tags, platform):
    """Strict seasonal-anime view: Japanese TV series without obvious promo/short noise."""
    if platform != "TV" or not tags:
        return False
    tag_names = {tag.get("name", "") for tag in tags}
    noise_tags = {"短片", "MV", "PV", "CM", "广告", "宣传片", "动态漫画"}
    return "日本" in tag_names and not (tag_names & noise_tags)


def fetch_season_month(api, year, month):
    """Fetch one calendar month with exact API pagination."""
    items = []
    offset = 0
    total = None
    while total is None or offset < total:
        params = {
            "type": 2,
            "sort": "date",
            "year": year,
            "month": month,
            "limit": 100,
            "offset": offset,
        }
        payload = get_bangumi_json_with_retry(api, params)
        page = payload.get("data")
        total = payload.get("total")
        if not isinstance(page, list) or not isinstance(total, int):
            raise ValueError(f"Invalid Bangumi response for {year}-{month:02d}")
        items.extend(page)
        if not page:
            if offset < total:
                raise ValueError(f"Incomplete Bangumi response for {year}-{month:02d}")
            break
        offset += len(page)
    return items


def get_bangumi_json_with_retry(api, params, attempts=3):
    """Request Bangumi JSON with short exponential backoff."""
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                "https://api.bgm.tv/v0/subjects",
                params=params,
                headers={"User-Agent": USER_AGENT},
                proxies=api._get_proxies(),
                timeout=(10, 30),
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as e:
            if attempt >= attempts:
                raise
            delay = 2 ** (attempt - 1)
            logging.warning(
                "Bangumi request retry %s/%s: year=%s, month=%s, offset=%s, error=%s",
                attempt,
                attempts,
                params.get("year"),
                params.get("month"),
                params.get("offset"),
                e,
            )
            time.sleep(delay)


def fetch_season_data(api, year, month):
    """Fetch all three months exactly and cache only a complete result."""
    months = [month, month + 1, month + 2]
    all_items = []
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(api._fetch_season_month, year, target_month): target_month
                for target_month in months
            }
            for future in as_completed(futures):
                all_items.extend(future.result())
    except Exception as e:
        logging.error("_fetch_season_data failed: year=%s, month=%s, error=%s", year, month, e)
        stale = api._get_cached_season_items(year, month)
        if stale:
            return stale
        raise

    unique_items = {item["id"]: item for item in all_items if item.get("id")}
    complete_items = sorted(
        unique_items.values(),
        key=lambda item: item.get("date") or "",
        reverse=True,
    )
    api.db.save_season_batch(year, month, complete_items)
    uncached = api.db.get_uncached_ids(list(unique_items))
    if uncached:
        if api.config.get("only_show_japanese"):
            api._load_season_tags(uncached)
        else:
            threading.Thread(target=api._preload_season_tags, args=(uncached,), daemon=True).start()

    return api._prepare_season_items(
        [api._item_to_season_dict(item) for item in complete_items]
    )


def preload_season_tags(api, ids):
    api._load_season_tags(ids)


def load_season_tags(api, ids):
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(api._fetch_single_subject_tags, sid): sid for sid in ids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                result = future.result()
                if result is not None:
                    api.db.save_tags(sid, result)
                    api.subject_tags_cache[sid] = result
            except Exception as e:
                logging.error("_preload_season_tags: id=%s, error=%s", sid, e)


def item_to_season_dict(api, item):
    sid = item["id"]
    cached_tags = api.subject_tags_cache.get(sid)
    is_jp = api._classify_by_tags(cached_tags) if cached_tags is not None else None
    is_mainline = api._is_season_mainline(cached_tags, item.get("platform"))
    return subject_schema.normalize_subject(
        item,
        top_tags=api._top_tags_from_cache(sid),
        is_japanese=is_jp,
        is_season_mainline=is_mainline,
    )


def get_season_anime(api, year, month):
    year = int(year)
    month = int(month)
    if month not in (1, 4, 7, 10):
        raise ValueError("Season month must be one of 1, 4, 7, 10")
    now = datetime.date.today()
    season_start = datetime.date(year, month, 1)
    current_month = ((now.month - 1) // 3) * 3 + 1
    current_season_start = datetime.date(now.year, current_month, 1)
    max_age_hours = 24 if season_start >= current_season_start else None
    if api.db.has_season_cache(year, month, max_age_hours=max_age_hours):
        return api._get_cached_season_items(year, month)
    return api._fetch_season_data(year, month)


def get_cached_season_items(api, year, month):
    ids = api.db.get_season_subject_ids(year, month)
    uncached = api.db.get_uncached_ids(ids)
    if uncached:
        if api.config.get("only_show_japanese"):
            api._load_season_tags(uncached)
        else:
            threading.Thread(target=api._preload_season_tags, args=(uncached,), daemon=True).start()
    items = []
    for sid in ids:
        row = api.db.get_subject(sid)
        if row:
            items.append(api._row_to_season_item(row))
    return api._prepare_season_items(items)


def prepare_season_items(api, items):
    items = api._apply_season_japanese_filter(items)
    api._process_image_urls(items)
    return items


def apply_season_japanese_filter(api, items):
    if not api.config.get("only_show_japanese"):
        return items
    return [item for item in items if item.get("is_season_mainline") is True]


def row_to_season_item(api, row):
    rating = json.loads(row["rating"]) if row["rating"] else None
    collection = json.loads(row["collection"]) if row["collection"] else None
    cached_tags = api.subject_tags_cache.get(row["id"])
    if cached_tags is not None:
        is_jp = api._classify_by_tags(cached_tags)
    else:
        is_jp = None
    is_mainline = api._is_season_mainline(cached_tags, row["platform"])
    return subject_schema.subject_from_row(
        row,
        rating=rating,
        collection=collection,
        top_tags=api._top_tags_from_cache(row["id"]),
        is_japanese=is_jp,
        is_season_mainline=is_mainline,
    )
