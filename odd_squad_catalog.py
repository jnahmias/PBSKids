#!/usr/bin/env python3
"""Retrieve and parse the PBS KIDS Odd Squad video catalog.

Phase 1 responsibilities:
- Fetch a catalog page, or read a saved HTML file.
- Parse Next.js __NEXT_DATA__ JSON without executing JavaScript.
- Find PBS KIDS video-playlist collections.
- Normalize and deduplicate records by Media Manager GUID.
- Preserve every collection membership.
- Construct individual watch-page URLs.
- Write JSON and/or CSV catalog output.

Phase 2 extension point:
- CaptionTrack and EpisodeDetail models are defined, but watch-page/caption
  enrichment and media downloading are intentionally not implemented here.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urljoin

try:
    import httpx
except ImportError:  # Allow parsing saved HTML without httpx installed.
    httpx = None  # type: ignore[assignment]

DEFAULT_CATALOG_URL = "https://pbskids.org/videos/odd-squad"
DEFAULT_OUTPUT = "odd-squad-catalog.json"
USER_AGENT = "OddSquadCatalog/0.1 (+personal catalog synchronization)"
LOG = logging.getLogger("odd_squad_catalog")


class CatalogError(RuntimeError):
    """Raised when the page cannot be fetched or parsed as a catalog."""


class NextDataHTMLParser(HTMLParser):
    """Extract the JSON text from script#__NEXT_DATA__."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._capturing = False
        self._parts: list[str] = []
        self.matches = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag.lower() == "script" and attributes.get("id") == "__NEXT_DATA__":
            self.matches += 1
            self._capturing = True

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self._capturing = False

    @property
    def json_text(self) -> str:
        return "".join(self._parts).strip()


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
    module_area: str
    watch_url: str


@dataclass(frozen=True)
class CaptionTrack:
    """Reserved for phase 2 watch-page and manifest enrichment."""

    language: str | None
    kind: str | None
    format: str | None
    url: str
    label: str | None = None


@dataclass
class CatalogVideo:
    cms_id: str
    guid: str
    slug: str
    title: str
    video_type: str
    show_slug: str | None
    show_title: str | None
    description_short: str | None
    duration_seconds: int | None
    legacy_tp_media_id: str | None
    premiered_on_epoch: int | None
    premiered_on_utc: str | None
    images: list[ImageAsset] = field(default_factory=list)
    memberships: list[CollectionMembership] = field(default_factory=list)
    preferred_watch_url: str | None = None
    captions: list[CaptionTrack] = field(default_factory=list)


@dataclass(frozen=True)
class CatalogMetadata:
    source_url: str
    retrieved_at_utc: str
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "metadata": asdict(self.metadata),
            "videos": [asdict(video) for video in self.videos],
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def epoch_to_iso(value: Any) -> tuple[int | None, str | None]:
    if value in (None, ""):
        return None, None
    try:
        epoch = int(value)
        iso = datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat()
        return epoch, iso
    except (TypeError, ValueError, OSError, OverflowError):
        LOG.warning("Ignoring invalid premiered_on value: %r", value)
        return None, None


def extract_next_data(html: str) -> dict[str, Any]:
    parser = NextDataHTMLParser()
    parser.feed(html)
    parser.close()

    if parser.matches == 0:
        raise CatalogError("The page does not contain script#__NEXT_DATA__.")
    if parser.matches > 1:
        raise CatalogError(f"Expected one __NEXT_DATA__ script, found {parser.matches}.")
    if not parser.json_text:
        raise CatalogError("The __NEXT_DATA__ script is empty.")

    try:
        data = json.loads(parser.json_text)
    except json.JSONDecodeError as exc:
        raise CatalogError(f"The __NEXT_DATA__ script is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise CatalogError("The __NEXT_DATA__ payload is not a JSON object.")
    return data


def fetch_html(url: str, timeout: float = 30.0) -> str:
    if httpx is None:
        raise CatalogError(
            "Fetching requires httpx. Install it with: python -m pip install httpx"
        )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
    }
    try:
        with httpx.Client(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout),
            http2=False,
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.lower():
                raise CatalogError(
                    f"Expected HTML from {url}, received {content_type or 'unknown content type'}."
                )
            return response.text
    except httpx.HTTPError as exc:
        raise CatalogError(f"Unable to retrieve {url}: {exc}") from exc


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _required_text(obj: Mapping[str, Any], key: str, context: str) -> str:
    value = obj.get(key)
    if value is None or str(value).strip() == "":
        raise CatalogError(f"Missing required field {key!r} in {context}.")
    return str(value)


