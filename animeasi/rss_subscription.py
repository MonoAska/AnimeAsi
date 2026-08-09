"""RSS subscription rules and download-plan checks."""

import os
import re
from datetime import datetime

from animeasi.downloads import downloader
from animeasi.subjects.aliases import select_search_keywords


_SPLIT_RE = re.compile(r"[,，\n]+")
_INVALID_PATH_CHARS = re.compile(r'[\\/:*?"<>|]+')


def sanitize_dir_name(name: str) -> str:
    safe = _INVALID_PATH_CHARS.sub("_", str(name or "")).strip(" ._")
    return safe or "Anime"


def split_terms_preserve_case(value) -> list[str]:
    if isinstance(value, list):
        values = value
    else:
        values = _SPLIT_RE.split(str(value or ""))
    return [str(v).strip() for v in values if str(v).strip()]


def split_terms(value) -> list[str]:
    return [value.lower() for value in split_terms_preserve_case(value)]


def subscription_search_keywords(subscription: dict) -> list[str]:
    return select_search_keywords(
        subscription.get("keyword", ""),
        split_terms_preserve_case(subscription.get("search_aliases")),
    )


def evaluate_result_rule(result: dict, subscription: dict) -> dict:
    title = str(result.get("title") or "").lower()
    include_terms = split_terms(subscription.get("include_keywords"))
    exclude_terms = split_terms(subscription.get("exclude_keywords"))
    group_terms = split_terms(subscription.get("group_filter"))
    quality_terms = split_terms(subscription.get("quality_filter"))

    reasons = []
    missing_includes = [term for term in include_terms if term not in title]
    if missing_includes:
        reasons.append(f"缺少必须包含：{', '.join(missing_includes)}")
    if group_terms and not any(term in title for term in group_terms):
        reasons.append(f"字幕组不匹配：{', '.join(group_terms)}")
    if quality_terms and not any(term in title for term in quality_terms):
        reasons.append(f"清晰度不匹配：{', '.join(quality_terms)}")
    excluded = [term for term in exclude_terms if term in title]
    if excluded:
        reasons.append(f"命中排除词：{', '.join(excluded)}")
    return {"matched": not reasons, "reasons": reasons}


def result_matches_rule(result: dict, subscription: dict) -> bool:
    return evaluate_result_rule(result, subscription)["matched"]


def preview_subscription(api, subscription: dict) -> dict:
    keyword = str(subscription.get("keyword") or "").strip()
    if not keyword:
        return {"status": "error", "message": "请填写搜索关键词", "results": []}

    keywords = subscription_search_keywords(subscription)
    status, results, source_stats = downloader.search_torrents(
        keywords,
        api.config.get("rss_sources", []),
        api._get_proxies(),
    )
    evaluated = []
    for result in results:
        evaluation = evaluate_result_rule(result, subscription)
        evaluated.append({**result, **evaluation})
    if status == "error":
        message = "RSS 源连接失败"
    elif status == "partial":
        failed = [stat for stat in source_stats if not stat["ok"]]
        message = "部分 RSS 源失败：" + "、".join(
            stat["name"] for stat in failed
        )
    else:
        message = ""
    return {
        "status": status,
        "message": message,
        "results": evaluated,
        "matched_count": sum(1 for item in evaluated if item["matched"]),
        "keywords": keywords,
        "source_stats": source_stats,
    }

def default_save_path(config: dict, subscription: dict) -> str:
    explicit_path = str(subscription.get("save_path") or "").strip()
    if explicit_path:
        return explicit_path
    root = str(config.get("local_anime_path") or "").strip()
    if not root:
        return ""
    folder_name = sanitize_dir_name(subscription.get("name") or subscription.get("keyword"))
    return os.path.join(root, folder_name)


