from __future__ import annotations

import queue
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from bili_dm_service import Store, deliver_comment_once, is_review_request, public_comment_body
from bilibili_tool import assess_description, animation_content_mismatch, collection_candidate_budget, collection_success_count, excluded_tag_hit, extract_bgm_name, should_deliver_public_comment, fetch_music_detail, merge_subtitle_candidates, ordered_subtitle_candidates, page_from_url, subtitle_is_ai, summarize, visual_detail_budget, visual_summary_is_heavy
from media_understanding import music_lyrics_analysis, visual_duration_factor, visual_frame_count
import qq_link_watcher
from qq_link_watcher import is_link_only_message, is_video_status_query, mark_reviewed, parse_configured_search_command, parse_search_command, qq_collection_payload, recent_video_status_message, recover_queued_tasks, review_already_used, should_skip_known_video, summary_delivery_text, task_status_label, video_page, visual_evidence_from_summary


class CollectionCompletionTests(unittest.TestCase):
    def test_candidate_pool_scales_beyond_requested_success_count(self):
        config = {"candidate_pool_multiplier": 8, "max_candidate_pool": 60}
        self.assertEqual(collection_candidate_budget(2, config), 16)
        self.assertEqual(collection_candidate_budget(10, config), 60)

    def test_import_mode_requires_confirmed_imports(self):
        self.assertEqual(collection_success_count(accepted=5, imported=1, do_import=True), 1)
        self.assertEqual(collection_success_count(accepted=5, imported=1, do_import=False), 5)

    def test_mmd_tag_is_excluded(self):
        self.assertEqual(excluded_tag_hit({"tags": ["MMD", "动画"]}, ["MMD"]), "MMD")
        self.assertEqual(excluded_tag_hit({"tags": ["MMD动画"]}, ["MMD"]), "MMD动画")
        self.assertEqual(excluded_tag_hit({"tags": ["动画", "动画"]}, ["MMD"]), "")

    def test_public_comment_requires_manual_or_explicit_publish_flag(self):
        self.assertTrue(should_deliver_public_comment(True, False, False))
        self.assertTrue(should_deliver_public_comment(False, True, False))
        self.assertFalse(should_deliver_public_comment(False, False, False))
        self.assertFalse(should_deliver_public_comment(True, True, True))

    def test_animation_query_rejects_gameplay_hud(self):
        video = {
            "title": "【动画创作激励】燃尽 五",
            "visual_analysis": "左上角显示小地图和任务提示，右下角有技能栏，敌人标注 Lv.54，右上角有 UID。",
        }
        self.assertIn("gameplay HUD markers", animation_content_mismatch(video, "动画"))

    def test_animation_query_allows_non_gameplay_animation(self):
        video = {
            "title": "菲比专场，可爱到超标！！",
            "visual_analysis": "金发少女在海边奔跑，穿着水手服，画面有彩色故障效果。",
        }
        self.assertEqual(animation_content_mismatch(video, "动画"), "")


class CommentConfidenceTests(unittest.TestCase):
    def test_unclear_summary_is_private_only_and_never_posts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "tasks.db")
            bvid = "BV1xx411c7mD"
            store.upsert_task(bvid, f"https://www.bilibili.com/video/{bvid}/", "m1", "completed", memory_imported=1)
            unclear = "\u5df2\u83b7\u53d6\u8bc1\u636e\u6574\u7406 " + ("uncertain evidence " * 30)
            with mock.patch("bili_dm_service.config", return_value={"comment_delivery": {"enabled": True, "execute": True}}):
                result = deliver_comment_once(store, bvid, unclear, "title")
            self.assertFalse(result["attempted"])
            self.assertEqual(result["status"], "low_confidence_private_only")
            store.close()


class CommentFallbackTests(unittest.TestCase):
    def test_comment_fallback_strips_summary_prefix(self):
        summary = "一句话概括：这是一段具有具体画面和动作的游戏战斗演示。"
        self.assertEqual(public_comment_body(summary), "这是一段具有具体画面和动作的游戏战斗演示。")


class ReviewRoutingTests(unittest.TestCase):
    def test_feedback_phrases_request_detailed_review(self):
        for text in (
            "\u4f60\u53d1\u7684\u8fd9\u4e2a\u89c6\u9891\u6ca1\u770b\u61c2",
            "\u91cd\u65b0\u7ec6\u770b",
            "\u521a\u624d\u90a3\u4e2a\u89c6\u9891\u770b\u9519\u4e86",
            "\u8fd9\u4e2a\u89c6\u9891\u6f0f\u770b\u4e86\u4e00\u6bb5",
            "BV1xx411c7mD \u518d\u770b\u4e00\u904d",
        ):
            self.assertTrue(is_review_request(text), text)

    def test_ordinary_chat_does_not_request_review(self):
        for text in ("\u6211\u770b\u5b8c\u4e86", "\u8fd9\u4e2a\u89c6\u9891\u4e0d\u9519", "\u5148\u8fd9\u6837\u5427", "\u6536\u5230", "[image] \u518d\u770b\u4e00\u904d"):
            self.assertFalse(is_review_request(text), text)

    def test_page_number_is_preserved_from_url(self):
        url = "https://www.bilibili.com/video/BV1xx411c7mD/?p=3&spm=test"
        self.assertEqual(page_from_url(url), 3)
        self.assertEqual(video_page(url), 3)
        self.assertEqual(page_from_url("https://www.bilibili.com/video/BV1xx411c7mD/"), 1)