def _build_watch_url(
    base_url: str,
    collection_slug: str,
    collection_id: str,
    video_slug: str,
    video_id: str,
) -> str:
    path = (
        f"/videos/watch/{collection_slug}/{collection_id}/"
        f"{video_slug}/{video_id}"
    )
    return urljoin(base_url, path)


def _images_from(entry: Mapping[str, Any]) -> list[ImageAsset]:
    images: list[ImageAsset] = []
    seen: set[tuple[str, str]] = set()

    asset = entry.get("mediaManagerAsset")
    if isinstance(asset, Mapping):
        for image in _as_list(asset.get("images")):
            if not isinstance(image, Mapping):
                continue
            url = image.get("image")
            profile = image.get("profile")
            if url and profile:
                key = (str(profile), str(url))
                if key not in seen:
                    seen.add(key)
                    images.append(ImageAsset(profile=key[0], url=key[1]))

    # Keep CMS images as fallbacks if Media Manager images are absent or incomplete.
    for key_name in ("posterImage", "mezzanine", "squareImage"):
        for image in _as_list(entry.get(key_name)):
            if not isinstance(image, Mapping):
                continue
            url = image.get("its_url") or image.get("url")
            if url:
                key = (f"cms-{key_name}", str(url))
                if key not in seen:
                    seen.add(key)
                    images.append(ImageAsset(profile=key[0], url=key[1]))
    return images


def _entry_show(entry: Mapping[str, Any]) -> tuple[str | None, str | None]:
    for prop in _as_list(entry.get("properties")):
        if isinstance(prop, Mapping):
            slug = prop.get("slug")
            title = prop.get("title")
            if slug or title:
                return (
                    str(slug) if slug is not None else None,
                    str(title) if title is not None else None,
                )
    return None, None


def _membership_rank(membership: CollectionMembership) -> tuple[int, str]:
    preferred = {
        "odd-squad-full-episodes": 0,
        "odd-squad-movies": 1,
        "odd-squad-asl-collection": 2,
    }
    return (preferred.get(membership.slug, 10), membership.slug)