def _push_result(api, subscription: dict, result: dict) -> tuple[str, str, str]:
    save_path = default_save_path(api.config, subscription)
    qbt_config = {
        "host": api.config.get("qbt_host"),
        "username": api.config.get("qbt_username", "admin"),
        "password": api.config.get("qbt_password"),
        "save_path": save_path,
    }
    status, message = downloader.push_to_qbittorrent(
        result.get("url", ""),
        qbt_config,
        auto_launch=api.config.get("qbt_auto_launch", True),
        qbt_exe_path=api.config.get("qbt_exe_path", ""),
    )
    return status, message, save_path


def check_subscription(api, subscription_id: int) -> dict:
    subscription = api.db.get_rss_subscription(subscription_id)
    if not subscription:
        return {"status": "error", "message": "订阅不存在"}
    if not subscription.get("enabled", True):
        return {"status": "disabled", "subscription": subscription, "results": []}

    keywords = subscription_search_keywords(subscription)
    status, results, source_stats = downloader.search_torrents(
        keywords, api.config.get("rss_sources", []), api._get_proxies(),
    )
    filtered = [result for result in results if result_matches_rule(result, subscription)]
    # 只有整轮检查健康（全部源成功或空）时才清理已消失的任务；
    # partial/error 说明有源失败，可能只是暂时取不到结果，误删会让用户漏掉资源。
    api.db.sync_rss_current_tasks(
        subscription_id,
        filtered,
        prune_missing=status in ("success", "empty"),
    )
    fresh = [
        result for result in filtered
        if not api.db.has_rss_download_record(
            subscription_id,
            result.get("url"),
            result.get("title"),
        )
    ]

    pushed = []
    skipped_existing = len(filtered) - len(fresh)
    push_error = ""
    if subscription.get("auto_push", False) and fresh:
        result = fresh[0]
        push_status, message, save_path = _push_result(api, subscription, result)
        api.db.record_rss_download(
            subscription_id,
            result,
            push_status,
            message,
            save_path,
        )
        if push_status == "success":
            pushed.append(result)
        else:
            push_error = message

    if status in ("success", "empty", "partial"):
        api.db.mark_rss_subscription_checked(subscription_id)
    return {
        "status": "success" if status in ("success", "empty") else status,
        "message": push_error,
        "subscription": api.db.get_rss_subscription(subscription_id),
        "results": filtered,
        "task_count": len(api.db.list_rss_current_tasks(subscription_id)),
        "fresh_count": len(fresh),
        "skipped_existing": skipped_existing,
        "pushed": pushed,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "keywords": keywords,
        "source_stats": source_stats,
    }


def push_tasks(api, subscription_id: int, task_ids) -> dict:
    subscription = api.db.get_rss_subscription(subscription_id)
    if not subscription:
        return {"status": "error", "message": "订阅不存在", "results": []}
    tasks = api.db.get_rss_current_tasks(subscription_id, task_ids or [])
    if not tasks:
        return {"status": "error", "message": "没有可推送的任务", "results": []}

    pushed, skipped, failed = [], [], []
    for task in tasks:
        task_id = task["id"]
        if task.get("status") == "success" or api.db.has_rss_download_record(
            subscription_id,
            task.get("url"),
            task.get("title"),
        ):
            skipped.append({"task_id": task_id, "status": "success", "message": "已下载"})
            api.db.update_rss_current_task(task_id, "success", "已下载")
            continue

        push_status, message, save_path = _push_result(api, subscription, task)
        api.db.record_rss_download(
            subscription_id,
            task,
            push_status,
            message,
            save_path,
        )
        item = {"task_id": task_id, "status": push_status, "message": message}
        (pushed if push_status == "success" else failed).append(item)

    if failed and not pushed:
        response_status = "error"
    elif failed:
        response_status = "partial"
    else:
        response_status = "success"
    return {
        "status": response_status, "message": failed[0]["message"] if failed else "",
        "pushed": pushed, "skipped": skipped, "failed": failed,
        "results": api.db.list_rss_current_tasks(subscription_id),
    }
