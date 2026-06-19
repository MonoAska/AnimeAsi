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

| 路径 | 职责 |
|------|------|
| `main.py` | 入口，Bottle HTTP server，pywebview 窗口，AnimeProAPI（JS API bridge） |
| `animeasi/database.py` | SQLite 数据库：日历/标签/收藏/观看记录，迁移旧 JSON |
| `animeasi/local_manager.py` | 本地动画文件扫描、集数解析、系统播放 |
| `animeasi/cache/cover_cache.py` | 封面缓存、缓存命中 URL 改写、后台下载 |
| `animeasi/downloads/` | 多站点 RSS 种子搜索、资源标签解析、去重、qBittorrent 推送 |
| `animeasi/season/browser.py` | 季度浏览、Bangumi 分页拉取、季度缓存、日漫主线过滤 |
| `animeasi/subjects/schema.py` | 统一前后端 subject/card 数据契约 |

路径约定：
- `EXE_DIR` = exe 所在目录（存放 config.json、animeasi.db、cache_covers/ 等用户数据）
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
- `cache_covers/` — 封面图片缓存
- `error.log` — 错误日志

## Key Patterns

- **JS API bridge**: pywebview 自动将 Python 类方法暴露为 `pywebview.api.method_name()` 供前端调用
- **Bottle routes**: `@server.route()` 定义 API 端点，同时通过 `@server.route('/<filepath:path>')` 提供静态文件服务
- **内联 onclick**: 前端事件用 `onclick` 属性（非事件监听），函数必须是 **全局作用域**
- **escAttr**: 在 onclick 中嵌入用户数据（番剧名/URL 等）时，必须用 `escAttr()` 转义。该函数会转义 `\` `'` `"` 三个字符，防止 XSS。注意：`encodeURIComponent` 不编码单引号，不可用于此场景。
- **统一卡片渲染**: 前端卡片统一走 `createAnimeCard(item, options)`。日历、季度、搜索、收藏不要再各写一份 `card.innerHTML`；收藏播放按钮通过 `playName` 选项保留。
- **统一 subject schema**: 后端返回给前端的番剧对象应先经过 `animeasi/subjects/schema.py` 归一化，优先提供 `display_name`、`images`、`air_date`、`rank` 等稳定字段。收藏接口可保留 `img` 兼容字段，但新逻辑应优先读 `images`。

### 日漫筛选机制

Bangumi 以日漫为主，**只有非日漫才会被打上产地标签**（如"国产""欧美""韩国"）。后端 `_classify_by_tags()` 检查标签集合是否与已知非日漫标签有交集，无产地标签视为日漫。不是靠分析番剧名或 IP 归属。

流程：
1. 启动时 `_preload_bgm()` 并行拉取所有日历条目标签（Bangumi `/v0/subjects/{id}` API），存入 `subject_tags_cache` 内存字典 + `subject_tags` 数据库表
2. `get_bgm_data()` 返回数据时，对每个 item 调用 `_classify_by_tags()` 设置 `is_japanese` 字段
3. 前端 `renderAnimeGrid()` 中 `isJapaneseAnime()` 读取该字段，未知时默认显示

### 标签缓存与排序

- 标签在启动时预加载到内存 `subject_tags_cache`（字典 key=subject_id）
- `_top_tags_from_cache(subject_id, limit=3)` 提取卡片标签：最多 1 个日期标签（取最具体的），排除年代范围（如 "2020-2029"、"2020年代"），其余按 count 降序填充
- 详情弹窗 `limit=8`

### qBittorrent 自动启动

`push_to_qbittorrent()` 在连接失败时：
1. 查找 `qbittorrent.exe`（用户配置路径 → `%PROGRAMFILES%` → `%PROGRAMFILES(X86)%` → `%LOCALAPPDATA%`）
2. `subprocess.Popen` 后台启动，`creationflags=0x08000000` 隐藏控制台窗口
3. 轮询 20 次（每 500ms）等待 WebUI 就绪后推送
4. 需用户先在 qBittorrent 中启用 WebUI（工具 → 选项 → Web UI → 启用），否则自动启动后仍连不上

### 人气高亮与排序

前端纯 JS 实现，不需要后端改动：

| 等级 | CSS 类 | 条件 |
|------|--------|------|
| 神作 | `.anime-card.elite` | rank ≤ 100 |
| 热门 | `.anime-card.popular` | rank ≤ 500 |

排序栏三种模式：默认（日历原序）、评分（`rating.score` 降序）、人气（`collection.collect` 下载人数降序）。

「全部」标签：`activeDay = -1`，展平所有天条目。日历/收藏/搜索三处卡片渲染都要同步维护人气高亮逻辑。

## Tutorial Maintenance

- 新手引导教程位于 `WEB/index.html`，每次**新增面向用户的重大功能**（如新按钮、新操作流程、新界面区域）时，必须同步更新教程内容和步骤
- 教程通过 `localStorage` 标记首次启动状态，设置页提供「重新观看教程」按钮

## Packaging Notes

- 使用 `build.spec`（PyInstaller spec 文件）
- 需要 `hiddenimports` 包含 `pycparser`、`cffi`（pythonnet/CLR 依赖）
- `pycparser` 不能出现在 `excludes` 中（cffi 运行时需要）
- 数据目录 `WEB/` 映射为 `web/`（注意大小写）
- 依赖: pywebview, bottle, requests, feedparser, qbittorrent-api, pycparser
- `animeasi/` 是自定义业务包，新增模块后需同步检查 `build.spec` 的 hiddenimports
- 目标机器需安装 WebView2 Runtime（Windows 10 可能需要手动安装，Win11 自带）
