"""Parse PBS KIDS watch pages and expose non-DRM media profiles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from pbs_kids_catalog_parser import CatalogError, extract_next_data


@dataclass(frozen=True)
class MediaProfile:
    profile: str
    redirect_url: str
    fairplay_license_url: str | None
    widevine_license_url: str | None

    @property
    def has_drm_license(self) -> bool:
        return bool(self.fairplay_license_url or self.widevine_license_url)


@dataclass(frozen=True)
class WatchPage:
    media_manager_guid: str
    title: str
    description_long: str | None
    duration_seconds: int | None
    source_expires_at: str | None
    drm_enabled: bool
    media_profiles: tuple[MediaProfile, ...]


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


def parse_watch_page(html: str) -> WatchPage:
    """Parse the embedded Next.js payload from one PBS KIDS watch page."""
    root = extract_next_data(html)
    try:
        video_data = root["props"]["pageProps"]["videoData"]
        media_asset = video_data["mediaManagerAsset"]
    except (KeyError, TypeError) as exc:
        raise CatalogError(
            "Missing props.pageProps.videoData.mediaManagerAsset."
        ) from exc

    if not isinstance(video_data, Mapping) or not isinstance(media_asset, Mapping):
        raise CatalogError("Watch-page video data is not an object.")

    media_manager_guid = media_asset.get("id")
    if not media_manager_guid:
        raise CatalogError("The watch page does not contain a Media Manager GUID.")

    duration = media_asset.get("duration")
    try:
        duration_seconds = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_seconds = None

    profiles: list[MediaProfile] = []
    seen: set[tuple[str, str]] = set()
    for item in media_asset.get("videos") or []:
        if not isinstance(item, Mapping):
            continue
        profile = item.get("profile")
        url = item.get("url")
        if not profile or not url:
            continue
        identity = (str(profile), str(url))
        if identity in seen:
            continue
        seen.add(identity)
        profiles.append(
            MediaProfile(
                profile=identity[0],
                redirect_url=identity[1],
                fairplay_license_url=(
                    str(item["fairplay_license"])
                    if item.get("fairplay_license")
                    else None
                ),
                widevine_license_url=(
                    str(item["widevine_license"])
                    if item.get("widevine_license")
                    else None
                ),
            )
        )

    return WatchPage(
        media_manager_guid=str(media_manager_guid),
        title=str(media_asset.get("title") or video_data.get("title") or "Untitled"),
        description_long=(
            str(media_asset["description_long"])
            if media_asset.get("description_long") is not None
            else None
        ),
        duration_seconds=duration_seconds,
        source_expires_at=_epoch_to_iso(video_data.get("expiryDate")),
        drm_enabled=bool(media_asset.get("drm_enabled")),
        media_profiles=tuple(profiles),
    )


def choose_media_profile(
    watch_page: WatchPage,
    preferred_profiles: list[str],
) -> MediaProfile | None:
    """Choose the first preferred, non-DRM media profile."""
    if watch_page.drm_enabled:
        return None

    available = [item for item in watch_page.media_profiles if not item.has_drm_license]
    for preferred in preferred_profiles:
        for item in available:
            if item.profile == preferred:
                return item
    return available[0] if available else None
