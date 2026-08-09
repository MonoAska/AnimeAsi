import os
import subprocess
import time
from typing import Optional


def find_qbt_exe(user_path: str = "") -> Optional[str]:
    """Find the qBittorrent executable path."""
    if user_path and os.path.isfile(user_path):
        return user_path

    candidates = [
        os.path.expandvars(r"%PROGRAMFILES%\qBittorrent\qbittorrent.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\qBittorrent\qbittorrent.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\qBittorrent\qbittorrent.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def try_qbt_connect(qbt_config: dict):
    """Try to connect to qBittorrent Web API.

    Return ``(client, error_message)``; on success ``error_message`` is empty.
    """
    import qbittorrentapi
    try:
        client = qbittorrentapi.Client(
            host=qbt_config.get("host", "127.0.0.1:8080"),
            username=qbt_config.get("username", "admin"),
            password=qbt_config.get("password", ""),
        )
        client.auth_log_in()
        return client, ""
    except qbittorrentapi.LoginFailed:
        return None, "qBittorrent 登录失败：WebUI 用户名或密码不正确。"
    except qbittorrentapi.APIConnectionError:
        return None, "无法连接 qBittorrent WebUI，请确认它正在运行且已开启 WebUI。"
    except Exception as e:
        return None, f"连接 qBittorrent 失败: {e}"


def push_to_qbittorrent(torrent_url: str, qbt_config: dict,
                        auto_launch: bool = False,
                        qbt_exe_path: str = "") -> tuple[str, str]:
    """
    Send a torrent/magnet URL to qBittorrent.
    If auto_launch is true and connection fails, try starting qBittorrent first.
    """
    import qbittorrentapi

    client, connect_error = try_qbt_connect(qbt_config)
    if client is not None:
        try:
            save_path = qbt_config.get("save_path", "")
            client.torrents_add(urls=torrent_url, save_path=save_path)
            return "success", "任务已成功添加到 qBittorrent！"
        except Exception as e:
            return "error", f"推送到下载器失败:\n{e}"

    if not auto_launch:
        return "error", (
            connect_error
            or "无法连接到 qBittorrent，请确保它正在运行且已开启 WebUI。"
        ) + "\n\n可在设置中开启「自动启动 qBittorrent」。"

    exe_path = find_qbt_exe(qbt_exe_path)
    if not exe_path:
        return "error", (
            "无法连接到 qBittorrent，且未找到 qbittorrent.exe。\n\n"
            "请在设置中手动指定 qBittorrent 的安装路径。"
        )

    try:
        subprocess.Popen(exe_path, creationflags=0x08000000 if os.name == 'nt' else 0)
    except Exception as e:
        return "error", f"启动 qBittorrent 失败:\n{e}"

    for _ in range(20):
        time.sleep(0.5)
        client, connect_error = try_qbt_connect(qbt_config)
        if client is not None:
            try:
                save_path = qbt_config.get("save_path", "")
                client.torrents_add(urls=torrent_url, save_path=save_path)
                return "success", "已自动启动 qBittorrent，任务添加成功！"
            except Exception as e:
                return "error", f"qBittorrent 已启动，但推送失败:\n{e}"

    return "error", (
        "qBittorrent 已启动，但 WebUI 在 10 秒内未就绪。"
        + (f"\n{connect_error}" if connect_error else "请检查 WebUI 设置。")
    )
