import os
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

import database
import local_manager
import main


class DatabaseConcurrencyTests(unittest.TestCase):
    def test_concurrent_writes_are_serialized(self):
        db = database.AnimeDB(":memory:")
        errors = []

        def write_episode(episode):
            try:
                db.mark_watched("Test Anime", episode)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=write_episode, args=(episode,))
            for episode in range(1, 101)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(db.get_watch_history()["Test Anime"]), 100)
        db.close()

    def test_full_subject_update_preserves_collection(self):
        db = database.AnimeDB(":memory:")
        db.save_calendar([{
            "weekday": {"id": 1},
            "items": [{
                "id": 1,
                "name": "Test Anime",
                "collection": {"collect": 123},
                "platform": "TV",
                "images": {},
            }],
        }])

        db.save_subject_full({
            "id": 1,
            "name": "Test Anime",
            "summary": "Updated summary",
            "images": {},
        })

        row = db.get_subject(1)
        self.assertEqual(row["collection"], '{"collect": 123}')
        self.assertEqual(row["platform"], "TV")
        db.close()

    def test_season_cache_is_complete_even_when_empty(self):
        db = database.AnimeDB(":memory:")
        db.save_season_batch(2015, 1, [])

        self.assertTrue(db.has_season_cache(2015, 1))
        self.assertEqual(db.get_season_subject_ids(2015, 1), [])
        db.close()

    def test_season_cache_preserves_default_order(self):
        db = database.AnimeDB(":memory:")
        db.save_season_batch(2015, 1, [
            {"id": 30, "images": {}},
            {"id": 10, "images": {}},
            {"id": 20, "images": {}},
        ])

        self.assertEqual(db.get_season_subject_ids(2015, 1), [30, 10, 20])
        db.close()