class QQTriggerPolicyTests(unittest.TestCase):
    def test_natural_search_is_disabled_by_default(self):
        policy = {"allow_natural_search": False}
        self.assertIsNone(parse_configured_search_command("\u5237\u4e00\u4e2a\u9e23\u6f6e\u4e8c\u521b", policy, 2, 5))
        self.assertIsNotNone(parse_configured_search_command("/\u5237\u4e00\u4e2a\u9e23\u6f6e\u4e8c\u521b", policy, 2, 5))

    def test_detailed_review_can_only_be_marked_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(qq_link_watcher, "REVIEWED_FILE", Path(tmp) / "reviews.json"), mock.patch.object(qq_link_watcher, "reviewed", set()):
                self.assertFalse(review_already_used("BV1xx411c7mD"))
                mark_reviewed("BV1xx411c7mD")
                self.assertTrue(review_already_used("BV1xx411c7mD"))

    def test_link_must_be_standalone(self):
        self.assertTrue(is_link_only_message("https://www.bilibili.com/video/BV1xx411c7mD/"))
        self.assertTrue(is_link_only_message("\u770b\u8fd9\u4e2a https://www.bilibili.com/video/BV1xx411c7mD/"))
        self.assertFalse(is_link_only_message("\u6211\u89c9\u5f97\u8fd9\u4e2a\u5f88\u597d\u7b11 https://www.bilibili.com/video/BV1xx411c7mD/"))

    def test_bilibili_share_title_and_mention_are_structural(self):
        message = "\u3010\u518d\u4e5f\u4e0d\u7528\u53bb\u536b\u751f\u95f4\u4e86-\u54d4\u54a9\u54d4\u54a9\u3011 https://b23.tv/Txz3s7O @\u6ee8\u6ee8\u7684\u5927\u80a5\u9c7c"
        self.assertTrue(is_link_only_message(message))
        self.assertTrue(is_link_only_message("https://b23.tv/Txz3s7O @\u6ee8\u6ee8\u7684\u5927\u80a5\u9c7c"))
        self.assertFalse(is_link_only_message("\u8fd9\u4e2a\u89c6\u9891\u5f88\u597d\u7b11 https://b23.tv/Txz3s7O"))

    def test_bilibili_share_route_queues_video(self):
        cfg = {"qq_link_policy": {"allowed_groups": ["900000001"], "require_link_only_message": True}}
        url = "https://www.bilibili.com/video/BV1xx411c7mD/"
        message = "\u3010\u6d4b\u8bd5\u6807\u9898-\u54d4\u54a9\u54d4\u54a9\u3011 " + url + " @\u6ee8\u6ee8\u7684\u5927\u80a5\u9c7c"
        with mock.patch.object(qq_link_watcher, "load_config", return_value=cfg), mock.patch.object(qq_link_watcher, "send_group_message"), mock.patch.object(qq_link_watcher, "enqueue") as enqueue:
            qq_link_watcher.handle_text(message, "900000001", "1000000002", "onebot")
        enqueue.assert_called_once_with(url, "900000001", "1000000002", "onebot", "", review_level="rough")


class QQSearchCommandTests(unittest.TestCase):
    def test_type_search_with_count(self):
        result = parse_search_command("/\u770b\u7c7b\u578b AI 3\u4e2a")
        self.assertEqual(result["query"], "AI")
        self.assertEqual(result["max_results"], 3)

    def test_natural_one_video_command(self):
        result = parse_search_command("\u5237\u4e00\u4e2a\u9e23\u6f6e\u4e8c\u521b")
        self.assertEqual(result["query"], "\u9e23\u6f6e\u4e8c\u521b")
        self.assertEqual(result["max_results"], 1)

    def test_title_search_uses_default_count(self):
        result = parse_search_command("\u641c\u89c6\u9891 \u62981\u4e07\u6b21\uff0c\u4f1a\u574f\u5417\uff1f")
        self.assertEqual(result["query"], "\u62981\u4e07\u6b21\uff0c\u4f1a\u574f\u5417\uff1f")
        self.assertEqual(result["max_results"], 2)

    def test_explicit_title_count_is_removed_from_query(self):
        result = parse_search_command("\u627e\u89c6\u9891 iPhone Duo \u6df1\u5ea6\u8bc4\u6d4b 2")
        self.assertEqual(result["query"], "iPhone Duo \u6df1\u5ea6\u8bc4\u6d4b")
        self.assertEqual(result["max_results"], 2)

    def test_ordinary_question_is_not_a_search_command(self):
        self.assertIsNone(parse_search_command("\u770b\u89c6\u9891\u5417"))
        self.assertIsNone(parse_search_command("\u666e\u901a\u804a\u5929"))


