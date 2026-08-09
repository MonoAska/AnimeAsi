import os
import json
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from animeasi import database, local_manager, rss_subscription
from animeasi.cache import cover_cache
from animeasi.downloads import downloader
import main
from animeasi.season import browser as season_browser
from animeasi.subjects import aliases as subject_aliases
from animeasi.subjects import schema as subject_schema


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
    def test_favorites_with_same_name_use_subject_id_identity(self):
        db = database.AnimeDB(":memory:")
        first = {"id": 1, "name": "Same Name", "img": "", "url": ""}
        second = {"id": 2, "name": "Same Name", "img": "", "url": ""}

        self.assertTrue(db.toggle_favorite(first))
        self.assertTrue(db.toggle_favorite(second))
        self.assertEqual({item["id"] for item in db.get_favorites()}, {1, 2})

        self.assertFalse(db.toggle_favorite(first))
        self.assertEqual([item["id"] for item in db.get_favorites()], [2])
        db.close()

    def test_favorite_requires_non_empty_name(self):
        db = database.AnimeDB(":memory:")

        with self.assertRaises(ValueError):
            db.toggle_favorite({"id": 1, "name": ""})

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
        self.temp_dir = tempfile.TemporaryDirectory()
        self.api.cache_path = self.temp_dir.name

    def tearDown(self):
        self.api.db.close()
        self.temp_dir.cleanup()

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

        with mock.patch.object(season_browser.requests, "get", side_effect=fake_get):
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
            season_browser.requests.ConnectTimeout("timed out"),
            season_browser.requests.ConnectTimeout("timed out"),
            success,
        ]

        with mock.patch.object(season_browser.requests, "get", side_effect=responses) as request:
            with mock.patch.object(season_browser.time, "sleep") as sleep:
                items = self.api._fetch_season_month(2015, 1)

        self.assertEqual([item["id"] for item in items], [1])
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_month_fetch_raises_after_retry_limit(self):
        with mock.patch.object(
            season_browser.requests, "get",
            side_effect=season_browser.requests.ConnectTimeout("timed out")
        ) as request:
            with mock.patch.object(season_browser.time, "sleep"):
                with self.assertRaises(season_browser.requests.ConnectTimeout):
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

    def test_cached_cover_rewrites_remote_url_to_local_cover_route(self):
        cover_path = os.path.join(self.api.cache_path, "cover.jpg")
        with open(cover_path, "wb") as cover:
            cover.write(b"x" * 20481)
        item = {
            "images": {
                "common": "https://lain.bgm.tv/pic/cover/c/cover.jpg?updated=1",
                "large": "https://lain.bgm.tv/pic/cover/l/cover.jpg?updated=1",
            }
        }

        with mock.patch.object(cover_cache.threading.Thread, "start") as start:
            self.api._process_image_urls([item])

        self.assertEqual(item["images"]["common"], "/covers/cover.jpg")
        self.assertEqual(item["images"]["large"], "/covers/cover.jpg")
        start.assert_not_called()

    def test_calendar_cache_uses_local_covers_when_files_exist(self):
        cover_path = os.path.join(self.api.cache_path, "calendar.jpg")
        with open(cover_path, "wb") as cover:
            cover.write(b"x" * 20481)
        self.api.cached_bgm_data = [{
            "weekday": {"id": 1},
            "items": [{
                "id": 1,
                "images": {
                    "common": "https://lain.bgm.tv/pic/cover/c/calendar.jpg",
                    "large": "https://lain.bgm.tv/pic/cover/l/calendar.jpg",
                },
            }],
        }]

        data = self.api.get_bgm_data()

        images = data[0]["items"][0]["images"]
        self.assertEqual(images["common"], "/covers/calendar.jpg")
        self.assertEqual(images["large"], "/covers/calendar.jpg")

    def test_favorites_use_local_covers_when_files_exist(self):
        cover_path = os.path.join(self.api.cache_path, "favorite.jpg")
        with open(cover_path, "wb") as cover:
            cover.write(b"x" * 20481)
        self.api.db.toggle_favorite({
            "id": 1,
            "name": "Favorite Anime",
            "img": "https://lain.bgm.tv/pic/cover/c/favorite.jpg",
            "url": "https://bgm.tv/subject/1",
        })

        favorites = self.api.get_favorites()

        self.assertEqual(favorites[0]["img"], "/covers/favorite.jpg")
        self.assertEqual(favorites[0]["images"]["common"], "/covers/favorite.jpg")
        self.assertEqual(favorites[0]["display_name"], "Favorite Anime")

    def test_subject_schema_normalizes_common_contract_fields(self):
        subject = subject_schema.normalize_subject({
            "id": 1,
            "name": "Original",
            "name_cn": "中文名",
            "date": "2026-01-01",
            "rating": {"score": 8.1, "rank": 0},
            "images": {"common": "cover.jpg"},
        })

        self.assertEqual(subject["display_name"], "中文名")
        self.assertEqual(subject["air_date"], "2026-01-01")
        self.assertIsNone(subject["rank"])
        self.assertEqual(subject["images"], {"common": "cover.jpg", "large": "cover.jpg"})
        self.assertIn("collection", subject)
        self.assertIn("is_season_mainline", subject)


    def test_subject_search_aliases_keep_chinese_and_romanized_names_only(self):
        data = {
            "id": 576121,
            "name": "あかね噺",
            "name_cn": "落语朱音",
            "infobox": [
                {"key": "别名", "value": [
                    {"v": "朱音落语"},
                    {"v": "Akane-banashi"},
                    {"v": "あかねばなし"},
                ]},
            ],
        }
        self.assertEqual(
            subject_aliases.extract_search_aliases(data),
            ["落语朱音", "朱音落语", "Akane-banashi"],
        )
        db = database.AnimeDB(":memory:")
        db.save_subject_full(data)
        self.assertEqual(
            db.get_subject_aliases(576121),
            ["落语朱音", "朱音落语", "Akane-banashi"],
        )
        db.close()

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