class SeasonFetchTests(unittest.TestCase):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def setUp(self):
        self.api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        self.api.config = {"use_proxy": False}
        self.api.db = database.AnimeDB(":memory:")
        self.api.subject_tags_cache = {}

    def tearDown(self):
        self.api.db.close()

    def test_month_fetch_uses_exact_pagination(self):
        calls = []

        def fake_get(url, params, **kwargs):
            calls.append(params.copy())
            if params["offset"] == 0:
                return self.Response({
                    "total": 101,
                    "data": [{"id": i, "date": "2015-01-01"} for i in range(1, 101)],
                })
            return self.Response({
                "total": 101,
                "data": [{"id": 101, "date": "2015-01-31"}],
            })

        with mock.patch.object(main.requests, "get", side_effect=fake_get):
            items = self.api._fetch_season_month(2015, 1)

        self.assertEqual(len(items), 101)
        self.assertEqual([call["offset"] for call in calls], [0, 100])
        self.assertTrue(all(call["year"] == 2015 and call["month"] == 1 for call in calls))

    def test_month_fetch_retries_connect_timeout(self):
        success = self.Response({
            "total": 1,
            "data": [{"id": 1, "date": "2015-01-01"}],
        })
        responses = [
            main.requests.ConnectTimeout("timed out"),
            main.requests.ConnectTimeout("timed out"),
            success,
        ]

        with mock.patch.object(main.requests, "get", side_effect=responses) as request:
            with mock.patch.object(main.time, "sleep") as sleep:
                items = self.api._fetch_season_month(2015, 1)

        self.assertEqual([item["id"] for item in items], [1])
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_month_fetch_raises_after_retry_limit(self):
        with mock.patch.object(
            main.requests, "get",
            side_effect=main.requests.ConnectTimeout("timed out")
        ) as request:
            with mock.patch.object(main.time, "sleep"):
                with self.assertRaises(main.requests.ConnectTimeout):
                    self.api._fetch_season_month(2015, 1)

        self.assertEqual(request.call_count, 3)

    def test_partial_season_fetch_is_not_cached(self):
        def fake_month(year, month):
            if month == 2:
                raise RuntimeError("network failure")
            return [{"id": month, "date": f"{year}-{month:02d}-01", "images": {}}]

        with mock.patch.object(self.api, "_fetch_season_month", side_effect=fake_month):
            with self.assertRaises(RuntimeError):
                self.api._fetch_season_data(2015, 1)

        self.assertFalse(self.api.db.has_season_cache(2015, 1))

    def test_complete_season_fetch_deduplicates_and_caches(self):
        def fake_month(year, month):
            return [
                {"id": 1, "date": f"{year}-{month:02d}-01", "images": {}},
                {"id": month, "date": f"{year}-{month:02d}-02", "images": {}},
            ]

        with mock.patch.object(self.api, "_fetch_season_month", side_effect=fake_month):
            with mock.patch.object(self.api, "_preload_season_tags"):
                items = self.api._fetch_season_data(2015, 1)

        self.assertEqual({item["id"] for item in items}, {1, 2, 3})
        self.assertTrue(self.api.db.has_season_cache(2015, 1))
        self.assertEqual(set(self.api.db.get_season_subject_ids(2015, 1)), {1, 2, 3})

    def test_strict_japanese_filter_waits_for_tags_and_removes_non_japanese(self):
        self.api.config["only_show_japanese"] = True
        self.api.db.save_season_batch(2015, 1, [
            {"id": 1, "name": "Japanese", "platform": "TV", "images": {}},
            {"id": 2, "name": "Non Japanese", "platform": "TV", "images": {}},
        ])

        def load_tags(ids):
            self.api.subject_tags_cache[1] = [{"name": "日本", "count": 1}]
            self.api.subject_tags_cache[2] = [{"name": "中国", "count": 1}]
            for sid in ids:
                self.api.db.save_tags(sid, self.api.subject_tags_cache[sid])

        with mock.patch.object(self.api, "_load_season_tags", side_effect=load_tags) as loader:
            items = self.api._get_cached_season_items(2015, 1)

        loader.assert_called_once()
        self.assertEqual([item["id"] for item in items], [1])

    def test_season_mainline_rule_excludes_web_and_noise(self):
        japanese = [{"name": "日本"}]
        noisy = [{"name": "日本"}, {"name": "MV"}]

        self.assertTrue(self.api._is_season_mainline(japanese, "TV"))
        self.assertFalse(self.api._is_season_mainline(japanese, "WEB"))
        self.assertFalse(self.api._is_season_mainline(noisy, "TV"))
        self.assertFalse(self.api._is_season_mainline([{"name": "日本动画"}], "TV"))
        self.assertFalse(self.api._is_season_mainline(None, "TV"))

    def test_unfiltered_season_keeps_unknown_items(self):
        self.api.config["only_show_japanese"] = False
        item = self.api._item_to_season_dict({"id": 1, "images": {}})

        self.assertIsNone(item["is_japanese"])
        self.assertEqual(self.api._apply_season_japanese_filter([item]), [item])

    def test_season_items_cache_only_visible_covers(self):
        self.api.config["only_show_japanese"] = True
        visible = {"id": 1, "is_season_mainline": True, "images": {"common": "visible.jpg"}}
        hidden = {"id": 2, "is_season_mainline": False, "images": {"common": "hidden.jpg"}}

        with mock.patch.object(self.api, "_process_image_urls") as process_images:
            items = self.api._prepare_season_items([visible, hidden])

        self.assertEqual(items, [visible])
        process_images.assert_called_once_with([visible])


class LocalPlaybackSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_dir = os.path.join(self.temp_dir.name, "anime")
        os.mkdir(self.root_dir)
        self.db = mock.Mock()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rejects_file_outside_configured_root(self):
        outside_file = os.path.join(self.temp_dir.name, "outside.mp4")
        with open(outside_file, "wb"):
            pass

        result = local_manager.play_episode(
            "Test Anime", 1, outside_file, self.root_dir, self.db
        )

        self.assertEqual(result["status"], "error")
        self.db.mark_watched.assert_not_called()

    def test_rejects_non_video_inside_configured_root(self):
        text_file = os.path.join(self.root_dir, "episode.txt")
        with open(text_file, "wb"):
            pass

        result = local_manager.play_episode(
            "Test Anime", 1, text_file, self.root_dir, self.db
        )

        self.assertEqual(result["status"], "error")
        self.db.mark_watched.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows playback API")
    def test_allows_video_inside_configured_root(self):
        video_file = os.path.join(self.root_dir, "episode.mp4")
        with open(video_file, "wb"):
            pass

        with mock.patch.object(local_manager.os, "startfile") as startfile:
            result = local_manager.play_episode(
                "Test Anime", 1, video_file, self.root_dir, self.db
            )

        self.assertEqual(result["status"], "success")
        startfile.assert_called_once_with(os.path.realpath(video_file))
        self.db.mark_watched.assert_called_once_with("Test Anime", 1)


class FrontendEscapingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).resolve().parents[1] / "WEB" / "index.html"
        ).read_text(encoding="utf-8")

    def test_rss_title_is_escaped_for_text_and_attribute_contexts(self):
        self.assertIn(
            'title="${escHtml(t.title)}">${escapeHtml(t.title)}',
            self.html,
        )

    def test_inline_handler_escaping_blocks_html_entity_bypass(self):
        esc_attr_start = self.html.index("function escAttr(s)")
        esc_attr_end = self.html.index("function openLink", esc_attr_start)
        esc_attr_source = self.html[esc_attr_start:esc_attr_end]
        self.assertIn(".replace(/&/g, '&amp;')", esc_attr_source)

    def test_popularity_sort_falls_back_to_calendar_doing_count(self):
        self.assertEqual(
            self.html.count(
                "items = [...items].sort((a, b) => popularityCount(b) - popularityCount(a));"
            ),
            2,
        )
        popularity_start = self.html.index("function popularityCount(item)")
        popularity_end = self.html.index("function safeNumber", popularity_start)
        popularity_source = self.html[popularity_start:popularity_end]
        self.assertIn("collection.collect ?? collection.doing", popularity_source)

    def test_rating_sort_uses_rank_and_places_unranked_last(self):
        self.assertEqual(
            self.html.count("items = [...items].sort(compareByBangumiRank);"),
            2,
        )
        compare_start = self.html.index("function compareByBangumiRank")
        compare_end = self.html.index("function popularityCount", compare_start)
        compare_source = self.html[compare_start:compare_end]
        self.assertIn("return aRank - bRank", compare_source)
        self.assertIn("Number.POSITIVE_INFINITY", compare_source)

    def test_season_switch_has_loading_error_and_stale_request_protection(self):
        self.assertIn("function renderSeasonLoading(year, month)", self.html)
        self.assertIn("function renderSeasonError(year, month)", self.html)
        self.assertIn("season-spinner", self.html)
        self.assertIn("重新加载", self.html)
        self.assertIn("const loadToken = ++seasonLoadToken", self.html)
        self.assertIn("if (loadToken !== seasonLoadToken) return", self.html)
        self.assertIn("await renderSeasonGrid(loadToken)", self.html)
        self.assertIn("if (seasonIsLoading) return", self.html)


class PackagingTests(unittest.TestCase):
    def test_file_dialogs_use_pywebview_instead_of_tkinter(self):
        root = Path(__file__).resolve().parents[1]
        main_source = (root / "main.py").read_text(encoding="utf-8")
        spec_source = (root / "build.spec").read_text(encoding="utf-8")

        self.assertNotIn("from tkinter", main_source)
        self.assertNotIn("'tkinter'", spec_source)
        self.assertIn("webview.FileDialog.FOLDER", main_source)
        self.assertIn("webview.FileDialog.OPEN", main_source)


if __name__ == "__main__":
    unittest.main()
