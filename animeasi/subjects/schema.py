def normalize_rank(rank, rating=None):
    if rank is None and rating:
        rank = rating.get("rank")
    return None if rank in (None, 0) else rank


def normalize_images(images=None, img=None):
    images = images or {}
    common = images.get("common") or img or ""
    large = images.get("large") or common
    return {"common": common, "large": large}


def normalize_subject(
    data,
    *,
    top_tags=None,
    is_japanese=None,
    is_season_mainline=None,
    include_legacy_img=False,
):
    sid = data.get("id") or data.get("subject_id")
    name = data.get("name") or ""
    name_cn = data.get("name_cn") or ""
    rating = data.get("rating")
    images = normalize_images(data.get("images"), data.get("img"))
    subject = {
        "id": sid,
        "name": name,
        "name_cn": name_cn,
        "display_name": name_cn or name,
        "url": data.get("url") or (f"https://bgm.tv/subject/{sid}" if sid else ""),
        "summary": data.get("summary") or "",
        "air_date": data.get("air_date") or data.get("date"),
        "rating": rating,
        "rank": normalize_rank(data.get("rank"), rating),
        "collection": data.get("collection"),
        "platform": data.get("platform"),
        "images": images,
        "top_tags": top_tags if top_tags is not None else data.get("top_tags"),
        "is_japanese": is_japanese if is_japanese is not None else data.get("is_japanese"),
        "is_season_mainline": (
            is_season_mainline
            if is_season_mainline is not None
            else data.get("is_season_mainline")
        ),
    }
    if include_legacy_img:
        subject["img"] = images["common"]
    return subject


def subject_from_row(
    row,
    *,
    rating=None,
    collection=None,
    top_tags=None,
    is_japanese=None,
    is_season_mainline=None,
):
    return normalize_subject(
        {
            "id": row["id"],
            "name": row["name"],
            "name_cn": row["name_cn"],
            "url": row["url"],
            "summary": row["summary"] or "",
            "air_date": row["air_date"],
            "rating": rating,
            "rank": row["rank"],
            "collection": collection,
            "platform": row["platform"] if "platform" in row.keys() else None,
            "images": {"common": row["image_common"], "large": row["image_large"]},
        },
        top_tags=top_tags,
        is_japanese=is_japanese,
        is_season_mainline=is_season_mainline,
    )
