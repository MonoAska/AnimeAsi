"""
SQLite 数据库模块 — 统一管理日历 / 标签 / 收藏 / 观看记录
"""

import sqlite3
import json
import logging
import threading
from functools import wraps


def synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            try:
                return method(self, *args, **kwargs)
            except Exception:
                if hasattr(self, "conn"):
                    self.conn.rollback()
                raise
    return wrapper

WEEKDAYS = [
    {"id": 1, "en": "Mon", "cn": "周一", "jp": "月曜日"},
    {"id": 2, "en": "Tue", "cn": "周二", "jp": "火曜日"},
    {"id": 3, "en": "Wed", "cn": "周三", "jp": "水曜日"},
    {"id": 4, "en": "Thu", "cn": "周四", "jp": "木曜日"},
    {"id": 5, "en": "Fri", "cn": "周五", "jp": "金曜日"},
    {"id": 6, "en": "Sat", "cn": "周六", "jp": "土曜日"},
    {"id": 7, "en": "Sun", "cn": "周日", "jp": "日曜日"},
]


class AnimeDB:
    def __init__(self, db_path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    @synchronized
    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS subjects (
                id INTEGER PRIMARY KEY,
                name TEXT,
                name_cn TEXT,
                url TEXT,
                air_date TEXT,
                air_weekday INTEGER,
                rating TEXT,
                rank INTEGER,
                summary TEXT,
                image_common TEXT,
                image_large TEXT,
                collection TEXT,
                platform TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS subject_tags (
                subject_id INTEGER,
                tag_name TEXT,
                tag_count INTEGER DEFAULT 0,
                PRIMARY KEY (subject_id, tag_name)
            );
            CREATE TABLE IF NOT EXISTS calendar (
                subject_id INTEGER PRIMARY KEY,
                weekday INTEGER
            );
            CREATE TABLE IF NOT EXISTS favorites (
                subject_id INTEGER,
                name TEXT,
                img TEXT,
                url TEXT,
                added_at TEXT DEFAULT (datetime('now')),
                UNIQUE(subject_id, name)
            );
            CREATE TABLE IF NOT EXISTS watch_history (
                anime_name TEXT,
                episode INTEGER,
                watched_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (anime_name, episode)
            );
            CREATE INDEX IF NOT EXISTS idx_tags_name ON subject_tags(tag_name);
            CREATE INDEX IF NOT EXISTS idx_calendar_wd ON calendar(weekday);
            CREATE TABLE IF NOT EXISTS season_subjects (
                year INTEGER,
                month INTEGER,
                subject_id INTEGER,
                sort_order INTEGER DEFAULT 0,
                PRIMARY KEY (year, month, subject_id)
            );
            CREATE TABLE IF NOT EXISTS season_cache (
                year INTEGER,
                month INTEGER,
                status TEXT NOT NULL,
                item_count INTEGER DEFAULT 0,
                fetched_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (year, month)
            );
        """)
        season_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(season_subjects)")
        }
        if "sort_order" not in season_columns:
            self.conn.execute(
                "ALTER TABLE season_subjects ADD COLUMN sort_order INTEGER DEFAULT 0"
            )
        subject_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(subjects)")
        }
        if "platform" not in subject_columns:
            self.conn.execute("ALTER TABLE subjects ADD COLUMN platform TEXT")
            self.conn.execute("DELETE FROM season_cache")
        self.conn.commit()

    # ─── Calendar ───────────────────────────────────────

    @synchronized
    def save_calendar(self, data):
        """data: Bangumi calendar API response"""
        c = self.conn
        c.execute("DELETE FROM calendar")
        for day in data:
            wd = day["weekday"]["id"]
            for item in day.get("items", []):
                sid = item["id"]
                rating = item.get("rating")
                collection = item.get("collection")
                images = item.get("images") or {}
                existing = c.execute(
                    "SELECT platform FROM subjects WHERE id = ?", (sid,)
                ).fetchone()
                platform = item.get("platform") or (existing["platform"] if existing else None)
                c.execute("""
                    INSERT OR REPLACE INTO subjects
                    (id, name, name_cn, url, air_date, air_weekday,
                     rating, rank, summary, image_common, image_large, collection,
                     platform, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (
                    sid,
                    item.get("name"),
                    item.get("name_cn"),
                    item.get("url"),
                    item.get("air_date"),
                    item.get("air_weekday"),
                    json.dumps(rating, ensure_ascii=False) if rating else None,
                    item.get("rank"),
                    item.get("summary"),
                    images.get("common"),
                    images.get("large"),
                    json.dumps(collection, ensure_ascii=False) if collection else None,
                    platform,
                ))
                c.execute("INSERT OR REPLACE INTO calendar(subject_id, weekday) VALUES (?, ?)", (sid, wd))
        c.commit()

    @synchronized
    def get_calendar(self):
        c = self.conn
        result = []
        for wd in WEEKDAYS:
            rows = c.execute("""
                SELECT s.* FROM subjects s
                JOIN calendar cal ON s.id = cal.subject_id
                WHERE cal.weekday = ?
            """, (wd["id"],)).fetchall()
            items = []
            for row in rows:
                item = {
                    "id": row["id"],
                    "url": row["url"],
                    "type": 2,
                    "name": row["name"],
                    "name_cn": row["name_cn"],
                    "summary": row["summary"] or "",
                    "air_date": row["air_date"],
                    "air_weekday": row["air_weekday"],
                    "rating": json.loads(row["rating"]) if row["rating"] else None,
                    "rank": row["rank"],
                    "images": {"common": row["image_common"], "large": row["image_large"]},
                    "collection": json.loads(row["collection"]) if row["collection"] else None,
                    "platform": row["platform"],
                }
                items.append(item)
            result.append({"weekday": wd, "items": items})
        return result

    # ─── Subject tags ───────────────────────────────────

    @synchronized
    def get_uncached_ids(self, ids):
        """返回尚未缓存标签的 subject_id 列表"""
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT subject_id FROM subject_tags WHERE subject_id IN ({placeholders})",
            ids
        ).fetchall()
        cached = {row[0] for row in rows}
        return [i for i in ids if i not in cached]

    @synchronized
    def save_tags(self, subject_id, tags):
        c = self.conn
        c.execute("DELETE FROM subject_tags WHERE subject_id = ?", (subject_id,))
        c.executemany(
            "INSERT INTO subject_tags (subject_id, tag_name, tag_count) VALUES (?, ?, ?)",
            [(subject_id, t["name"], t.get("count", 0)) for t in tags]
        )
        c.commit()

    @synchronized
    def get_tags(self, subject_id):
        rows = self.conn.execute(
            "SELECT tag_name, tag_count FROM subject_tags WHERE subject_id = ?",
            (subject_id,)
        ).fetchall()
        if not rows:
            return None
        return [{"name": r["tag_name"], "count": r["tag_count"]} for r in rows]

    @synchronized
    def get_all_tags_map(self):
        """{subject_id: [tag_dict, ...]}"""
        rows = self.conn.execute(
            "SELECT subject_id, tag_name, tag_count FROM subject_tags"
        ).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row["subject_id"], []).append({
                "name": row["tag_name"],
                "count": row["tag_count"]
            })
        return result

    @synchronized
    def save_subject_full(self, data):
        """从 Bangumi v0 API 完整数据存入 subjects 表并保存标签。"""
        c = self.conn
        sid = data["id"]
        rating = data.get("rating")
        images = data.get("images") or {}
        rank = data.get("rank")
        if rank is None and rating:
            rank = rating.get("rank")
        c.execute(
            """INSERT INTO subjects
               (id, name, name_cn, url, air_date, air_weekday,
                rating, rank, summary, image_common, image_large, platform, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                   name = excluded.name,
                   name_cn = excluded.name_cn,
                   url = excluded.url,
                   air_date = excluded.air_date,
                   air_weekday = excluded.air_weekday,
                   rating = excluded.rating,
                   rank = excluded.rank,
                   summary = excluded.summary,
                   image_common = excluded.image_common,
                   image_large = excluded.image_large,
                   platform = COALESCE(excluded.platform, subjects.platform),
                   updated_at = datetime('now')""",
            (
                sid,
                data.get("name"),
                data.get("name_cn"),
                data.get("url") or f"https://bgm.tv/subject/{sid}",
                data.get("date"),
                data.get("air_weekday"),
                json.dumps(rating, ensure_ascii=False) if rating else None,
                rank,
                data.get("summary"),
                images.get("common"),
                images.get("large"),
                data.get("platform"),
            )
        )
        tags = data.get("tags", [])
        if tags:
            c.execute("DELETE FROM subject_tags WHERE subject_id = ?", (sid,))
            c.executemany(
                "INSERT INTO subject_tags (subject_id, tag_name, tag_count) VALUES (?, ?, ?)",
                [(sid, t["name"], t.get("count", 0)) for t in tags]
            )
        c.commit()

    # ─── Favorites ──────────────────────────────────────

    @synchronized
    def get_favorites(self):
        rows = self.conn.execute(
            """SELECT f.*, s.rating, s.rank
               FROM favorites f
               LEFT JOIN subjects s ON f.subject_id = s.id AND f.subject_id != 0
               ORDER BY f.added_at DESC"""
        ).fetchall()
        result = []
        for r in rows:
            fav = {"id": r["subject_id"], "name": r["name"], "img": r["img"], "url": r["url"]}
            if r["rating"]:
                fav["rating"] = json.loads(r["rating"])
                fav["rank"] = r["rank"]
            else:
                # JOIN 未命中，回退按名称匹配（覆盖 id=0 老数据 + 搜索结果不在 calendar 中的新条目）
                sub = self.conn.execute(
                    "SELECT rating, rank FROM subjects WHERE name = ? OR name_cn = ? LIMIT 1",
                    (r["name"], r["name"])
                ).fetchone()
                if sub and sub["rating"]:
                    fav["rating"] = json.loads(sub["rating"])
                    fav["rank"] = sub["rank"]
            result.append(fav)
        return result

    @synchronized
    def toggle_favorite(self, anime_data):
        name = anime_data.get("name")
        c = self.conn
        existing = c.execute(
            "SELECT subject_id FROM favorites WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            c.execute("DELETE FROM favorites WHERE name = ?", (name,))
            c.commit()
            return False
        else:
            sid = anime_data.get("id", 0)
            rating = anime_data.get("rating")
            rank = anime_data.get("rank")
            # 将番剧元数据写入 subjects 表，确保后续 get_favorites 的 JOIN 能命中
            # （搜索结果、非当季番剧的评分数据不在 calendar 中）
            if rating and sid:
                c.execute(
                    """INSERT OR IGNORE INTO subjects
                       (id, name, rating, rank, url, image_common)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (sid, name,
                     json.dumps(rating, ensure_ascii=False) if isinstance(rating, dict) else json.dumps({"score": rating}),
                     rank,
                     anime_data.get("url", ""),
                     anime_data.get("img", ""))
                )
            c.execute(
                "INSERT INTO favorites (subject_id, name, img, url) VALUES (?, ?, ?, ?)",
                (sid, name, anime_data.get("img", ""), anime_data.get("url", ""))
            )
            c.commit()
            return True

    # ─── Watch history ──────────────────────────────────

    @synchronized
    def get_watch_history(self):
        rows = self.conn.execute("SELECT * FROM watch_history").fetchall()
        result = {}
        for r in rows:
            result.setdefault(r["anime_name"], {})[str(r["episode"])] = {"watched": True}
        return result

    @synchronized
    def mark_watched(self, anime_name, episode_num):
        self.conn.execute(
            "INSERT OR REPLACE INTO watch_history (anime_name, episode, watched_at) VALUES (?, ?, datetime('now'))",
            (anime_name, episode_num)
        )
        self.conn.commit()


    # ─── Season Cache ──────────────────────────────────

    @synchronized
    def has_season_cache(self, year, month, max_age_hours=None):
        age_clause = ""
        params = [year, month]
        if max_age_hours is not None:
            age_clause = " AND fetched_at >= datetime('now', ?)"
            params.append(f"-{int(max_age_hours)} hours")
        r = self.conn.execute(
            """SELECT 1 FROM season_cache
               WHERE year = ? AND month = ? AND status = 'complete'"""
            + age_clause + " LIMIT 1",
            params
        ).fetchone()
        return r is not None

    @synchronized
    def save_season_batch(self, year, month, items):
        """items: list of subject dicts from /v0/subjects listing API"""
        c = self.conn
        c.execute(
            "DELETE FROM season_subjects WHERE year = ? AND month = ?",
            (year, month)
        )
        for sort_order, item in enumerate(items):
            sid = item["id"]
            rating = item.get("rating")
            images = item.get("images") or {}
            rank = item.get("rank")
            if rank is None and rating:
                rank = rating.get("rank")
            collection = item.get("collection")
            c.execute("""
                INSERT OR REPLACE INTO subjects
                (id, name, name_cn, url, air_date, rating, rank,
                 summary, image_common, image_large, collection, platform, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                sid, item.get("name"), item.get("name_cn"),
                item.get("url") or f"https://bgm.tv/subject/{sid}",
                item.get("date"),
                json.dumps(rating, ensure_ascii=False) if rating else None,
                rank, item.get("summary"),
                images.get("common"), images.get("large"),
                json.dumps(collection, ensure_ascii=False) if collection else None,
                item.get("platform"),
            ))
            c.execute(
                """INSERT OR REPLACE INTO season_subjects
                   (year, month, subject_id, sort_order) VALUES (?, ?, ?, ?)""",
                (year, month, sid, sort_order)
            )
        c.execute(
            """INSERT INTO season_cache (year, month, status, item_count, fetched_at)
               VALUES (?, ?, 'complete', ?, datetime('now'))
               ON CONFLICT(year, month) DO UPDATE SET
                   status = 'complete',
                   item_count = excluded.item_count,
                   fetched_at = datetime('now')""",
            (year, month, len(items))
        )
        c.commit()

    @synchronized
    def get_season_subject_ids(self, year, month):
        rows = self.conn.execute(
            """SELECT subject_id FROM season_subjects
               WHERE year = ? AND month = ?
               ORDER BY sort_order, subject_id""",
            (year, month)
        ).fetchall()
        return [r["subject_id"] for r in rows]

    @synchronized
    def get_subject(self, subject_id):
        return self.conn.execute(
            "SELECT * FROM subjects WHERE id = ?", (subject_id,)
        ).fetchone()

    @synchronized
    def has_subject(self, subject_id):
        return self.conn.execute(
            "SELECT 1 FROM subjects WHERE id = ?", (subject_id,)
        ).fetchone() is not None

    @synchronized
    def close(self):
        self.conn.close()
