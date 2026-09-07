# AnimeAsi

全能番剧中枢 — 基于 pywebview 的桌面追番工具，集成 Bangumi 日历、番剧搜索、种子下载、本地播放。

> **关于开发**：本项目完全由 AI 辅助生成，作者无编程背景，仅提供功能需求与测试反馈。代码可能存在不规范之处，欢迎指出和改进。

## 功能

- **番剧日历** — 浏览 Bangumi 每日放送表，自动按标签过滤日漫/非日漫。**由于近期Bangumi的原因，现在日历功能可能也需要对应网络环境**。
- **番剧搜索** — 搜索 Bangumi 数据库，支持评分展示
- **种子搜索** — 聚合多个 RSS 源（蜜柑计划、Nyaa.si、动漫花园等）并行搜索
- **下载推送** — 一键推送到 qBittorrent
- **主题系统** — 支持明暗模式（日间 ☀️ / 夜间 🌙）导航栏独立胶囊切换，并在设置中提供经典紫、自然绿、樱花粉、晴空蓝、暖秋橘 5 套专属双模式色彩方案自由定制
- **RSS 订阅计划** — 按关键词、字幕组、画质与排除词自动匹配资源，支持手动检查或在 RSS 页面设置自动检查间隔（15 分钟 ~ 24 小时），命中后可自动推送到 qBittorrent
- **本地播放** — 扫描本地视频文件，解析集数，标记观看状态

## 运行

```bash
# 创建虚拟环境并安装依赖
python -m venv venv
source venv/Scripts/activate   # Windows Git Bash
pip install pywebview bottle requests feedparser qbittorrent-api pycparser

# 运行
python main.py
```

## 打包

```bash
pip install pyinstaller
pyinstaller build.spec
# 输出: dist/AnimeAsi.exe
```

## 架构

| 路径 | 职责 |
|------|------|
| `main.py` | 桌面入口，Bottle HTTP 服务，pywebview 窗口，JS API bridge |
| `animeasi/database.py` | SQLite 数据层（日历/标签/收藏/观看记录，旧 JSON 迁移） |
| `animeasi/local_manager.py` | 本地视频扫描、集数解析、系统播放 |
| `animeasi/cache/cover_cache.py` | 封面缓存、缓存命中 URL 改写、后台下载 |
| `animeasi/downloads/` | RSS 种子搜索、资源标签解析、去重、qBittorrent 推送 |
| `animeasi/season/browser.py` | 季度浏览、Bangumi 分页拉取、季度缓存、日漫主线过滤 |
| `animeasi/subjects/schema.py` | 统一前后端 subject/card 数据契约 |
| `WEB/index.html` | 前端单页应用（内联 CSS + JS） |

数据文件（运行时自动生成）：

| 文件 | 说明 |
|------|------|
| `animeasi.db` | SQLite 数据库（WAL 模式） |
| `config.json` | 用户配置 |
| `cache_covers/` | 封面图片缓存 |
| `error.log` | 错误日志 |

## 数据来源

- 番剧数据：[Bangumi API](https://bangumi.github.io/api/)
- 种子搜索：蜜柑计划、Nyaa.si、动漫花园、TokyoTosho、ACG.RIP

## 网络说明

- Bangumi API 国内通常可直接访问
- RSS 源中 Nyaa.si、TokyoTosho、ACG.RIP 需要代理
- 可在设置中启用代理（默认 `127.0.0.1:7890`）或仅启用国内源
- qBittorrent 推送需开启 WebUI（工具 → 选项 → Web UI）

## 本地播放

1. 设置中配置动画存放目录
2. 点击番剧卡片播放按钮 → 弹出剧集列表
3. 支持格式：mp4 / mkv / avi / mov / wmv / flv / webm

目录结构示例：
```
D:\Anime\葬送的芙莉莲\EP01.mp4
D:\Anime\葬送的芙莉莲\EP02.mp4
```

## 许可

MIT