class QQSearchPermissionTests(unittest.TestCase):
    def test_any_member_in_allowed_group_can_search(self):
        cfg = {"qq_link_policy": {
            "allowed_groups": ["900000001"],
            "owner_qq": "1000000001",
            "control_commands_owner_only": True,
            "search_enabled": True,
            "search_allow_all_group_members": True,
            "search_default_max": 2,
            "search_max_results": 5,
        }}
        with mock.patch.object(qq_link_watcher, "load_config", return_value=cfg), mock.patch.object(qq_link_watcher, "send_group_message", return_value=True), mock.patch.object(qq_link_watcher, "enqueue_search") as enqueue:
            qq_link_watcher.handle_text("/\u770b\u7c7b\u578b AI 3\u4e2a", "900000001", "123456", "test")
        enqueue.assert_called_once_with("AI", 3, "900000001", "123456", "test", "")


class QQPrivateRoutingTests(unittest.TestCase):
    CFG = {"qq_link_policy": {
        "owner_qq": "1000000001",
        "search_default_max": 2,
        "search_max_results": 5,
    }}

    def test_owner_private_search_routes_reply_to_private_chat(self):
        with mock.patch.object(qq_link_watcher, "load_config", return_value=self.CFG), mock.patch.object(qq_link_watcher, "send_private_message", return_value=True), mock.patch.object(qq_link_watcher, "enqueue_search") as enqueue:
            qq_link_watcher.handle_private_text("/\u770b\u4e2a\u9e23\u6f6e\u4e8c\u521b", "1000000001", "test")
        enqueue.assert_called_once_with("\u9e23\u6f6e\u4e8c\u521b", 2, "", "1000000001", "test", "", private=True)

    def test_other_user_private_search_is_ignored(self):
        with mock.patch.object(qq_link_watcher, "load_config", return_value=self.CFG), mock.patch.object(qq_link_watcher, "send_private_message", return_value=True), mock.patch.object(qq_link_watcher, "enqueue_search") as enqueue:
            qq_link_watcher.handle_private_text("\u770b\u4e2a\u9e23\u6f6e\u4e8c\u521b", "999", "test")
        enqueue.assert_not_called()


class StoreMigrationTests(unittest.TestCase):
    def test_existing_database_gains_comment_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.db"
            db = sqlite3.connect(path)
            db.execute(
                """CREATE TABLE video_tasks (
                    bvid TEXT PRIMARY KEY, msg_key TEXT, url TEXT NOT NULL,
                    title TEXT, summary TEXT, one_line TEXT, status TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'picked',
                    memory_imported INTEGER NOT NULL DEFAULT 0,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    admin_task_id TEXT, error TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                )"""
            )
            db.commit()
            db.close()
            store = Store(path)
            columns = {row[1] for row in store.db.execute("PRAGMA table_info(video_tasks)")}
            self.assertTrue({"review_level", "private_mode", "requester_uid", "comment_status", "comment_rpid", "comment_verified", "comment_message", "comment_checked_at"}.issubset(columns))
            store.upsert_task("BV1xx411c7mD", "https://www.bilibili.com/video/BV1xx411c7mD/", "m1", "completed", memory_imported=1, comment_rpid=123, comment_verified=1)
            task = store.get_task("BV1xx411c7mD")
            self.assertEqual(task["comment_rpid"], 123)
            self.assertEqual(task["comment_verified"], 1)
            store.close()


class CommentIntegrationTests(unittest.TestCase):
    def test_comment_runs_only_after_memory_and_persists_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "tasks.db")
            bvid = "BV1xx411c7mD"
            store.upsert_task(bvid, f"https://www.bilibili.com/video/{bvid}/", "m1", "completed", memory_imported=1, summary="A concise factual summary of the video. " * 20, title="Test video")
            class FakeClient:
                def send_comment(self, *args, **kwargs):
                    self.args = args
                    return {"accepted": True, "rpid": 456, "status": "accepted", "code": 0, "message": "ok"}
                def verify_comment_visible(self, *args, **kwargs):
                    return {"verified": True, "status": "visible", "message": "ok"}
            with mock.patch("bili_dm_service.PUBLIC_COMMENT_DELIVERY_ENABLED", True), mock.patch("bili_dm_service.config", return_value={"comment_delivery": {"enabled": True, "execute": True, "verify_readback": True}}), mock.patch("bili_dm_service.humanize_public_comment", return_value="A natural summary comment."), mock.patch("bili_comment_delivery.BilibiliCommentClient", FakeClient), mock.patch("bili_dm_service.time.sleep", return_value=None):
                result = deliver_comment_once(store, bvid, "A concise factual summary of the video. " * 20, "Test video")
            self.assertTrue(result["accepted"])
            task = store.get_task(bvid)
            self.assertEqual(task["comment_rpid"], 456)
            self.assertEqual(task["comment_verified"], 1)
            store.close()

    def test_comment_does_not_run_without_confirmed_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "tasks.db")
            bvid = "BV1xx411c7mD"
            store.upsert_task(bvid, f"https://www.bilibili.com/video/{bvid}/", "m1", "completed", memory_imported=0)
            with mock.patch("bili_dm_service.PUBLIC_COMMENT_DELIVERY_ENABLED", True), mock.patch("bili_dm_service.config", return_value={"comment_delivery": {"enabled": True, "execute": True}}):
                result = deliver_comment_once(store, bvid, "summary", "title")
            self.assertFalse(result["attempted"])
            self.assertEqual(result["status"], "memory_not_confirmed")
            store.close()