def parse_catalog_html(html: str, source_url: str = DEFAULT_CATALOG_URL) -> ParsedCatalog:
    next_data = extract_next_data(html)
    try:
        page_props = next_data["props"]["pageProps"]
        page_data = page_props["pageData"]
    except (KeyError, TypeError) as exc:
        raise CatalogError(
            "The Next.js payload does not contain props.pageProps.pageData."
        ) from exc

    if not isinstance(page_data, Mapping):
        raise CatalogError("props.pageProps.pageData is not an object.")

    modules: list[tuple[str, Mapping[str, Any]]] = []
    for area, key in (
        ("body", "bodyContentModules"),
        ("masthead", "mastheadContentModules"),
    ):
        for module in _as_list(page_data.get(key)):
            if isinstance(module, Mapping):
                modules.append((area, module))

    videos_by_identity: dict[str, CatalogVideo] = {}
    total_occurrences = 0

    for area, module in modules:
        typename = str(module.get("__typename") or "")
        if "VideoPlaylist" not in typename and not module.get("collection"):
            continue

        heading = str(module["heading"]) if module.get("heading") is not None else None
        for collection in _as_list(module.get("collection")):
            if not isinstance(collection, Mapping):
                continue
            collection_id = _required_text(collection, "id", "playlist collection")
            collection_slug = _required_text(collection, "slug", "playlist collection")
            collection_title = str(collection.get("title") or heading or collection_slug)

            for entry in _as_list(collection.get("entries")):
                if not isinstance(entry, Mapping):
                    continue
                if entry.get("__typename") not in (None, "PbsKidsVideo"):
                    continue

                total_occurrences += 1
                cms_id = _required_text(entry, "id", "video entry")
                guid = _required_text(entry, "guid", f"video entry {cms_id}")
                slug = _required_text(entry, "slug", f"video entry {cms_id}")
                title = _required_text(entry, "title", f"video entry {cms_id}")
                video_type = str(entry.get("videoType") or "unknown")
                watch_url = _build_watch_url(
                    source_url, collection_slug, collection_id, slug, cms_id
                )
                membership = CollectionMembership(
                    id=collection_id,
                    slug=collection_slug,
                    title=collection_title,
                    heading=heading,
                    module_area=area,
                    watch_url=watch_url,
                )

                asset = entry.get("mediaManagerAsset")
                asset = asset if isinstance(asset, Mapping) else {}
                premiered_epoch, premiered_iso = epoch_to_iso(asset.get("premiered_on"))
                show_slug, show_title = _entry_show(entry)

                video = videos_by_identity.get(guid)
                if video is None:
                    duration = asset.get("duration")
                    try:
                        duration_seconds = int(duration) if duration is not None else None
                    except (TypeError, ValueError):
                        duration_seconds = None

                    video = CatalogVideo(
                        cms_id=cms_id,
                        guid=guid,
                        slug=slug,
                        title=title,
                        video_type=video_type,
                        show_slug=show_slug,
                        show_title=show_title,
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
                        premiered_on_epoch=premiered_epoch,
                        premiered_on_utc=premiered_iso,
                        images=_images_from(entry),
                    )
                    videos_by_identity[guid] = video
                else:
                    # Duplicate appearances should describe the same CMS item.
                    if video.cms_id != cms_id or video.slug != slug:
                        LOG.warning(
                            "GUID %s appears with conflicting CMS IDs/slugs: %s/%s vs %s/%s",
                            guid,
                            video.cms_id,
                            video.slug,
                            cms_id,
                            slug,
                        )

                if membership not in video.memberships:
                    video.memberships.append(membership)

    if not videos_by_identity:
        raise CatalogError("No PBS KIDS video entries were found in the catalog page.")

    videos = list(videos_by_identity.values())
    for video in videos:
        video.memberships.sort(key=_membership_rank)
        video.preferred_watch_url = (
            video.memberships[0].watch_url if video.memberships else None
        )

    videos.sort(
        key=lambda video: (
            video.video_type.casefold(),
            video.premiered_on_epoch or 0,
            video.title.casefold(),
            video.guid,
        )
    )

    metadata = CatalogMetadata(
        source_url=source_url,
        retrieved_at_utc=utc_now_iso(),
        page_title=(str(page_data["title"]) if page_data.get("title") else None),
        page_slug=(str(page_data["slug"]) if page_data.get("slug") else None),
        next_page_route=(str(next_data["page"]) if next_data.get("page") else None),
        next_build_id=(
            str(next_data["buildId"]) if next_data.get("buildId") else None
        ),
        total_occurrences=total_occurrences,
        unique_videos=len(videos),
    )
    return ParsedCatalog(metadata=metadata, videos=videos)


def filter_videos(
    videos: Iterable[CatalogVideo],
    video_types: set[str] | None,
    include_asl: bool,
    collections: set[str] | None,
) -> list[CatalogVideo]:
    output: list[CatalogVideo] = []
    for video in videos:
        slugs = {m.slug for m in video.memberships}
        if video_types and video.video_type not in video_types:
            continue
        if collections and not slugs.intersection(collections):
            continue
        if not include_asl and "odd-squad-asl-collection" in slugs:
            continue
        output.append(video)
    return output


