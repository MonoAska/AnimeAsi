"""
本地动画管理模块：扫描文件夹、解析集数、系统播放、观看记录
"""

import os
import re
import logging
import subprocess

WATCH_HISTORY_FILE = "watch_history.json"  # 迁移用

# 支持的主流视频格式
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"}


def _parse_episode(filename: str) -> int | None:
    """从视频文件名中提取集数，返回整数或 None"""
    name = os.path.splitext(filename)[0]

    patterns = [
        r"[Ee][Pp]\s*(\d+)",           # EP01, Ep 12
        r"第\s*(\d+)\s*[话集話]",      # 第01话, 第 12 集
        r"S\d+\s*[Ee]\s*(\d+)",        # S01E01, S1 E12
        r"\[(\d+)]",                    # [01]
        r"[\s\.\-_](\d+)[\s\.\-_]",    # 01, 被分隔符包围的数字
        r"^(\d+)",                      # 文件名开头的数字
        r"[\.\-_]\s*(\d+)$",           # 文件名结尾的数字（允许空格间隔）
        r"\s(\d{1,3})$",                # 空格+1~3位数字结尾（如 "芙莉莲 - 01"）
    ]

    for pattern in patterns:
        match = re.search(pattern, name)
        if match:
            num = int(match.group(1))
            if 1 <= num <= 9999:
                return num

    return None


def scan_local_episodes(anime_name: str, root_dir: str, db) -> list[dict]:
    if not root_dir or not os.path.isdir(root_dir):
        logging.warning("scan_local_episodes: root_dir does not exist: %s", root_dir)
        return []

    matched_dir = None
    try:
        for entry in os.listdir(root_dir):
            entry_path = os.path.join(root_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            if anime_name.lower() in entry.lower():
                matched_dir = entry_path
                break
    except PermissionError as e:
        logging.error("scan_local_episodes: permission denied on %s: %s", root_dir, e)
        return []

    if not matched_dir:
        return []

    episodes = []
    try:
        for f in os.listdir(matched_dir):
            ext = os.path.splitext(f)[1].lower()
            if ext not in VIDEO_EXTENSIONS:
                continue
            ep_num = _parse_episode(f)
            if ep_num is None:
                continue
            episodes.append({
                "episode": ep_num,
                "file": f,
                "path": os.path.join(matched_dir, f).replace("\\", "/"),
            })
    except PermissionError as e:
        logging.error("scan_local_episodes: permission denied reading %s: %s", matched_dir, e)
        return []

    episodes.sort(key=lambda x: x["episode"])
    history = db.get_watch_history()
    anime_history = history.get(anime_name, {})

    for ep in episodes:
        ep_key = str(ep["episode"])
        ep["watched"] = anime_history.get(ep_key, {}).get("watched", False)

    return episodes


def get_anime_episodes(anime_name: str, root_dir: str, db) -> dict:
    episodes = scan_local_episodes(anime_name, root_dir, db)
    return {"anime_name": anime_name, "episodes": episodes}


def play_episode(anime_name: str, episode_num: int, file_path: str, db) -> dict:
    if not os.path.exists(file_path):
        return {"status": "error", "message": f"文件不存在: {file_path}"}

    try:
        if os.name == "nt":
            os.startfile(file_path)
        elif os.name == "darwin":
            subprocess.Popen(["open", file_path])
        else:
            subprocess.Popen(["xdg-open", file_path])
    except Exception as e:
        logging.error("play_episode: failed to open %s: %s", file_path, e)
        return {"status": "error", "message": str(e)}

    db.mark_watched(anime_name, episode_num)
    return {"status": "success"}


def get_watch_history(db) -> dict:
    return db.get_watch_history()
