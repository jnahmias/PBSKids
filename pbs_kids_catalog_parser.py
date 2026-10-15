#!/usr/bin/env python3
"""Parse PBS KIDS show catalogs from embedded Next.js page data."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import urljoin

try:
    import httpx
except ImportError:
    httpx = None

USER_AGENT = "PBSKidsCatalog/0.1 (+personal catalog synchronization)"

class CatalogError(RuntimeError):
    pass

class _NextDataParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.capture = False
        self.parts: list[str] = []
        self.matches = 0
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.matches += 1
            self.capture = True
    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.capture:
            self.capture = False

@dataclass(frozen=True)
class ImageAsset:
    profile: str
    url: str

@dataclass(frozen=True)
class CollectionMembership:
    id: str
    slug: str
    title: str
    heading: str | None
    watch_url: str

@dataclass
class CatalogVideo:
    cms_id: str
    guid: str
    slug: str
    title: str
    video_type: str
    description_short: str | None
    duration_seconds: int | None
    legacy_tp_media_id: str | None
    premiered_on_utc: str | None
    images: list[ImageAsset] = field(default_factory=list)
    memberships: list[CollectionMembership] = field(default_factory=list)

@dataclass(frozen=True)
class CatalogMetadata:
    source_url: str
    page_title: str | None
    page_slug: str | None
    next_page_route: str | None
    next_build_id: str | None
    total_occurrences: int
    unique_videos: int

@dataclass
class ParsedCatalog:
    metadata: CatalogMetadata
    videos: list[CatalogVideo]

def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []

def _required(obj: Mapping[str, Any], key: str, context: str) -> str:
    value = obj.get(key)
    if value is None or not str(value).strip():
        raise CatalogError(f"Missing {key!r} in {context}")
    return str(value)

def _epoch_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None

def extract_next_data(html: str) -> dict[str, Any]:
    p = _NextDataParser()
    p.feed(html)
    p.close()
    if p.matches != 1:
        raise CatalogError(f"Expected exactly one __NEXT_DATA__ script; found {p.matches}")
    try:
        value = json.loads("".join(p.parts))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Invalid __NEXT_DATA__ JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CatalogError("__NEXT_DATA__ is not a JSON object")
    return value

def _images(entry: Mapping[str, Any]) -> list[ImageAsset]:
    result: list[ImageAsset] = []
    seen: set[tuple[str, str]] = set()
    asset = entry.get("mediaManagerAsset")
    if isinstance(asset, Mapping):
        for item in _as_list(asset.get("images")):
            if isinstance(item, Mapping) and item.get("profile") and item.get("image"):
                key = (str(item["profile"]), str(item["image"]))
                if key not in seen:
                    seen.add(key)
                    result.append(ImageAsset(*key))
    for name in ("posterImage", "mezzanine", "squareImage"):
        for item in _as_list(entry.get(name)):
            if isinstance(item, Mapping):
                url = item.get("its_url") or item.get("url")
                if url:
                    key = (f"cms-{name}", str(url))
                    if key not in seen:
                        seen.add(key)
                        result.append(ImageAsset(*key))
    return result

def parse_catalog_html(html: str, source_url: str) -> ParsedCatalog:
    root = extract_next_data(html)
    try:
        page_data = root["props"]["pageProps"]["pageData"]
    except (KeyError, TypeError) as exc:
        raise CatalogError("Missing props.pageProps.pageData") from exc
    if not isinstance(page_data, Mapping):
        raise CatalogError("pageData is not an object")

    modules: list[Mapping[str, Any]] = []
    for key in ("bodyContentModules", "mastheadContentModules"):
        modules.extend(x for x in _as_list(page_data.get(key)) if isinstance(x, Mapping))

    videos: dict[str, CatalogVideo] = {}
    total = 0
    for module in modules:
        heading = str(module["heading"]) if module.get("heading") is not None else None
        for collection in _as_list(module.get("collection")):
            if not isinstance(collection, Mapping):
                continue
            collection_id = _required(collection, "id", "collection")
            collection_slug = _required(collection, "slug", "collection")
            collection_title = str(collection.get("title") or heading or collection_slug)
            for entry in _as_list(collection.get("entries")):
                if not isinstance(entry, Mapping) or entry.get("__typename") not in (None, "PbsKidsVideo"):
                    continue
                total += 1
                cms_id = _required(entry, "id", "video")
                guid = _required(entry, "guid", f"video {cms_id}")
                slug = _required(entry, "slug", f"video {cms_id}")
                title = _required(entry, "title", f"video {cms_id}")
                asset = entry.get("mediaManagerAsset")
                asset = asset if isinstance(asset, Mapping) else {}
                duration = asset.get("duration")
                try:
                    duration_seconds = int(duration) if duration is not None else None
                except (TypeError, ValueError):
                    duration_seconds = None
                video = videos.get(guid)
                if video is None:
                    video = CatalogVideo(
                        cms_id=cms_id,
                        guid=guid,
                        slug=slug,
                        title=title,
                        video_type=str(entry.get("videoType") or "unknown"),
                        description_short=str(asset["description_short"]) if asset.get("description_short") is not None else None,
                        duration_seconds=duration_seconds,
                        legacy_tp_media_id=str(asset["legacy_tp_media_id"]) if asset.get("legacy_tp_media_id") is not None else None,
                        premiered_on_utc=_epoch_iso(asset.get("premiered_on")),
                        images=_images(entry),
                    )
                    videos[guid] = video
                membership = CollectionMembership(
                    id=collection_id,
                    slug=collection_slug,
                    title=collection_title,
                    heading=heading,
                    watch_url=urljoin(source_url, f"/videos/watch/{collection_slug}/{collection_id}/{slug}/{cms_id}"),
                )
                if membership not in video.memberships:
                    video.memberships.append(membership)
    if not videos:
        raise CatalogError("No PBS KIDS video entries found")
    output = sorted(videos.values(), key=lambda x: (x.video_type.casefold(), x.title.casefold(), x.guid))
    return ParsedCatalog(
        metadata=CatalogMetadata(
            source_url=source_url,
            page_title=str(page_data["title"]) if page_data.get("title") else None,
            page_slug=str(page_data["slug"]) if page_data.get("slug") else None,
            next_page_route=str(root["page"]) if root.get("page") else None,
            next_build_id=str(root["buildId"]) if root.get("buildId") else None,
            total_occurrences=total,
            unique_videos=len(output),
        ),
        videos=output,
    )