class PublicCommentContentTests(unittest.TestCase):
    def test_public_comment_uses_only_video_summary(self):
        summary = (
            "# title\n"
            "\u94fe\u63a5\uff1ahttps://example.invalid/video\n"
            "\u6807\u7b7e\uff1a\u7535\u7ade\u3001\u6e38\u620f\u3001\u6392\u4f4d\n"
            "\u4f9d\u636e\u6807\u9898\u3001\u7b80\u4ecb\u548c\u753b\u9762\uff1a\u8fd9\u662f\u4e00\u6bb5\u5173\u4e8e\u89c6\u9891\u5185\u5bb9\u7684\u53ef\u9760\u603b\u7ed3\u3002\n"
            "\u5904\u7406\u8fdb\u5ea6\uff1a\u5df2\u5b8c\u6210\u91cd\u8bd5\u548c\u6821\u9a8c\u3002\n"
        )
        body = public_comment_body(summary)
        self.assertIn("\u8fd9\u662f\u4e00\u6bb5\u5173\u4e8e\u89c6\u9891\u5185\u5bb9\u7684\u53ef\u9760\u603b\u7ed3", body)
        self.assertNotIn("http", body)
        self.assertNotIn("\u5904\u7406\u8fdb\u5ea6", body)


class AdaptiveFrameCountTests(unittest.TestCase):
    MEDIA = {
        "rough_max_frames": 18,
        "detailed_max_frames": 60,
        "rough_subtitle_frames": 6,
        "detailed_subtitle_frames": 30,
    }

    def test_talking_video_with_subtitles_uses_fewer_frames(self):
        video = {"title": "technology review", "subtitle": "spoken transcript", "tags": []}
        self.assertEqual(visual_frame_count(video, self.MEDIA, "rough"), 6)
        self.assertEqual(visual_frame_count(video, self.MEDIA, "detailed"), 30)

    def test_no_subtitle_video_uses_dense_frames(self):
        video = {"title": "silent visual clip", "subtitle": "", "tags": []}
        self.assertEqual(visual_frame_count(video, self.MEDIA, "rough"), 18)
        self.assertEqual(visual_frame_count(video, self.MEDIA, "detailed"), 60)

    def test_game_video_uses_dense_frames_even_with_subtitles(self):
        video = {"title": "\u738b\u8005\u8363\u8000\u56fd\u699c\u51b2\u5206", "subtitle": "game commentary", "tags": ["\u6e38\u620f", "\u7535\u7ade"]}
        self.assertEqual(visual_frame_count(video, self.MEDIA, "rough"), 18)
        self.assertEqual(visual_frame_count(video, self.MEDIA, "detailed"), 60)

    def test_animation_video_uses_dense_frames_even_with_subtitles(self):
        video = {"title": "animation short", "subtitle": "dialogue", "tags": ["\u52a8\u753b", "\u624b\u4e66"]}
        self.assertEqual(visual_frame_count(video, self.MEDIA, "rough"), 18)
        self.assertEqual(visual_frame_count(video, self.MEDIA, "detailed"), 60)

    def test_visual_heavy_video_uses_dense_frames_even_with_lyrics(self):
        video = {"title": "Visual Test MV", "subtitle": "lyrics", "tags": ["MV"]}
        self.assertEqual(visual_frame_count(video, self.MEDIA, "rough"), 18)
        self.assertEqual(visual_frame_count(video, self.MEDIA, "detailed"), 60)


class DurationAdaptiveVisualTests(unittest.TestCase):
    MEDIA = {"rough_max_frames": 18, "detailed_max_frames": 60, "rough_subtitle_frames": 6, "detailed_subtitle_frames": 30}

    def test_duration_factor(self):
        self.assertEqual(visual_duration_factor(30), 0.7)
        self.assertEqual(visual_duration_factor(120), 1.0)
        self.assertEqual(visual_duration_factor(600), 1.35)
        self.assertEqual(visual_duration_factor(1200), 1.6)

    def test_short_talking_video_uses_fewer_frames(self):
        video = {"title": "short talk", "subtitle": "text", "tags": [], "duration_seconds": 30}
        self.assertEqual(visual_frame_count(video, self.MEDIA, "rough"), 4)
        self.assertEqual(visual_frame_count(video, self.MEDIA, "detailed"), 21)

    def test_long_visual_video_uses_more_frames(self):
        video = {"title": "long animation", "subtitle": "", "tags": ["动画"], "duration_seconds": 1200}
        self.assertEqual(visual_frame_count(video, self.MEDIA, "rough"), 29)
        self.assertEqual(visual_frame_count(video, self.MEDIA, "detailed"), 96)

    def test_visual_detail_budget_grows_with_duration(self):
        self.assertEqual(visual_detail_budget(30), 2)
        self.assertEqual(visual_detail_budget(120), 3)
        self.assertEqual(visual_detail_budget(600), 5)
        self.assertEqual(visual_detail_budget(1200), 6)


