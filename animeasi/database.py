"""
SQLite 数据库模块 — 统一管理日历 / 标签 / 收藏 / 观看记录
"""

import sqlite3
import json
import logging
import threading
from datetime import datetime, timezone
from functools import wraps

from animeasi.subjects.aliases import extract_search_aliases


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


def _to_local_time(value):
    """Convert a naive UTC timestamp from SQLite to the local timezone.

    SQLite ``datetime('now')`` stores UTC; user-facing RSS timestamps should
    display in the machine's local timezone.
    """
    if not value:
        return value
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return value

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
            CREATE TABLE IF NOT EXISTS subject_aliases (
                subject_id INTEGER,
                alias TEXT,
                PRIMARY KEY (subject_id, alias)
            );
            CREATE INDEX IF NOT EXISTS idx_subject_aliases_subject ON subject_aliases(subject_id);
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
            CREATE TABLE IF NOT EXISTS rss_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                keyword TEXT NOT NULL,
                search_aliases TEXT DEFAULT '',
                include_keywords TEXT DEFAULT '',
                exclude_keywords TEXT DEFAULT '',
                group_filter TEXT DEFAULT '',
                quality_filter TEXT DEFAULT '',
                save_path TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                auto_push INTEGER DEFAULT 0,
                last_checked_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS rss_download_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                source TEXT DEFAULT '',
                size TEXT DEFAULT '',
                save_path TEXT DEFAULT '',
                status TEXT NOT NULL,
                message TEXT DEFAULT '',
                pushed_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(subscription_id) REFERENCES rss_subscriptions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS rss_current_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subscription_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                source TEXT DEFAULT '',
                size TEXT DEFAULT '',
                meta_json TEXT DEFAULT '{}',
                resource_tags_json TEXT DEFAULT '[]',
                matched_keyword TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                message TEXT DEFAULT '',
                discovered_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY(subscription_id) REFERENCES rss_subscriptions(id) ON DELETE CASCADE,
                UNIQUE(subscription_id, url)
            );
            CREATE INDEX IF NOT EXISTS idx_rss_tasks_subscription ON rss_current_tasks(subscription_id, status, id DESC);
            CREATE INDEX IF NOT EXISTS idx_rss_records_subscription ON rss_download_records(subscription_id, pushed_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_records_url ON rss_download_records(subscription_id, url);
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
        rss_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(rss_subscriptions)")
        }
        if "search_aliases" not in rss_columns:
            self.conn.execute(
                "ALTER TABLE rss_subscriptions ADD COLUMN search_aliases TEXT DEFAULT ''"
            )
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
    def get_subject_aliases(self, subject_id):
        rows = self.conn.execute(
            "SELECT alias FROM subject_aliases WHERE subject_id = ? ORDER BY rowid",
            (subject_id,),
        ).fetchall()
        return [row["alias"] for row in rows]

    @synchronized
    def get_all_subject_aliases(self):
        rows = self.conn.execute(
            "SELECT subject_id, alias FROM subject_aliases ORDER BY rowid"
        ).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row["subject_id"], []).append(row["alias"])
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
        aliases = extract_search_aliases(data)
        c.execute("DELETE FROM subject_aliases WHERE subject_id = ?", (sid,))
        if aliases:
            c.executemany(
                "INSERT INTO subject_aliases (subject_id, alias) VALUES (?, ?)",
                [(sid, alias) for alias in aliases],
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
        name = str(anime_data.get("name") or "").strip()
        if not name:
            raise ValueError("Favorite name is required")

        try:
            sid = int(anime_data.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0

        c = self.conn
        if sid:
            existing = c.execute(
                "SELECT 1 FROM favorites WHERE subject_id = ?", (sid,)
            ).fetchone()
        else:
            existing = c.execute(
                "SELECT 1 FROM favorites WHERE subject_id = 0 AND name = ?", (name,)
            ).fetchone()

        if existing:
            if sid:
                c.execute("DELETE FROM favorites WHERE subject_id = ?", (sid,))
            else:
                c.execute(
                    "DELETE FROM favorites WHERE subject_id = 0 AND name = ?", (name,)
                )
            c.commit()
            return False

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

    # ─── RSS subscriptions ──────────────────────────────

    @staticmethod
    def _rss_subscription_from_row(row):
        return {
            "id": row["id"],
            "name": row["name"],
            "keyword": row["keyword"],
            "search_aliases": row["search_aliases"] or "",
            "include_keywords": row["include_keywords"] or "",
            "exclude_keywords": row["exclude_keywords"] or "",
            "group_filter": row["group_filter"] or "",
            "quality_filter": row["quality_filter"] or "",
            "save_path": row["save_path"] or "",
            "enabled": bool(row["enabled"]),
            "auto_push": bool(row["auto_push"]),
            "last_checked_at": _to_local_time(row["last_checked_at"]),
            "created_at": _to_local_time(row["created_at"]),
            "updated_at": _to_local_time(row["updated_at"]),
        }

    @synchronized
    def list_rss_subscriptions(self):
        rows = self.conn.execute(
            "SELECT * FROM rss_subscriptions ORDER BY enabled DESC, updated_at DESC, id DESC"
        ).fetchall()
        return [self._rss_subscription_from_row(row) for row in rows]

    @synchronized
    def get_rss_subscription(self, subscription_id):
        row = self.conn.execute(
            "SELECT * FROM rss_subscriptions WHERE id = ?",
            (subscription_id,)
        ).fetchone()
        return self._rss_subscription_from_row(row) if row else None

    @synchronized
    def save_rss_subscription(self, data):
        sub_id = data.get("id")
        name = (data.get("name") or data.get("keyword") or "未命名订阅").strip()
        keyword = (data.get("keyword") or name).strip()
        values = (
            name,
            keyword,
            data.get("search_aliases", "") or "",
            data.get("include_keywords", "") or "",
            data.get("exclude_keywords", "") or "",
            data.get("group_filter", "") or "",
            data.get("quality_filter", "") or "",
            data.get("save_path", "") or "",
            1 if data.get("enabled", True) else 0,
            1 if data.get("auto_push", False) else 0,
        )
        clear_tasks = False
        if sub_id:
            current = self.conn.execute(
                """SELECT keyword, search_aliases, include_keywords, exclude_keywords,
                          group_filter, quality_filter
                   FROM rss_subscriptions WHERE id = ?""",
                (sub_id,),
            ).fetchone()
            if current:
                previous_rule = tuple((current[key] or "") for key in (
                    "keyword", "search_aliases", "include_keywords", "exclude_keywords",
                    "group_filter", "quality_filter",
                ))
                next_rule = (values[1], values[2], values[3], values[4], values[5], values[6])
                clear_tasks = previous_rule != next_rule
            self.conn.execute(
                """UPDATE rss_subscriptions SET
                   name = ?, keyword = ?, search_aliases = ?, include_keywords = ?, exclude_keywords = ?,
                   group_filter = ?, quality_filter = ?, save_path = ?, enabled = ?,
                   auto_push = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                values + (sub_id,)
            )
            saved_id = int(sub_id)
            if clear_tasks:
                self.conn.execute(
                    "DELETE FROM rss_current_tasks WHERE subscription_id = ?",
                    (saved_id,),
                )
        else:
            cur = self.conn.execute(
                """INSERT INTO rss_subscriptions
                   (name, keyword, search_aliases, include_keywords, exclude_keywords,
                    group_filter, quality_filter, save_path, enabled, auto_push)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values
            )
            saved_id = cur.lastrowid
        self.conn.commit()
        return self.get_rss_subscription(saved_id)

    @synchronized
    def delete_rss_subscription(self, subscription_id):
        self.conn.execute("DELETE FROM rss_current_tasks WHERE subscription_id = ?", (subscription_id,))
        self.conn.execute("DELETE FROM rss_download_records WHERE subscription_id = ?", (subscription_id,))
        self.conn.execute("DELETE FROM rss_subscriptions WHERE id = ?", (subscription_id,))
        self.conn.commit()
        return True

    @synchronized
    def set_rss_subscription_enabled(self, subscription_id, enabled):
        self.conn.execute(
            "UPDATE rss_subscriptions SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if enabled else 0, subscription_id)
        )
        self.conn.commit()
        return self.get_rss_subscription(subscription_id)

    @synchronized
    def mark_rss_subscription_checked(self, subscription_id):
        self.conn.execute(
            "UPDATE rss_subscriptions SET last_checked_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
            (subscription_id,)
        )
        self.conn.commit()

    @synchronized
    def has_rss_download_record(self, subscription_id, url, title):
        row = self.conn.execute(
            """SELECT 1 FROM rss_download_records
               WHERE subscription_id = ? AND status = 'success'
                 AND (url = ? OR title = ?) LIMIT 1""",
            (subscription_id, url or "", title or "")
        ).fetchone()
        return row is not None

    @synchronized
    def record_rss_download(self, subscription_id, result, status, message, save_path):
        self.conn.execute(
            """INSERT OR REPLACE INTO rss_download_records
               (subscription_id, title, url, source, size, save_path, status, message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                subscription_id,
                result.get("title", ""),
                result.get("url", ""),
                result.get("source", ""),
                result.get("size", ""),
                save_path or "",
                status,
                message or "",
            )
        )
        self.conn.execute(
            """UPDATE rss_current_tasks SET status = ?, message = ?, updated_at = datetime('now')
               WHERE subscription_id = ? AND (url = ? OR title = ?)""",
            (status, message or "", subscription_id, result.get("url", ""), result.get("title", "")),
        )
        self.conn.commit()

    @synchronized
    def sync_rss_current_tasks(self, subscription_id, results, prune_missing=False):
        for result in results:
            url = str(result.get("url") or "")
            title = str(result.get("title") or "")
            if not url or not title:
                continue
            completed = self.conn.execute(
                """SELECT 1 FROM rss_download_records
                   WHERE subscription_id = ? AND status = 'success'
                     AND (url = ? OR title = ?) LIMIT 1""",
                (subscription_id, url, title),
            ).fetchone()
            status = "success" if completed else "pending"
            self.conn.execute(
                """INSERT INTO rss_current_tasks
                   (subscription_id, title, url, source, size, meta_json,
                    resource_tags_json, matched_keyword, status, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                   ON CONFLICT(subscription_id, url) DO UPDATE SET
                       title = excluded.title,
                       source = excluded.source,
                       size = excluded.size,
                       meta_json = excluded.meta_json,
                       resource_tags_json = excluded.resource_tags_json,
                       matched_keyword = excluded.matched_keyword,
                       status = CASE
                           WHEN rss_current_tasks.status = 'success' OR excluded.status = 'success'
                           THEN 'success' ELSE 'pending' END,
                       message = CASE
                           WHEN rss_current_tasks.status = 'success' THEN rss_current_tasks.message
                           ELSE '' END,
                       updated_at = datetime('now')""",
                (
                    subscription_id,
                    title,
                    url,
                    result.get("source", "") or "",
                    result.get("size", "") or "",
                    json.dumps(result.get("meta") or {}, ensure_ascii=False),
                    json.dumps(result.get("resource_tags") or [], ensure_ascii=False),
                    result.get("matched_keyword", "") or "",
                    status,
                ),
            )
        if prune_missing and results:
            # 整轮检查健康且返回了真实结果时，清理已从 RSS 源消失的任务。
            # 只清理未成功的（pending/error）；已下载成功的保留作可见记录。
            current_urls = [
                str(result.get("url") or "") for result in results if str(result.get("url") or "")
            ]
            if current_urls:
                placeholders = ",".join("?" for _ in current_urls)
                self.conn.execute(
                    f"""DELETE FROM rss_current_tasks
                        WHERE subscription_id = ? AND status != 'success'
                          AND url NOT IN ({placeholders})""",
                    [subscription_id, *current_urls],
                )
        self.conn.commit()

    @staticmethod
    def _rss_task_from_row(row):
        try:
            meta = json.loads(row["meta_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        try:
            resource_tags = json.loads(row["resource_tags_json"] or "[]")
        except (TypeError, ValueError):
            resource_tags = []
        return {
            "id": row["id"],
            "subscription_id": row["subscription_id"],
            "title": row["title"],
            "url": row["url"],
            "source": row["source"] or "",
            "size": row["size"] or "",
            "meta": meta,
            "resource_tags": resource_tags,
            "matched_keyword": row["matched_keyword"] or "",
            "status": row["status"] or "pending",
            "message": row["message"] or "",
            "discovered_at": _to_local_time(row["discovered_at"]),
            "updated_at": _to_local_time(row["updated_at"]),
        }

    @synchronized
    def list_rss_current_tasks(self, subscription_id=None, limit=500):
        params = []
        where = ""
        if subscription_id is not None:
            where = "WHERE subscription_id = ?"
            params.append(int(subscription_id))
        params.append(max(1, min(int(limit), 1000)))
        rows = self.conn.execute(
            f"""SELECT * FROM rss_current_tasks {where}
                ORDER BY subscription_id, id DESC LIMIT ?""",
            params,
        ).fetchall()
        return [self._rss_task_from_row(row) for row in rows]

    @synchronized
    def get_rss_current_tasks(self, subscription_id, task_ids):
        ids = [int(task_id) for task_id in task_ids][:50]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"""SELECT * FROM rss_current_tasks
                WHERE subscription_id = ? AND id IN ({placeholders})
                ORDER BY id""",
            [int(subscription_id), *ids],
        ).fetchall()
        return [self._rss_task_from_row(row) for row in rows]

    @synchronized
    def update_rss_current_task(self, task_id, status, message=""):
        self.conn.execute(
            """UPDATE rss_current_tasks SET status = ?, message = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (status, message or "", int(task_id)),
        )
        self.conn.commit()

    @synchronized
    def list_rss_download_records(self, limit=80):
        rows = self.conn.execute(
            """SELECT r.*, s.name AS subscription_name
               FROM rss_download_records r
               LEFT JOIN rss_subscriptions s ON s.id = r.subscription_id
               ORDER BY r.pushed_at DESC, r.id DESC
               LIMIT ?""",
            (int(limit),)
        ).fetchall()
        return [{
            "id": row["id"],
            "subscription_id": row["subscription_id"],
            "subscription_name": row["subscription_name"] or "",
            "title": row["title"],
            "url": row["url"],
            "source": row["source"],
            "size": row["size"],
            "save_path": row["save_path"],
            "status": row["status"],
            "message": row["message"],
            "pushed_at": _to_local_time(row["pushed_at"]),
        } for row in rows]
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