class TorrentMetadataTests(unittest.TestCase):
    def test_parse_single_episode_title(self):
        title = "[喵萌奶茶屋] Summer Pockets - 06 [1080p][简繁][HEVC][MKV]"

        meta = downloader.parse_torrent_title(title)
        tags = downloader.build_resource_tags(meta, "Mikan", "742 MB")

        self.assertEqual(meta["group"], "喵萌奶茶屋")
        self.assertEqual(meta["episode"], "06")
        self.assertFalse(meta["is_batch"])
        self.assertEqual(meta["resolution"], "1080p")
        self.assertEqual(meta["subtitle"], "简繁")
        self.assertEqual(meta["codec"], "HEVC")
        self.assertEqual(meta["container"], "MKV")
        self.assertEqual(tags, ["喵萌奶茶屋", "EP 06", "1080p", "简繁", "HEVC", "MKV", "742 MB"])

    def test_parse_batch_title(self):
        title = "Some Anime Complete Batch 01-12 1080p CHS x264 MP4"

        meta = downloader.parse_torrent_title(title)
        tags = downloader.build_resource_tags(meta, "Nyaa.si", "")

        self.assertTrue(meta["is_batch"])
        self.assertEqual(meta["episode_range"], "01-12")
        self.assertIn("合集", tags)
        self.assertIn("01-12", tags)
        self.assertNotIn("EP 01", tags)

    def test_season_dash_episode_is_not_treated_as_batch(self):
        title = "[Group] Dorohedoro Season 2-06 [1080p][CHS][HEVC][MKV]"

        meta = downloader.parse_torrent_title(title)
        tags = downloader.build_resource_tags(meta, "Mikan", "1.2 GB")

        self.assertFalse(meta["is_batch"])
        self.assertEqual(meta["episode"], "06")
        self.assertEqual(meta["episode_range"], "")
        self.assertIn("EP 06", tags)
        self.assertNotIn("合集", tags)

    def test_parses_compact_season_episode_notation(self):
        meta = downloader.parse_torrent_title(
            "[Group] Akane-banashi - S01E11 [1080p][HEVC]"
        )
        self.assertEqual(meta["episode"], "11")
        self.assertFalse(meta["is_batch"])
    def test_ignores_untrusted_tiny_rss_size(self):
        entry = {
            "title": "[Group] Anime - 06 [1080p][HEVC][1.3GB]",
            "enclosures": [{"length": "1"}],
        }

        self.assertEqual(downloader._extract_entry_size(entry), "1.3 GB")

    def test_deduplicates_same_release_signature_and_prefers_size(self):
        meta = downloader.parse_torrent_title("[Group] Anime - 06 [1080p][CHS][HEVC][MKV]")
        duplicate_without_size = {
            "title": "[Group] Anime - 06 [1080p][CHS][HEVC][MKV]",
            "url": "https://example.test/a.torrent",
            "source": "Mikan",
            "size": "",
            "meta": meta,
        }
        duplicate_with_size = {
            "title": "[Group] Anime - 06 [1080p][CHS][HEVC][MKV]",
            "url": "https://example.test/b.torrent",
            "source": "Mikan",
            "size": "1.3 GB",
            "meta": meta,
        }

        results = downloader._dedupe_torrent_results([
            duplicate_without_size,
            duplicate_with_size,
        ])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["size"], "1.3 GB")

    def test_extracts_torrent_url_with_query_and_rejects_detail_page(self):
        valid = {
            "enclosures": [{"href": "https://example.test/file.torrent?download=1"}],
            "links": [{"href": "https://example.test/view/123"}],
        }
        invalid = {
            "link": "https://example.test/view/123",
            "links": [{"href": "https://example.test/view/123"}],
        }

        self.assertEqual(
            downloader._extract_torrent_url(valid),
            "https://example.test/file.torrent?download=1",
        )
        self.assertIsNone(downloader._extract_torrent_url(invalid))

    def test_search_results_skip_entries_without_download_url(self):
        class Response:
            content = b"rss"

            def raise_for_status(self):
                return None

        feed = mock.Mock()
        feed.entries = [
            {
                "title": "Detail Page Only",
                "link": "https://example.test/view/1",
                "links": [{"href": "https://example.test/view/1"}],
            },
            {
                "title": "[Group] Anime - 06 [1080p]",
                "links": [{"href": "https://example.test/download/6.torrent"}],
            },
        ]

        source = downloader.RSSSource("Test", "https://example.test/rss?q={keyword}")
        with mock.patch.object(downloader.requests, "get", return_value=Response()):
            with mock.patch.object(downloader.feedparser, "parse", return_value=feed):
                results = downloader._search_single_source("Anime", source)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "[Group] Anime - 06 [1080p]")

    def test_search_torrents_distinguishes_empty_from_source_failure(self):
        source = [{"name": "Test", "url_template": "https://example.test/rss?q={keyword}", "enabled": True}]

        with mock.patch.object(downloader.torrent_sources, "search_single_source_with_error", return_value=([], "")):
            status, results, stats = downloader.search_torrents("Anime", source)

        self.assertEqual(status, "empty")
        self.assertEqual(results, [])
        self.assertTrue(stats[0]["ok"])

        with mock.patch.object(downloader.torrent_sources, "search_single_source_with_error", return_value=([], "timeout")):
            status, results, stats = downloader.search_torrents("Anime", source)

        self.assertEqual(status, "error")
        self.assertEqual(results, [])
        self.assertFalse(stats[0]["ok"])
        self.assertEqual(stats[0]["failed_queries"], 1)
        self.assertEqual(stats[0]["error"], "timeout")

    def test_search_torrents_reports_partial_source_failures(self):
        source = [
            {"name": "Good Source", "url_template": "https://good.test/rss?q={keyword}", "enabled": True},
            {"name": "Bad Source", "url_template": "https://bad.test/rss?q={keyword}", "enabled": True},
            {"name": "Disabled Source", "url_template": "https://off.test/rss?q={keyword}", "enabled": False},
        ]

        def fake_search(keyword, _source, _proxies=None):
            if _source.name == "Bad Source":
                return ([], "timeout")
            return ([{
                "title": "[Group] Anime - 06 [1080p]",
                "url": "https://good.test/6.torrent",
                "meta": {"group": "Group", "episode": "06", "resolution": "1080p"},
            }], "")

        with mock.patch.object(
            downloader.torrent_sources,
            "search_single_source_with_error",
            side_effect=fake_search,
        ):
            status, results, stats = downloader.search_torrents("Anime", source)

        self.assertEqual(status, "partial")
        self.assertEqual(len(results), 1)
        by_name = {stat["name"]: stat for stat in stats}
        self.assertEqual(len(by_name), 2)
        self.assertTrue(by_name["Good Source"]["ok"])
        self.assertEqual(by_name["Good Source"]["result_count"], 1)
        self.assertFalse(by_name["Bad Source"]["ok"])
        self.assertEqual(by_name["Bad Source"]["failed_queries"], 1)
        self.assertEqual(by_name["Bad Source"]["error"], "timeout")
        self.assertNotIn("Disabled Source", by_name)

    def test_deduplicates_same_release_across_sources(self):
        meta = downloader.parse_torrent_title("[Group] Anime - 06 [1080p][CHS][HEVC][MKV]")
        results = downloader._dedupe_torrent_results([
            {
                "title": "[Group] Anime - 06 [1080p][CHS][HEVC][MKV]",
                "url": "https://source-a.test/a.torrent",
                "source": "Source A",
                "size": "",
                "meta": meta,
            },
            {
                "title": "[Group] Anime - 06 [1080p][CHS][HEVC][MKV]",
                "url": "https://source-b.test/b.torrent",
                "source": "Source B",
                "size": "",
                "meta": meta,
            },
        ])

        self.assertEqual(len(results), 1)

    def test_dedup_prefers_sized_torrent_over_unsized_magnet(self):
        meta = downloader.parse_torrent_title("[Group] Anime - 06 [1080p][CHS][HEVC][MKV]")
        results = downloader._dedupe_torrent_results([
            {
                "title": "[Group] Anime - 06 [1080p][CHS][HEVC][MKV]",
                "url": "https://example.test/6.torrent",
                "source": "Source A",
                "size": "1.3 GB",
                "meta": meta,
            },
            {
                "title": "[Group] Anime - 06 [1080p][CHS][HEVC][MKV]",
                "url": "magnet:?xt=urn:btih:abcdef",
                "source": "Source B",
                "size": "",
                "meta": meta,
            },
        ])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["size"], "1.3 GB")
        self.assertEqual(results[0]["url"], "https://example.test/6.torrent")



    def test_search_torrents_merges_multiple_aliases_before_dedup(self):
        source = [{"name": "Test", "url_template": "https://example.test/rss?q={keyword}", "enabled": True}]

        def fake_search(keyword, _source, _proxies=None):
            episode = "01" if keyword == "中文名" else "02"
            return ([{
                "title": f"[Group] Anime - {episode} [1080p]",
                "url": f"magnet:?xt={episode}",
                "meta": {"group": "Group", "episode": episode, "resolution": "1080p"},
            }], "")

        with mock.patch.object(downloader.torrent_sources, "search_single_source_with_error", side_effect=fake_search) as search:
            status, results, stats = downloader.search_torrents(["中文名", "Anime-Romaji"], source)

        self.assertEqual(status, "success")
        self.assertEqual([item["meta"]["episode"] for item in results], ["01", "02"])
        self.assertEqual([item["matched_keyword"] for item in results], ["中文名", "Anime-Romaji"])
        self.assertTrue(stats[0]["ok"])
        self.assertEqual(stats[0]["total_queries"], 2)
        self.assertEqual(search.call_count, 2)
        self.assertIn("c=1_0", downloader.PRESET_SOURCES[1]["url_template"])
        self.assertFalse(downloader.torrent_sources.is_probable_anime_video_title("Anime OST [FLAC]"))