class LyricsPolicyTests(unittest.TestCase):
    def test_mv_and_music_analysis_allow_lyrics(self):
        self.assertTrue(music_lyrics_analysis({"title": "Rolling Girl MV", "tags": ["MV"], "description": ""}))
        self.assertTrue(music_lyrics_analysis({"title": "album review", "tags": ["\u97f3\u4e50\u8d4f\u6790", "\u4e50\u8bc4"], "description": ""}))

    def test_animation_and_game_do_not_enable_lyrics_mode(self):
        self.assertFalse(music_lyrics_analysis({"title": "Visual Test animation", "tags": ["\u624b\u4e66", "\u52a8\u753b"], "description": ""}))
        self.assertFalse(music_lyrics_analysis({"title": "game rank climb", "tags": ["\u6e38\u620f", "\u51b2\u699c"], "description": ""}))


class BgmMetadataTests(unittest.TestCase):
    def test_extracts_bgm_from_description(self):
        desc = "\u53c2\u8003\u89c6\u9891\nBGM\uff1arolling girl\n\u5176\u4ed6\u5185\u5bb9"
        self.assertEqual(extract_bgm_name({}, desc), "rolling girl")

    def test_extracts_api_music_title(self):
        self.assertEqual(extract_bgm_name({"music": {"title": "Test Song"}}, ""), "Test Song")

    def test_missing_bgm_is_empty(self):
        self.assertEqual(extract_bgm_name({}, "no music here"), "")


class SubtitlePriorityTests(unittest.TestCase):
    def test_embedded_subtitle_beats_ai_subtitle(self):
        candidates = [
            {"lan": "ai-zh", "lan_doc": "AI中文", "_subtitle_kind": "ai", "subtitle_url": "ai"},
            {"lan": "en-US", "lan_doc": "English", "_subtitle_kind": "embedded", "subtitle_url": "embedded"},
        ]
        ordered = ordered_subtitle_candidates(candidates)
        self.assertEqual(ordered[0]["subtitle_url"], "embedded")
        self.assertFalse(subtitle_is_ai(ordered[0]))
        self.assertTrue(subtitle_is_ai(ordered[1]))

    def test_ai_subtitle_is_used_when_no_embedded_exists(self):
        candidates = [
            {"lan": "ai-zh", "lan_doc": "AI中文", "_subtitle_kind": "ai", "subtitle_url": "ai"},
        ]
        ordered = ordered_subtitle_candidates(candidates)
        self.assertEqual(ordered[0]["subtitle_url"], "ai")

    def test_wbi_merge_keeps_embedded_version_of_same_subtitle(self):
        ai = {"lan": "ai-zh", "_subtitle_kind": "ai", "subtitle_url": "same-url"}
        embedded = {"lan": "zh-CN", "_subtitle_kind": "embedded", "subtitle_url": "same-url"}
        merged = merge_subtitle_candidates([ai], [embedded])
        self.assertEqual(len(merged), 1)
        self.assertFalse(subtitle_is_ai(merged[0]))

    def test_language_field_can_mark_ai_even_if_listed_as_subtitle(self):
        self.assertTrue(subtitle_is_ai({"lan": "ai-zh", "lan_doc": "中文", "_subtitle_kind": "embedded"}))
        self.assertFalse(subtitle_is_ai({"lan": "zh-CN", "lan_doc": "中文", "_subtitle_kind": "embedded"}))


class VisualSummaryBalanceTests(unittest.TestCase):
    def test_timestamp_heavy_summary_is_rejected(self):
        text = "[00:00] 画面中有角色 [00:05] 背景为粉色 [00:10] 左侧有头像 [00:15] 右侧有星星"
        self.assertTrue(visual_summary_is_heavy(text))

    def test_story_summary_is_not_flagged(self):
        text = "这是一段角色互动展示。画面先出现进度条，随后展示多个Q版角色。根据画面顺序推断，它在为新内容做预热。"
        self.assertFalse(visual_summary_is_heavy(text))


class DescriptionQualityTests(unittest.TestCase):
    def test_meaningful_description_is_kept(self):
        result = assess_description("\u672c\u671f\u8bb2\u89e3\u65b0\u624b\u5982\u4f55\u914d\u961f\u548c\u6253\u6cd5\uff0c\u5305\u542b\u5177\u4f53\u6b65\u9aa4\u548c\u5b9e\u6d4b\u6570\u636e\u3002", "\u6e38\u620f\u653b\u7565")
        self.assertTrue(result["meaningful"])
        self.assertIn("\u914d\u961f", result["text"])

    def test_hashtags_and_links_only_are_meaningless(self):
        result = assess_description("#\u9e23\u6f6e# #\u4e8c\u521b# https://example.com #\u6e38\u620f#", "\u9e23\u6f6e\u4e8c\u521b")
        self.assertFalse(result["meaningful"])
        self.assertEqual(result["reason"], "boilerplate_only")

    def test_follow_and_copyright_boilerplate_is_meaningless(self):
        result = assess_description("\u611f\u8c22\u89c2\u770b\uff0c\u70b9\u8d5e\u5173\u6ce8\u4e00\u952e\u4e09\u8fde\uff0c\u672a\u7ecf\u6388\u6743\u7981\u6b62\u8f6c\u8f7d\uff0c\u4fb5\u5220\u3002", "\u6d4b\u8bd5")
        self.assertFalse(result["meaningful"])
        self.assertEqual(result["reason"], "boilerplate_only")

    def test_description_repeating_title_is_meaningless(self):
        result = assess_description("\u6e38\u620f\u653b\u7565", "\u6e38\u620f\u653b\u7565")
        self.assertFalse(result["meaningful"])
        self.assertEqual(result["reason"], "repeats_title")

    def test_bgm_only_description_is_not_used_as_content(self):
        result = assess_description("BGM\uff1arolling girl", "\u6d4b\u8bd5\u89c6\u9891")
        self.assertFalse(result["meaningful"])


