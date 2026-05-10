# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AnimeAsi — 全能番剧中枢，pywebview 桌面应用。Python 后端提供本地 HTTP 服务 + JS API bridge，前端单页 HTML 渲染。

## Build & Run

```bash
# 开发运行
cd E:/CC/test && source venv/Scripts/activate && python main.py

# 打包单文件 exe
cd E:/CC/test && source venv/Scripts/activate && pyinstaller build.spec
# 输出: dist/AnimeAsi.exe (~14MB)
```

## Architecture

### Backend (Python)

| 文件 | 职责 |
|------|------|
| `main.py` | 入口，Bottle HTTP server，pywebview 窗口，AnimeProAPI（JS API bridge） |
| `database.py` | SQLite 数据库：日历/标签/收藏/观看记录，迁移旧 JSON |
| `downloader.py` | 多站点 RSS 种子搜索 + qBittorrent 推送 |
| `local_manager.py` | 本地动画文件扫描、集数解析、系统播放 |

路径约定：
- `EXE_DIR` = exe 所在目录（存放 config.json / favorites.json 等数据文件）
- `RUNTIME_DIR` = 开发时同 EXE_DIR，打包后为 PyInstaller 临时解压目录（存放 web 前端资源）
- `main.py` 启动时 `os.chdir(EXE_DIR)`，后续所有数据文件都写在 exe 旁边

### Frontend

- `WEB/index.html` — 单页应用，内含全部 CSS + JS
- `WEB/static/js/lucide.min.js` — 图标库（defer 加载）
- 纯内联 CSS（CSS 变量实现 dark/light 主题切换）
- 无前端框架，无构建步骤

### Data Files (auto-generated in EXE_DIR)

- `animeasi.db` — SQLite 数据库（日历/标签/收藏/观看记录）
- `config.json` — 用户配置（唯一保留的 JSON）
- `favorites.json` / `bgm_cache.json` / `watch_history.json` — 旧 JSON 文件，仅首次启动迁移用，之后不再读写
- `cache_covers/` — 封面图片缓存
- `error.log` — 错误日志

## Key Patterns

- **JS API bridge**: pywebview 自动将 Python 类方法暴露为 `pywebview.api.method_name()` 供前端调用
- **Bottle routes**: `@server.route()` 定义 API 端点，同时通过 `@server.route('/<filepath:path>')` 提供静态文件服务
- **内联 onclick**: 前端事件用 `onclick` 属性（非事件监听），函数必须是 **全局作用域**
- **escAttr**: 在 onclick 中嵌入用户数据（番剧名/URL 等）时，必须用 `escAttr()` 转义。该函数会转义 `\` `'` `"` 三个字符，防止 XSS。注意：`encodeURIComponent` 不编码单引号，不可用于此场景。

## Packaging Notes

- 使用 `build.spec`（PyInstaller spec 文件）
- 需要 `hiddenimports` 包含 `pycparser`、`cffi`（pythonnet/CLR 依赖）
- `pycparser` 不能出现在 `excludes` 中（cffi 运行时需要）
- 数据目录 `WEB/` 映射为 `web/`（注意大小写）
- 依赖: pywebview, bottle, requests, feedparser, qbittorrent-api, pycparser
- `database.py` 是自定义模块，需随 exe 一起打包（非 pip 依赖）
- 目标机器需安装 WebView2 Runtime（Windows 10 可能需要手动安装，Win11 自带）