class RSSSubscriptionTests(unittest.TestCase):
    def test_rss_subscription_crud_and_history(self):
        db = database.AnimeDB(":memory:")
        sub = db.save_rss_subscription({
            "name": "Test Plan",
            "keyword": "Test Anime",
            "search_aliases": "Test-Anime",
            "group_filter": "Group",
            "quality_filter": "1080p",
            "auto_push": True,
        })

        self.assertEqual(sub["name"], "Test Plan")
        self.assertEqual(sub["search_aliases"], "Test-Anime")
        self.assertEqual(rss_subscription.subscription_search_keywords(sub), ["Test Anime", "Test-Anime"])
        self.assertTrue(sub["enabled"])
        self.assertTrue(sub["auto_push"])
        self.assertEqual(len(db.list_rss_subscriptions()), 1)

        result = {"title": "[Group] Test Anime - 06 [1080p]", "url": "magnet:?xt=1", "source": "Mikan", "size": "1.2 GB"}
        self.assertFalse(db.has_rss_download_record(sub["id"], result["url"], result["title"]))
        db.record_rss_download(sub["id"], result, "success", "ok", "D:/Anime/Test")
        self.assertTrue(db.has_rss_download_record(sub["id"], result["url"], result["title"]))
        history = db.list_rss_download_records()
        self.assertEqual(history[0]["subscription_name"], "Test Plan")
        self.assertEqual(history[0]["size"], "1.2 GB")
        db.close()

    def test_rss_current_tasks_persist_metadata_and_download_status(self):
        db = database.AnimeDB(":memory:")
        sub = db.save_rss_subscription({"name": "Plan", "keyword": "Anime"})
        resources = [
            {
                "title": "[Group A] Anime - 01 [1080p]",
                "url": "magnet:?xt=one-a",
                "source": "Test",
                "size": "1.2 GB",
                "meta": {"group": "Group A", "episode": "01", "resolution": "1080p"},
                "resource_tags": ["Group A", "EP 01", "1080p"],
            },
            {
                "title": "[Group B] Anime - 01 [720p]",
                "url": "magnet:?xt=one-b",
                "source": "Test",
                "meta": {"group": "Group B", "episode": "01", "resolution": "720p"},
            },
        ]
        db.sync_rss_current_tasks(sub["id"], resources)
        tasks = db.list_rss_current_tasks(sub["id"])
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["meta"]["episode"], "01")
        self.assertEqual(tasks[0]["status"], "pending")

        db.record_rss_download(sub["id"], tasks[0], "success", "ok", "D:/Anime")
        db.sync_rss_current_tasks(sub["id"], resources)
        refreshed = {task["url"]: task for task in db.list_rss_current_tasks(sub["id"])}
        self.assertEqual(refreshed[tasks[0]["url"]]["status"], "success")
        db.save_rss_subscription({"id": sub["id"], "name": "Other", "keyword": "Other"})
        self.assertEqual(db.list_rss_current_tasks(sub["id"]), [])
        db.delete_rss_subscription(sub["id"])
        self.assertEqual(db.list_rss_current_tasks(sub["id"]), [])
        db.close()

    def test_rss_current_tasks_prune_missing(self):
        db = database.AnimeDB(":memory:")
        sub = db.save_rss_subscription({"name": "Plan", "keyword": "Anime"})
        resources = [
            {"title": "Anime - 01", "url": "magnet:?xt=01"},
            {"title": "Anime - 02", "url": "magnet:?xt=02"},
        ]
        db.sync_rss_current_tasks(sub["id"], resources)

        # prune_missing=False 时过期任务保留（老行为）
        db.sync_rss_current_tasks(
            sub["id"],
            [{"title": "Anime - 03", "url": "magnet:?xt=03"}],
            prune_missing=False,
        )
        remaining = {task["url"] for task in db.list_rss_current_tasks(sub["id"])}
        self.assertEqual(remaining, {"magnet:?xt=01", "magnet:?xt=02", "magnet:?xt=03"})

        # 已下载成功的任务不会被 prune 删掉
        success_task = next(task for task in db.list_rss_current_tasks(sub["id"]) if task["url"] == "magnet:?xt=01")
        db.record_rss_download(sub["id"], success_task, "success", "ok", "D:/Anime")

        # prune_missing=True 只清理未成功的过期任务
        db.sync_rss_current_tasks(
            sub["id"],
            [{"title": "Anime - 03", "url": "magnet:?xt=03"}],
            prune_missing=True,
        )
        remaining = {task["url"] for task in db.list_rss_current_tasks(sub["id"])}
        self.assertEqual(remaining, {"magnet:?xt=01", "magnet:?xt=03"})
        db.close()

    def test_rss_batch_push_records_each_selected_task(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "local_anime_path": "D:/Anime",
            "qbt_host": "127.0.0.1:8080",
            "qbt_password": "",
            "qbt_auto_launch": False,
            "qbt_exe_path": "",
        }
        api.db = database.AnimeDB(":memory:")
        sub = api.db.save_rss_subscription({"name": "Anime", "keyword": "Anime"})
        api.db.sync_rss_current_tasks(sub["id"], [
            {"title": "Anime - 01", "url": "magnet:?xt=01", "meta": {"episode": "01"}},
            {"title": "Anime - 02", "url": "magnet:?xt=02", "meta": {"episode": "02"}},
        ])
        tasks = api.db.list_rss_current_tasks(sub["id"])
        task_ids = [task["id"] for task in tasks]
        with mock.patch.object(
            rss_subscription.downloader,
            "push_to_qbittorrent",
            side_effect=[("success", "ok"), ("error", "offline")],
        ) as push:
            result = rss_subscription.push_tasks(api, sub["id"], task_ids)

        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["pushed"]), 1)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(push.call_count, 2)
        statuses = {task["status"] for task in api.db.list_rss_current_tasks(sub["id"])}
        self.assertEqual(statuses, {"success", "error"})
        api.db.close()
    def test_rss_check_filters_rules_and_pushes_only_unrecorded_first_match(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "rss_sources": [{"name": "Test", "url_template": "https://example.test/rss?q={keyword}", "enabled": True}],
            "local_anime_path": "D:/Anime",
            "qbt_host": "127.0.0.1:8080",
            "qbt_password": "",
            "qbt_auto_launch": False,
            "qbt_exe_path": "",
        }
        api.db = database.AnimeDB(":memory:")
        api._get_proxies = lambda: None
        sub = api.db.save_rss_subscription({
            "name": "Test Anime",
            "keyword": "Test Anime",
            "group_filter": "Group",
            "quality_filter": "1080p",
            "exclude_keywords": "720p",
            "auto_push": True,
        })
        api.db.record_rss_download(
            sub["id"],
            {"title": "[Group] Test Anime - 05 [1080p]", "url": "magnet:?xt=old"},
            "success",
            "ok",
            "D:/Anime/Test Anime",
        )
        results = [
            {"title": "[Group] Test Anime - 06 [1080p]", "url": "magnet:?xt=new", "source": "Test", "size": "1.3 GB"},
            {"title": "[Group] Test Anime - 05 [1080p]", "url": "magnet:?xt=old", "source": "Test", "size": "1.2 GB"},
            {"title": "[Other] Test Anime - 07 [1080p]", "url": "magnet:?xt=other", "source": "Test", "size": "1.4 GB"},
            {"title": "[Group] Test Anime - 08 [720p]", "url": "magnet:?xt=bad", "source": "Test", "size": "700 MB"},
        ]
        stats = [{
            "name": "Test", "ok": True, "total_queries": 1,
            "failed_queries": 0, "result_count": len(results), "error": "",
        }]
        with mock.patch.object(rss_subscription.downloader, "search_torrents", return_value=("success", results, stats)):
            with mock.patch.object(rss_subscription.downloader, "push_to_qbittorrent", return_value=("success", "ok")) as push:
                res = rss_subscription.check_subscription(api, sub["id"])

        self.assertEqual(res["status"], "success")
        self.assertEqual(len(res["results"]), 2)
        self.assertEqual(len(res["pushed"]), 1)
        self.assertEqual(res["pushed"][0]["url"], "magnet:?xt=new")
        self.assertEqual(res["source_stats"], stats)
        push.assert_called_once()
        self.assertTrue(api.db.has_rss_download_record(sub["id"], "magnet:?xt=new", "[Group] Test Anime - 06 [1080p]"))
        tasks = api.db.list_rss_current_tasks(sub["id"])
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(task["status"] == "success" for task in tasks))
        api.db.close()

    def test_rss_check_failure_keeps_last_checked_at(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "rss_sources": [{"name": "Test", "url_template": "https://example.test/rss?q={keyword}", "enabled": True}],
        }
        api._get_proxies = lambda: None
        api.db = database.AnimeDB(":memory:")
        sub = api.db.save_rss_subscription({"name": "Test Anime", "keyword": "Test Anime"})
        stats = [{
            "name": "Test", "ok": False, "total_queries": 1,
            "failed_queries": 1, "result_count": 0, "error": "timeout",
        }]

        with mock.patch.object(
            rss_subscription.downloader,
            "search_torrents",
            return_value=("error", [], stats),
        ):
            res = rss_subscription.check_subscription(api, sub["id"])

        self.assertEqual(res["status"], "error")
        self.assertEqual(res["source_stats"], stats)
        self.assertIsNone(api.db.get_rss_subscription(sub["id"])["last_checked_at"])
        api.db.close()

    def test_rss_check_partial_reports_stats_and_marks_checked(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "rss_sources": [{"name": "Test", "url_template": "https://example.test/rss?q={keyword}", "enabled": True}],
        }
        api._get_proxies = lambda: None
        api.db = database.AnimeDB(":memory:")
        sub = api.db.save_rss_subscription({"name": "Test Anime", "keyword": "Test Anime"})
        results = [{"title": "[Group] Test Anime - 06 [1080p]", "url": "magnet:?xt=new", "source": "Test"}]
        stats = [
            {"name": "Good", "ok": True, "total_queries": 1, "failed_queries": 0, "result_count": 1, "error": ""},
            {"name": "Bad", "ok": False, "total_queries": 1, "failed_queries": 1, "result_count": 0, "error": "timeout"},
        ]

        with mock.patch.object(
            rss_subscription.downloader,
            "search_torrents",
            return_value=("partial", results, stats),
        ):
            res = rss_subscription.check_subscription(api, sub["id"])

        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["source_stats"], stats)
        self.assertEqual(len(res["results"]), 1)
        self.assertIsNotNone(api.db.get_rss_subscription(sub["id"])["last_checked_at"])
        api.db.close()

    def test_rss_check_healthy_prunes_stale_pending_tasks(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "rss_sources": [{"name": "Test", "url_template": "https://example.test/rss?q={keyword}", "enabled": True}],
            "local_anime_path": "D:/Anime",
            "qbt_host": "127.0.0.1:8080",
            "qbt_password": "",
            "qbt_auto_launch": False,
            "qbt_exe_path": "",
        }
        api._get_proxies = lambda: None
        api.db = database.AnimeDB(":memory:")
        sub = api.db.save_rss_subscription({"name": "Test Anime", "keyword": "Test Anime"})
        api.db.sync_rss_current_tasks(sub["id"], [
            {"title": "[Group] Test Anime - 01 [1080p]", "url": "magnet:?xt=stale"},
        ])
        results = [{"title": "[Group] Test Anime - 02 [1080p]", "url": "magnet:?xt=new", "source": "Test"}]
        stats = [{
            "name": "Test", "ok": True, "total_queries": 1,
            "failed_queries": 0, "result_count": len(results), "error": "",
        }]

        with mock.patch.object(
            rss_subscription.downloader,
            "search_torrents",
            return_value=("success", results, stats),
        ):
            res = rss_subscription.check_subscription(api, sub["id"])

        self.assertEqual(res["status"], "success")
        remaining = {task["url"] for task in api.db.list_rss_current_tasks(sub["id"])}
        self.assertEqual(remaining, {"magnet:?xt=new"})
        api.db.close()

    def test_rss_check_partial_keeps_stale_tasks(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "rss_sources": [{"name": "Test", "url_template": "https://example.test/rss?q={keyword}", "enabled": True}],
        }
        api._get_proxies = lambda: None
        api.db = database.AnimeDB(":memory:")
        sub = api.db.save_rss_subscription({"name": "Test Anime", "keyword": "Test Anime"})
        api.db.sync_rss_current_tasks(sub["id"], [
            {"title": "[Group] Test Anime - 01 [1080p]", "url": "magnet:?xt=stale"},
        ])
        results = [{"title": "[Group] Test Anime - 02 [1080p]", "url": "magnet:?xt=new", "source": "Test"}]
        stats = [
            {"name": "Good", "ok": True, "total_queries": 1, "failed_queries": 0, "result_count": 1, "error": ""},
            {"name": "Bad", "ok": False, "total_queries": 1, "failed_queries": 1, "result_count": 0, "error": "timeout"},
        ]

        with mock.patch.object(
            rss_subscription.downloader,
            "search_torrents",
            return_value=("partial", results, stats),
        ):
            res = rss_subscription.check_subscription(api, sub["id"])

        self.assertEqual(res["status"], "partial")
        remaining = {task["url"] for task in api.db.list_rss_current_tasks(sub["id"])}
        self.assertEqual(remaining, {"magnet:?xt=stale", "magnet:?xt=new"})
        api.db.close()

    def test_rss_push_uses_configured_qbt_username(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "local_anime_path": "D:/Anime",
            "qbt_host": "127.0.0.1:8080",
            "qbt_username": "custom-user",
            "qbt_password": "secret",
            "qbt_auto_launch": False,
            "qbt_exe_path": "",
        }
        api.db = database.AnimeDB(":memory:")
        sub = api.db.save_rss_subscription({"name": "Anime", "keyword": "Anime"})
        api.db.sync_rss_current_tasks(sub["id"], [
            {"title": "Anime - 01", "url": "magnet:?xt=01", "meta": {"episode": "01"}},
        ])
        task_ids = [task["id"] for task in api.db.list_rss_current_tasks(sub["id"])]

        with mock.patch.object(
            rss_subscription.downloader,
            "push_to_qbittorrent",
            return_value=("success", "ok"),
        ) as push:
            result = rss_subscription.push_tasks(api, sub["id"], task_ids)

        self.assertEqual(result["status"], "success")
        qbt_config = push.call_args.args[1]
        self.assertEqual(qbt_config["username"], "custom-user")
        self.assertEqual(qbt_config["password"], "secret")
        api.db.close()

    def test_main_push_download_uses_configured_qbt_username(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "qbt_host": "127.0.0.1:8080",
            "qbt_username": "custom-user",
            "qbt_password": "secret",
            "qbt_auto_launch": False,
            "qbt_exe_path": "",
        }
        with mock.patch.object(
            main.downloader,
            "push_to_qbittorrent",
            return_value=("success", "ok"),
        ) as push:
            res = api.push_download("magnet:?xt=01", "Anime", "D:/Anime")

        self.assertEqual(res["status"], "success")
        qbt_config = push.call_args.args[1]
        self.assertEqual(qbt_config["username"], "custom-user")
        self.assertEqual(qbt_config["save_path"], "D:/Anime")
    def test_rss_preview_uses_shared_rules_and_explains_rejections(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "rss_sources": [{"name": "Test", "url_template": "https://example.test/rss?q={keyword}", "enabled": True}],
        }
        api._get_proxies = lambda: None
        results = [
            {"title": "[Group] Test Anime - 06 [1080p][HEVC]", "url": "magnet:?xt=ok"},
            {"title": "[Other] Test Anime - 07 [720p]", "url": "magnet:?xt=bad"},
        ]
        rule = {
            "keyword": "Test Anime",
            "group_filter": "Group",
            "quality_filter": "1080p",
            "include_keywords": "HEVC",
            "exclude_keywords": "720p",
        }
        stats = [{
            "name": "Test", "ok": True, "total_queries": 1,
            "failed_queries": 0, "result_count": len(results), "error": "",
        }]
        with mock.patch.object(rss_subscription.downloader, "search_torrents", return_value=("success", results, stats)):
            preview = rss_subscription.preview_subscription(api, rule)

        self.assertEqual(preview["status"], "success")
        self.assertEqual(preview["source_stats"], stats)
        self.assertEqual(preview["matched_count"], 1)
        self.assertTrue(preview["results"][0]["matched"])
        self.assertFalse(preview["results"][1]["matched"])
        self.assertTrue(any("字幕组不匹配" in reason for reason in preview["results"][1]["reasons"]))
        self.assertTrue(any("命中排除词" in reason for reason in preview["results"][1]["reasons"]))

    def test_rss_check_interval_getter_setter(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config_path = os.path.join(tempfile.mkdtemp(), "config.json")
        api.config = {"rss_check_interval_minutes": 0}

        self.assertEqual(api.get_rss_check_interval(), 0)
        result = api.set_rss_check_interval(30)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["interval_minutes"], 30)
        self.assertEqual(api.get_rss_check_interval(), 30)
        with open(api.config_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["rss_check_interval_minutes"], 30)

        result = api.set_rss_check_interval(0)
        self.assertEqual(result["status"], "success")
        self.assertEqual(api.get_rss_check_interval(), 0)
        result = api.set_rss_check_interval("abc")
        self.assertEqual(result["status"], "error")
        result = api.set_rss_check_interval(-5)
        self.assertEqual(result["status"], "error")
        self.assertEqual(api.get_rss_check_interval(), 0)

    def test_rss_scheduler_run_once_checks_enabled_subscriptions_only(self):
        api = main.AnimeProAPI.__new__(main.AnimeProAPI)
        api.config = {
            "rss_check_interval_minutes": 30,
            "rss_sources": [{"name": "Test", "url_template": "https://example.test/rss?q={keyword}", "enabled": True}],
        }
        api._get_proxies = lambda: None
        api.db = database.AnimeDB(":memory:")
        api._rss_check_running = False
        api.db.save_rss_subscription({"name": "On Anime", "keyword": "On Anime", "enabled": True})
        api.db.save_rss_subscription({"name": "Off Anime", "keyword": "Off Anime", "enabled": False})

        with mock.patch.object(
            rss_subscription.downloader,
            "search_torrents",
            return_value=("empty", [], []),
        ) as search:
            api._rss_scheduler_run_once()

        self.assertEqual(search.call_count, 1)
        self.assertFalse(api._rss_check_running)
        self.assertIsNotNone(api.db.get_rss_subscription(1)["last_checked_at"])
        api.db.close()

class FrontendEscapingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1] / "WEB"
        cls.html = (root / "index.html").read_text(encoding="utf-8")
        cls.css = (root / "static" / "css" / "app.css").read_text(encoding="utf-8")
        cls.cards_js = (root / "static" / "js" / "cards.js").read_text(encoding="utf-8")
        cls.downloads_js = (root / "static" / "js" / "downloads.js").read_text(encoding="utf-8")
        cls.rss_js = (root / "static" / "js" / "rss.js").read_text(encoding="utf-8")

    def test_rss_title_is_escaped_for_text_and_attribute_contexts(self):
        self.assertIn(
            'title="${escHtml(t.title)}">${escapeHtml(t.title)}',
            self.downloads_js,
        )
        self.assertIn("function renderTorrentTags(t)", self.downloads_js)
        self.assertIn("escapeHtml(tagText)", self.downloads_js)

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

    def test_anime_cards_use_shared_renderer_with_favorite_play_overlay(self):
        self.assertIn("function createAnimeCard(item, options = {})", self.cards_js)
        self.assertEqual(self.cards_js.count("card.innerHTML = `"), 1)
        self.assertIn("playName: favName", self.html)
        self.assertIn("openEpisodeModal('${escAttr(options.playName)}')", self.cards_js)
        self.assertIn("fragment.appendChild(createAnimeCard(item));", self.html)
    def test_favorite_grid_uses_shared_commit_and_initializes_icons(self):
        start = self.html.index("function renderFavGrid()")
        end = self.html.index("// ================= 新增：缓存管理逻辑", start)
        source = self.html[start:end]

        self.assertIn("document.createDocumentFragment()", source)
        self.assertIn("fragment.appendChild(createAnimeCard(item", source)
        self.assertIn("replaceGridCards(grid, fragment)", source)
        self.assertNotIn("grid.appendChild(createAnimeCard", source)
        self.assertIn("const favUrl = item.url || ''", source)

    def test_card_grid_uses_smooth_replacement(self):
        self.assertIn("function replaceGridCards(grid, fragment)", self.cards_js)
        self.assertIn("grid.classList.add('is-updating', 'is-fading-out')", self.cards_js)
        self.assertIn("grid.replaceChildren(fragment)", self.cards_js)
        self.assertIn("grid.classList.remove('is-updating', 'is-fading-in')", self.cards_js)
        self.assertIn(".card-grid.is-updating", self.css)
        self.assertIn(".card-grid.is-fading-out", self.css)
        self.assertIn(".card-grid.is-fading-in", self.css)
        self.assertIn("@keyframes card-grid-fade-in", self.css)
        self.assertIn("prefers-reduced-motion: reduce", self.css)
        self.assertIn("grid._replaceToken", self.cards_js)
        self.assertEqual(self.html.count("replaceGridCards(grid, fragment);"), 5)

    def test_covers_use_local_fallback_instead_of_external_placeholder(self):
        self.assertNotIn("via.placeholder.com", self.html + self.cards_js)
        self.assertIn("const LOCAL_COVER_PLACEHOLDER", self.cards_js)
        self.assertIn("function handleCoverError(img)", self.cards_js)
        self.assertIn('onerror="handleCoverError(this)"', self.cards_js)
        self.assertIn("coverSrc(img)", self.cards_js)

    def test_download_modal_opens_before_async_search_and_caches_results(self):
        self.assertIn("openDownloadModal(request.keyword)", self.downloads_js)
        self.assertIn("renderTorrentState('searching'", self.downloads_js)
        self.assertIn("const torrentSearchCache = new Map()", self.downloads_js)
        self.assertIn("const TORRENT_CACHE_TTL = 60 * 1000", self.downloads_js)
        self.assertIn("getCachedTorrentResults(request)", self.downloads_js)
        self.assertIn("pywebview.api.search_torrents(request)", self.downloads_js)
        self.assertIn("function handleSubjectSearch(subjectId, keyword)", self.downloads_js)
        self.assertIn("if (token !== torrentSearchToken) return", self.downloads_js)
        self.assertIn("res.status === 'empty'", self.downloads_js)

    def test_rss_subscriptions_use_independent_page(self):
        self.assertIn('id="rssBtn" title="RSS 订阅"', self.html)
        self.assertIn('data-lucide="rss"', self.html)
        self.assertIn('id="rssView"', self.html)
        self.assertIn('id="calendarView"', self.html)
        self.assertIn('id="rssEditorModal"', self.html)
        self.assertIn('function openRssView()', self.rss_js)
        self.assertIn('pywebview.api.get_rss_subscriptions()', self.rss_js)
        self.assertIn('pywebview.api.check_rss_subscription(subscriptionId)', self.rss_js)
        self.assertIn('escapeHtml(subscription.name)', self.rss_js)
        self.assertIn('.rss-layout', self.css)
        self.assertIn('onclick="startRssDraftFromDownload()"', self.html)
        self.assertIn("openRssEditor(null, keyword, aliases)", self.rss_js)
        self.assertIn("document.getElementById('rss_edit_keyword').value = sub?.keyword || draftKeyword || '';", self.rss_js)
        self.assertIn("document.getElementById('rss_edit_aliases').value = sub?.search_aliases || aliasText;", self.rss_js)
        self.assertIn("document.getElementById('rss_edit_group').value = sub?.group_filter || '';", self.rss_js)
        self.assertIn('pywebview.api.preview_rss_subscription(payload)', self.rss_js)
        self.assertIn('function renderRssPreview(response)', self.rss_js)
        self.assertIn('id="rss_edit_aliases"', self.html)
        self.assertIn('pywebview.api.get_torrent_search_keywords(context.request)', self.rss_js)
        self.assertIn('.rss-preview-pane', self.css)

    def test_rss_current_tasks_persist_and_support_batch_push(self):
        self.assertIn('pywebview.api.get_rss_current_tasks(null, 1000)', self.rss_js)
        self.assertIn('function groupRssTasks(subscriptionId)', self.rss_js)
        self.assertIn('function renderRssTaskSection(subscriptionId)', self.rss_js)
        self.assertIn('function selectBestRssTasks(subscriptionId)', self.rss_js)
        self.assertIn('pywebview.api.push_rss_tasks(subscriptionId, taskIds)', self.rss_js)
        self.assertIn('rssRuleFeedback.set(id', self.rss_js)
        self.assertIn('.rss-task-section', self.css)
        self.assertIn('.rss-task-variants', self.css)

    def test_rss_auto_check_interval_ui(self):
        self.assertIn('id="rssCheckInterval"', self.html)
        self.assertIn('onchange="setRssCheckInterval(this.value)"', self.html)
        self.assertIn('pywebview.api.get_rss_check_interval()', self.rss_js)
        self.assertIn('pywebview.api.set_rss_check_interval(value)', self.rss_js)
        self.assertIn('window.setRssCheckInterval = setRssCheckInterval', self.rss_js)
        self.assertIn('.rss-interval-select', self.css)

    def test_rss_delete_uses_custom_confirm_dialog(self):
        self.assertIn('id="confirmModal"', self.html)
        self.assertIn("function showConfirmDialog(options)", self.html)
        self.assertIn("function confirmModalOk()", self.html)
        self.assertIn("showConfirmDialog({", self.rss_js)
        self.assertIn(".btn-danger", self.css)
        self.assertNotIn("confirm('删除这个 RSS 订阅", self.rss_js)

    def test_index_loads_split_frontend_assets(self):
        self.assertIn('<link rel="stylesheet" href="static/css/app.css">', self.html)
        self.assertIn('<script src="static/js/lucide.min.js" defer></script>', self.html)
        self.assertIn('<script src="static/js/cards.js"></script>', self.html)
        self.assertIn('<script src="static/js/downloads.js"></script>', self.html)
        self.assertIn('<script src="static/js/rss.js"></script>', self.html)


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