class MusicDetailTests(unittest.TestCase):
    def test_music_detail_returns_only_title_and_original_artist(self):
        class Response:
            def json(self):
                return {
                    "code": 0,
                    "data": {
                        "music_title": "\u30ed\u30fc\u30ea\u30f3\u30ac\u30fc\u30eb",
                        "origin_artist": "wowaka,\u521d\u97f3\u672a\u6765",
                        "music_comment": {"nums": 999},
                        "music_rank": 1,
                    },
                }
        class Request:
            def get(self, *args, **kwargs):
                return Response()
        class Context:
            request = Request()
        result = fetch_music_detail(Context(), "MA406623164408320176", 1, 2, "BV1xx411c7mD")
        self.assertEqual(result.get("title"), "\u30ed\u30fc\u30ea\u30f3\u30ac\u30fc\u30eb")
        self.assertEqual(result.get("artist"), "wowaka\u3001\u521d\u97f3\u672a\u6765")
        self.assertNotIn("rating", result)
        self.assertNotIn("comments", result)


class QQVisualDeliveryTests(unittest.TestCase):
    def test_visual_evidence_is_extracted_from_summary(self):
        summary = "\u6807\u9898\uff1a\u6d4b\u8bd5\n\n- \u753b\u9762\u63cf\u8ff0\uff1a\u753b\u9762\u4e2d\u6709\u8fdb\u5ea6\u6761\u3001Q\u7248\u89d2\u8272\u548c\u50cf\u7d20\u5934\u50cf\u3002\n"
        evidence = visual_evidence_from_summary(summary)
        self.assertIn("\u8fdb\u5ea6\u6761", evidence)
        self.assertIn("Q\u7248\u89d2\u8272", evidence)

    def test_delivery_text_contains_visual_confirmation(self):
        summary = "\u6807\u9898\uff1a\u6d4b\u8bd5\n\n\u753b\u9762\u63cf\u8ff0\uff1a\u753b\u9762\u4e2d\u6709\u8fdb\u5ea6\u6761\u548cQ\u7248\u89d2\u8272\u3002"
        text = summary_delivery_text(summary, "\u6d4b\u8bd5\u6458\u8981", "BV1xx411c7mD")
        self.assertIn("\u753b\u9762\u786e\u8ba4", text)
        self.assertIn("\u8fdb\u5ea6\u6761", text)

    def test_missing_visual_is_reported_honestly(self):
        text = summary_delivery_text("\u6807\u9898\uff1a\u6d4b\u8bd5\n\n\u753b\u9762\u63cf\u8ff0\uff1a\u672a\u63d0\u4f9b\u3002", "\u6458\u8981", "BV1xx411c7mD")
        self.assertIn("\u6ca1\u6709\u53d6\u5230\u53ef\u7528\u7684\u753b\u9762\u8bc1\u636e", text)

    def test_story_section_with_visual_heading_is_used_as_evidence(self):
        summary = "\u6807\u9898\uff1a\u6d4b\u8bd5\n\n**\u753b\u9762\u4e0e\u6545\u4e8b\uff1a** \u753b\u9762\u4e2d\u9ec4\u8272\u89d2\u8272\u7a7f\u767d\u978b\uff0c\u978b\u5e95\u63a5\u7740\u900f\u660e\u8f6f\u7ba1\u3002"
        text = summary_delivery_text(summary, "\u6d4b\u8bd5\u6458\u8981", "BV1xx411c7mD")
        self.assertIn("\u753b\u9762\u786e\u8ba4", text)
        self.assertIn("\u9ec4\u8272\u89d2\u8272", text)
        self.assertNotIn("\u753b\u9762\u4e0e\u6545\u4e8b\uff1a**", text)

    def test_uncertain_visual_line_is_not_claimed_as_evidence(self):
        summary = "\u6807\u9898\uff1a\u6d4b\u8bd5\n\n**\u4e0d\u786e\u5b9a\u70b9\uff1a** \u89c6\u89c9\u5206\u6790\u5931\u8d25\uff0c\u65e0\u6cd5\u786e\u8ba4\u753b\u9762\u3002"
        self.assertEqual(visual_evidence_from_summary(summary), "")


