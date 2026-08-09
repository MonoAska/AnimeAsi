import re


def _clean_token(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" []【】()（）")


def format_size(value) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ""
    if size < 1024 * 1024:
        return ""
    units = ["B", "KB", "MB", "GB"]
    num = float(size)
    for unit in units:
        if num < 1024 or unit == units[-1]:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return ""


def extract_entry_size(entry) -> str:
    for enc in entry.get("enclosures", []):
        size = format_size(enc.get("length"))
        if size:
            return size
    for key in ("length", "size", "nyaa_size"):
        size = format_size(entry.get(key))
        if size:
            return size
    title = entry.get("title", "")
    match = re.search(r"(?i)(\d+(?:\.\d+)?)\s*(GB|GiB|MB|MiB)", title)
    if match:
        return f"{match.group(1)} {match.group(2).upper().replace('IB', 'B')}"
    return ""


def parse_torrent_title(title: str) -> dict:
    """Extract readable resource metadata from common anime torrent titles."""
    text = title or ""
    meta = {
        "group": "",
        "episode": "",
        "episode_range": "",
        "is_batch": False,
        "resolution": "",
        "subtitle": "",
        "codec": "",
        "container": "",
    }

    group = re.match(r"^\s*[\[【]([^\]】]{1,48})[\]】]", text)
    if group:
        meta["group"] = _clean_token(group.group(1))

    batch_match = re.search(
        r"(?i)(合集|全集|全\s*\d{1,3}\s*[话話集]?|batch|complete|complete\s+series)",
        text,
    )

    season_episode_match = re.search(
        r"(?i)\bseason\s*\d{1,2}\s*[-_]\s*(\d{1,3})(?:v\d+)?\b",
        text,
    )
    if not season_episode_match:
        season_episode_match = re.search(
            r"(?i)\bs\d{1,2}e(\d{1,3})(?:v\d+)?\b",
            text,
        )

    range_match = None
    if not season_episode_match or batch_match:
        range_match = re.search(
            r"(?<!\d)(\d{1,3})\s*(?:-|~|～|至|到)\s*(\d{1,3})(?!\d)", text
        )
    if range_match:
        start, end = range_match.groups()
        meta["episode_range"] = f"{int(start):02d}-{int(end):02d}"
        meta["is_batch"] = True

    if batch_match:
        meta["is_batch"] = True

    if season_episode_match and not meta["episode_range"]:
        episode = int(season_episode_match.group(1))
        if 0 < episode < 200:
            meta["episode"] = f"{episode:02d}"

    if not meta["episode_range"] and not meta["episode"]:
        episode_patterns = [
            r"(?i)(?:第|ep(?:isode)?\.?\s*)\s*(\d{1,3})(?:\s*[话話集])?",
            r"(?:^|[\s_\-\[\(【])(\d{1,3})(?:v\d+)?(?:$|[\s_\-\]\)】.])",
        ]
        for pattern in episode_patterns:
            candidates = []
            for match in re.finditer(pattern, text):
                value = int(match.group(1))
                if 0 < value < 200 and value not in (264, 265):
                    candidates.append(value)
            if candidates:
                meta["episode"] = f"{candidates[-1]:02d}"
                break

    resolution = re.search(r"(?i)(2160p|1080p|720p|480p|4k)", text)
    if resolution:
        value = resolution.group(1).lower()
        meta["resolution"] = "2160p" if value == "4k" else value

    if re.search(r"(?i)(简繁|繁简|CHS\s*[&+/]\s*CHT|CHT\s*[&+/]\s*CHS)", text):
        meta["subtitle"] = "简繁"
    elif re.search(r"(?i)(简中|简体|CHS|GB)", text):
        meta["subtitle"] = "简中"
    elif re.search(r"(?i)(繁中|繁体|CHT|BIG5)", text):
        meta["subtitle"] = "繁中"

    codec = re.search(r"(?i)(hevc|h\.?265|x265|avc|h\.?264|x264|av1)", text)
    if codec:
        raw = codec.group(1).lower().replace(".", "")
        if raw in ("hevc", "h265", "x265"):
            meta["codec"] = "HEVC"
        elif raw in ("avc", "h264", "x264"):
            meta["codec"] = "AVC"
        else:
            meta["codec"] = "AV1"

    container = re.search(r"(?i)\b(mkv|mp4)\b", text)
    if container:
        meta["container"] = container.group(1).upper()

    return meta


def build_resource_tags(meta: dict, source: str = "", size: str = "") -> list[str]:
    tags = []
    if meta.get("group"):
        tags.append(meta["group"])
    elif source:
        tags.append(source)

    if meta.get("is_batch"):
        tags.append("合集")
        if meta.get("episode_range"):
            tags.append(meta["episode_range"])
    elif meta.get("episode"):
        tags.append(f"EP {meta['episode']}")
    else:
        tags.append("集数未知")

    for key in ("resolution", "subtitle", "codec", "container"):
        if meta.get(key):
            tags.append(meta[key])
    if size:
        tags.append(size)
    return tags