def write_json(path: Path, catalog: ParsedCatalog, videos: Sequence[CatalogVideo]) -> None:
    payload = catalog.as_dict()
    payload["metadata"]["selected_videos"] = len(videos)
    payload["videos"] = [asdict(video) for video in videos]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_csv(path: Path, videos: Sequence[CatalogVideo]) -> None:
    fields = [
        "guid",
        "cms_id",
        "legacy_tp_media_id",
        "title",
        "video_type",
        "show_title",
        "description_short",
        "duration_seconds",
        "premiered_on_utc",
        "preferred_watch_url",
        "collection_slugs",
        "image_16x9",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for video in videos:
            preferred_image = next(
                (
                    image.url
                    for image in video.images
                    if "16x9" in image.profile.casefold()
                ),
                video.images[0].url if video.images else "",
            )
            writer.writerow(
                {
                    "guid": video.guid,
                    "cms_id": video.cms_id,
                    "legacy_tp_media_id": video.legacy_tp_media_id or "",
                    "title": video.title,
                    "video_type": video.video_type,
                    "show_title": video.show_title or "",
                    "description_short": video.description_short or "",
                    "duration_seconds": video.duration_seconds or "",
                    "premiered_on_utc": video.premiered_on_utc or "",
                    "preferred_watch_url": video.preferred_watch_url or "",
                    "collection_slugs": ";".join(sorted({m.slug for m in video.memberships})),
                    "image_16x9": preferred_image,
                }
            )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retrieve and parse the PBS KIDS Odd Squad video catalog."
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--html",
        type=Path,
        help="Parse a previously saved catalog HTML file instead of fetching the URL.",
    )
    source.add_argument(
        "--url",
        default=DEFAULT_CATALOG_URL,
        help=f"Catalog URL to retrieve (default: {DEFAULT_CATALOG_URL}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT),
        help=f"JSON output path (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument("--csv", type=Path, help="Also write a flattened CSV catalog.")
    parser.add_argument(
        "--type",
        dest="video_types",
        action="append",
        help=(
            "Video type to include; repeat for multiple values. "
            "Default: fullEpisode. Use --all-types to disable type filtering."
        ),
    )
    parser.add_argument(
        "--all-types", action="store_true", help="Include all video types."
    )
    parser.add_argument(
        "--include-asl",
        action="store_true",
        help="Include entries from the American Sign Language collection.",
    )
    parser.add_argument(
        "--collection",
        action="append",
        help="Only include videos belonging to this collection slug; repeatable.",
    )
    parser.add_argument(
        "--save-html",
        type=Path,
        help="When fetching, save the retrieved HTML for diagnostics.",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="HTTP timeout in seconds."
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        if args.html:
            source_url = DEFAULT_CATALOG_URL
            LOG.info("Reading saved catalog: %s", args.html)
            html = args.html.read_text(encoding="utf-8")
        else:
            source_url = args.url
            LOG.info("Retrieving catalog: %s", source_url)
            html = fetch_html(source_url, timeout=args.timeout)
            if args.save_html:
                args.save_html.write_text(html, encoding="utf-8")
                LOG.info("Saved HTML: %s", args.save_html)

        catalog = parse_catalog_html(html, source_url=source_url)
        video_types = None if args.all_types else set(args.video_types or ["fullEpisode"])
        collections = set(args.collection) if args.collection else None
        selected = filter_videos(
            catalog.videos,
            video_types=video_types,
            include_asl=args.include_asl,
            collections=collections,
        )

        write_json(args.output, catalog, selected)
        LOG.info(
            "Parsed %d unique videos from %d occurrences; selected %d.",
            catalog.metadata.unique_videos,
            catalog.metadata.total_occurrences,
            len(selected),
        )
        LOG.info("Wrote JSON: %s", args.output)

        if args.csv:
            write_csv(args.csv, selected)
            LOG.info("Wrote CSV: %s", args.csv)
        return 0
    except (CatalogError, OSError) as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