class QQVideoStatusTests(unittest.TestCase):
    def test_detects_watch_status_questions(self):
        self.assertTrue(is_video_status_query("刚才那个视频看完了吗"))
        self.assertTrue(is_video_status_query("/视频状态"))
        self.assertTrue(is_video_status_query("\u4f60\u73b0\u5728\u5728\u5e72\u5417"))
        self.assertTrue(is_video_status_query("\u5fd9\u4e0d\u5fd9"))
        self.assertFalse(is_video_status_query("你好呀"))

    def test_status_labels_do_not_claim_unconfirmed_memory(self):
        self.assertEqual(task_status_label({"status": "completed", "memory_imported": 1}), "已看完并写入记忆")
        self.assertEqual(task_status_label({"status": "completed", "memory_imported": 0}), "处理完成，但记忆未确认")
        self.assertEqual(task_status_label({"status": "queued"}), "处理中")
        self.assertEqual(task_status_label({"status": "interrupted"}), "上次处理中断")

    def test_recent_queued_task_is_requeued_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "tasks.db")
            bvid = "BV1xx411c7mD"
            store.upsert_task(
                bvid,
                f"https://www.bilibili.com/video/{bvid}/",
                "m1",
                "queued",
                source="qq",
                delivery_target="900000001",
                source_message_key="m1",
                review_level="detailed",
                private_mode=0,
                requester_uid="1000000002",
                admin_task_id="task-existing",
            )
            task = store.get_task(bvid)
            work = queue.Queue()
            cfg = {"qq_link_policy": {"enabled": True, "queue_recovery_max_age_seconds": 60}, "bili_dm": {"video_timeout_seconds": 30}}
            with mock.patch.object(qq_link_watcher, "load_config", return_value=cfg), mock.patch.object(qq_link_watcher, "work_queue", work), mock.patch.object(qq_link_watcher, "log"):
                result = recover_queued_tasks(store, now=task["updated_at"] + 5, max_age_seconds=60)
            self.assertEqual(result, {"recovered": 1, "interrupted": 0})
            item = work.get_nowait()
            self.assertEqual(item["url"], f"https://www.bilibili.com/video/{bvid}/")
            self.assertEqual(item["group_id"], "900000001")
            self.assertEqual(item["user_id"], "1000000002")
            self.assertEqual(item["review_level"], "detailed")
            self.assertEqual(item["admin_task_id"], "task-existing")
            self.assertFalse(item["private"])
            self.assertTrue(item["recovered"])
            self.assertEqual(store.get_task(bvid)["status"], "queued")
            store.close()

    def test_recovered_task_reattaches_without_starting_duplicate_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "tasks.db")
            bvid = "BV1xx411c7mD"
            store.upsert_task(
                bvid,
                f"https://www.bilibili.com/video/{bvid}/",
                "m1",
                "queued",
                source="qq",
                delivery_target="900000001",
                source_message_key="m1",
                review_level="detailed",
                private_mode=0,
                requester_uid="1000000002",
                admin_task_id="task-existing",
            )
            class Response:
                ok = True
                def json(self):
                    return {"status": "completed", "log": "导入 1 个"}
            item = {
                "url": f"https://www.bilibili.com/video/{bvid}/",
                "group_id": "900000001",
                "user_id": "1000000002",
                "source": "qq",
                "message_key": "m1",
                "review_level": "detailed",
                "private": False,
                "recovered": True,
                "admin_task_id": "task-existing",
            }
            cfg = {"qq_link_policy": {"enabled": True}, "bili_dm": {"video_timeout_seconds": 30}}
            with (
                mock.patch.object(qq_link_watcher, "load_config", return_value=cfg),
                mock.patch.object(qq_link_watcher, "get_task_store", return_value=store),
                mock.patch.object(qq_link_watcher.requests, "get", return_value=Response()) as get,
                mock.patch.object(qq_link_watcher.requests, "post") as post,
                mock.patch.object(qq_link_watcher, "send_item_message", return_value=True),
                mock.patch.object(qq_link_watcher, "mark_seen"),
                mock.patch.object(qq_link_watcher, "log"),
                mock.patch.object(qq_link_watcher, "seen", set()),
            ):
                qq_link_watcher.process_url(item)
            task = store.get_task(bvid)
            store.close()
            get.assert_any_call("http://127.0.0.1:8765/api/tasks/task-existing", timeout=20)
            self.assertEqual(get.call_count, 2)
            post.assert_not_called()
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["admin_task_id"], "task-existing")
            self.assertEqual(task["memory_imported"], 1)

    def test_expired_queued_task_is_interrupted_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "tasks.db")
            bvid = "BV1xx411c7mD"
            store.upsert_task(
                bvid,
                f"https://www.bilibili.com/video/{bvid}/",
                "m1",
                "queued",
                source="qq",
                delivery_target="900000001",
                requester_uid="1000000002",
            )
            task = store.get_task(bvid)
            work = queue.Queue()
            cfg = {"qq_link_policy": {"enabled": True, "queue_recovery_max_age_seconds": 60}, "bili_dm": {"video_timeout_seconds": 30}}
            with mock.patch.object(qq_link_watcher, "load_config", return_value=cfg), mock.patch.object(qq_link_watcher, "work_queue", work), mock.patch.object(qq_link_watcher, "log"):
                result = recover_queued_tasks(store, now=task["updated_at"] + 61, max_age_seconds=60)
            self.assertEqual(result, {"recovered": 0, "interrupted": 1})
            self.assertTrue(work.empty())
            interrupted = store.get_task(bvid)
            self.assertEqual(interrupted["status"], "interrupted")
            self.assertIn("expired", interrupted["error"])
            store.close()

    def test_recent_status_reads_group_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "tasks.db")
            bvid = "BV1xx411c7mD"
            store.upsert_task(bvid, f"https://www.bilibili.com/video/{bvid}/", "m1", "completed", delivery_target="900000001", title="Test video", memory_imported=1)
            with mock.patch("qq_link_watcher.get_task_store", return_value=store):
                message = recent_video_status_message("900000001")
            self.assertIn(bvid, message)
            self.assertIn("已看完并写入记忆", message)
            store.close()


    def test_no_task_status_is_explicitly_online_and_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "tasks.db")
            with mock.patch("qq_link_watcher.get_task_store", return_value=store):
                message = recent_video_status_message("900000001")
            self.assertIn("\u5728\u7ebf", message)
            self.assertIn("\u6ca1\u6709\u6b63\u5728\u5904\u7406", message)
            store.close()

