"""Parse PBS KIDS catalog pages from embedded Next.js page data."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import urljoin


class CatalogError(RuntimeError):
    """Raised when a page cannot be interpreted as a PBS KIDS catalog."""


class NextDataParser(HTMLParser):
    """Extract the JSON text from script#__NEXT_DATA__."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.capture = False
        self.parts: list[str] = []
        self.matches = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() == "script" and dict(attrs).get("id") == "__NEXT_DATA__":
            self.capture = True
            self.matches += 1

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.capture:
            self.capture = False


@dataclass(frozen=True)
class ImageAsset:
    profile: str
    url: str


@dataclass(frozen=True)
class CollectionMembership:
    pbs_collection_id: str
    slug: str
    title: str
    heading: str | None
    watch_url: str
    ordinal_position: int


@dataclass
class CatalogVideo:
    cms_id: str
    media_manager_guid: str
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
class CollectionSummary:
    pbs_collection_id: str
    slug: str
    title: str
    heading: str | None
    entry_count: int
    video_types: dict[str, int]


@dataclass(frozen=True)
class CatalogMetadata:
    page_title: str | None
    page_slug: str | None
    build_id: str | None
    occurrence_count: int
    unique_asset_count: int


@dataclass
class ParsedCatalog:
    metadata: CatalogMetadata
    videos: list[CatalogVideo]
    collections: list[CollectionSummary]


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _required(obj: Mapping[str, Any], key: str, context: str) -> str:
    value = obj.get(key)
    if value is None or not str(value).strip():
        raise CatalogError(f"Missing {key!r} in {context}.")
    return str(value)


def _epoch_to_iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return (
            datetime.fromtimestamp(int(value), timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def extract_next_data(html: str) -> dict[str, Any]:
    parser = NextDataParser()
    parser.feed(html)
    parser.close()

    if parser.matches != 1:
        raise CatalogError(
            f"Expected exactly one __NEXT_DATA__ script; found {parser.matches}."
        )

    try:
        data = json.loads("".join(parser.parts))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"Invalid __NEXT_DATA__ JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise CatalogError("The __NEXT_DATA__ payload is not an object.")
    return data


def _extract_images(entry: Mapping[str, Any]) -> list[ImageAsset]:
    images: list[ImageAsset] = []
    seen: set[tuple[str, str]] = set()
    asset = entry.get("mediaManagerAsset")

    if isinstance(asset, Mapping):
        for item in _list(asset.get("images")):
            if not isinstance(item, Mapping):
                continue
            if not item.get("profile") or not item.get("image"):
                continue
            identity = (str(item["profile"]), str(item["image"]))
            if identity not in seen:
                seen.add(identity)
                images.append(ImageAsset(*identity))

    return images


def parse_catalog_html(html: str, source_url: str) -> ParsedCatalog:
    root = extract_next_data(html)
    try:
        page_data = root["props"]["pageProps"]["pageData"]
    except (KeyError, TypeError) as exc:
        raise CatalogError("Missing props.pageProps.pageData.") from exc

    if not isinstance(page_data, Mapping):
        raise CatalogError("pageData is not an object.")

    modules: list[Mapping[str, Any]] = []
    for key in ("bodyContentModules", "mastheadContentModules"):
        modules.extend(
            item for item in _list(page_data.get(key)) if isinstance(item, Mapping)
        )

    videos: dict[str, CatalogVideo] = {}
    collection_rows: dict[str, CollectionSummary] = {}
    occurrence_count = 0

    for module in modules:
        heading = str(module["heading"]) if module.get("heading") is not None else None
        for collection in _list(module.get("collection")):
            if not isinstance(collection, Mapping):
                continue

            collection_id = _required(collection, "id", "collection")
            collection_slug = _required(collection, "slug", "collection")
            collection_title = str(collection.get("title") or heading or collection_slug)
            entries = [
                item
                for item in _list(collection.get("entries"))
                if isinstance(item, Mapping)
                and item.get("__typename") in (None, "PbsKidsVideo")
            ]

            type_counts: dict[str, int] = {}
            for entry in entries:
                video_type = str(entry.get("videoType") or "unknown")
                type_counts[video_type] = type_counts.get(video_type, 0) + 1

            current_summary = CollectionSummary(
                pbs_collection_id=collection_id,
                slug=collection_slug,
                title=collection_title,
                heading=heading,
                entry_count=len(entries),
                video_types=type_counts,
            )
            previous = collection_rows.get(collection_id)
            if previous is None or current_summary.entry_count > previous.entry_count:
                collection_rows[collection_id] = current_summary

            for position, entry in enumerate(entries):
                occurrence_count += 1
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
                        media_manager_guid=guid,
                        slug=slug,
                        title=title,
                        video_type=str(entry.get("videoType") or "unknown"),
                        description_short=(
                            str(asset["description_short"])
                            if asset.get("description_short") is not None
                            else None
                        ),
                        duration_seconds=duration_seconds,
                        legacy_tp_media_id=(
                            str(asset["legacy_tp_media_id"])
                            if asset.get("legacy_tp_media_id") is not None
                            else None
                        ),
                        premiered_on_utc=_epoch_to_iso(asset.get("premiered_on")),
                        images=_extract_images(entry),
                    )
                    videos[guid] = video

                membership = CollectionMembership(
                    pbs_collection_id=collection_id,
                    slug=collection_slug,
                    title=collection_title,
                    heading=heading,
                    watch_url=urljoin(
                        source_url,
                        f"/videos/watch/{collection_slug}/{collection_id}/{slug}/{cms_id}",
                    ),
                    ordinal_position=position,
                )
                if membership not in video.memberships:
                    video.memberships.append(membership)

    if not videos:
        raise CatalogError("No PBS KIDS video entries were found.")

    return ParsedCatalog(
        metadata=CatalogMetadata(
            page_title=str(page_data["title"]) if page_data.get("title") else None,
            page_slug=str(page_data["slug"]) if page_data.get("slug") else None,
            build_id=str(root["buildId"]) if root.get("buildId") else None,
            occurrence_count=occurrence_count,
            unique_asset_count=len(videos),
        ),
        videos=sorted(
            videos.values(),
            key=lambda item: (
                item.video_type.casefold(),
                item.title.casefold(),
                item.media_manager_guid,
            ),
        ),
        collections=sorted(collection_rows.values(), key=lambda item: item.slug),
    )