class QQVideoDedupeTests(unittest.TestCase):
    def test_known_video_is_skipped_for_normal_processing(self):
        self.assertTrue(should_skip_known_video("BV1xx411c7mD", "rough", {"BV1xx411c7mD"}))

    def test_known_video_can_be_reprocessed_for_detailed_review(self):
        self.assertFalse(should_skip_known_video("BV1xx411c7mD", "detailed", {"BV1xx411c7mD"}))

    def test_new_video_is_processed(self):
        self.assertFalse(should_skip_known_video("BV1xx411c7mD", "rough", set()))


class QQVideoDeliveryPolicyTests(unittest.TestCase):
    def test_qq_linked_video_suppresses_public_bilibili_comment(self):
        payload = qq_collection_payload("https://www.bilibili.com/video/BV1xx411c7mD/", "rough")
        self.assertTrue(payload["suppress_comment"])
        self.assertTrue(payload["import"])




class SummaryFallbackTests(unittest.TestCase):
    def test_fallback_preserves_visual_and_subtitle_as_separate_evidence(self):
        class FakeResponse:
            ok = True
            status = 200
            def json(self):
                return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        video = {
            "title": "animation test",
            "owner": "uploader",
            "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
            "description": "description marker",
            "subtitle": "SUBTITLE_MARKER",
            "audio_transcript": "AUDIO_MARKER",
            "visual_analysis": "VISUAL_MARKER",
            "comments": ["COMMENT_MARKER"],
            "tags": ["MV"],
        }
        with mock.patch("bilibili_tool.parse_deepseek_provider", return_value=("https://api.example", "key")), mock.patch("bilibili_tool.requests.post", return_value=FakeResponse()), mock.patch("bilibili_tool.log"):
            result = summarize(video, {"summary_model": "deepseek-flash"})
        self.assertIn("VISUAL_MARKER", result)
        self.assertIn("SUBTITLE_MARKER", result)
        self.assertIn("AUDIO_MARKER", result)
        self.assertLess(result.index("VISUAL_MARKER"), result.index("SUBTITLE_MARKER"))

    def test_summary_keeps_bgm_name_without_music_rating_or_music_id(self):
        captured = []
        class FakeResponse:
            ok = True
            status = 200
            def json(self):
                return {"choices": [{"message": {"content": "TestBot??" * 80}, "finish_reason": "stop"}]}
        def fake_post(url, **kwargs):
            captured.append(kwargs.get("json") or {})
            return FakeResponse()
        video = {
            "title": "rolling girl test",
            "owner": "uploader",
            "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
            "description": "",
            "subtitle": "",
            "audio_transcript": "",
            "visual_analysis": "",
            "comments": [],
            "tags": ["MV"],
            "bgm": "\u30ed\u30fc\u30ea\u30f3\u30ac\u30fc\u30eb",
            "bgm_id": "MA406623164408320176",
            "bgm_original_artist": "wowaka、初音未来",
        }
        with mock.patch("bilibili_tool.parse_deepseek_provider", return_value=("https://api.example", "key")), mock.patch("bilibili_tool.requests.post", side_effect=fake_post), mock.patch("bilibili_tool.log"):
            summarize(video, {"summary_model": "deepseek-flash"})
        prompt = captured[0]["messages"][-1]["content"]
        self.assertIn("\u30ed\u30fc\u30ea\u30f3\u30ac\u30fc\u30eb", prompt)
        self.assertNotIn("MA406623164408320176", prompt)
        self.assertIn("\u4e0d\u9700\u8981\u97f3\u4e50\u8bc4\u5206\u6216\u97f3\u4e50\u8bc4\u8bba", prompt)


        self.assertIn("250 \u5230 450 \u5b57", prompt)
        self.assertIn("\u8fde\u7eed\u753b\u9762", prompt)
        self.assertIn("\u636e\u6b64\u63a8\u65ad", prompt)
        self.assertIn("\u6700\u591a\u4fdd\u7559 3 \u4e2a\u771f\u6b63\u6709\u7528\u7684\u89c6\u89c9\u7ec6\u8282", prompt)
        self.assertNotIn("500 \u5230 1200 \u5b57", prompt)

if __name__ == "__main__":
    unittest.main()
