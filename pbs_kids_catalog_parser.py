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


class CatalogError(RuntimeError):
    pass


class NP(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.on = False
        self.parts = []
        self.n = 0

    def handle_starttag(self, t, a):
        if t.lower() == "script" and dict(a).get("id") == "__NEXT_DATA__":
            self.on = True
            self.n += 1

    def handle_data(self, d):
        if self.on:
            self.parts.append(d)

    def handle_endtag(self, t):
        if t.lower() == "script" and self.on:
            self.on = False


@dataclass(frozen=True)
class ImageAsset:
    profile: str
    url: str


@dataclass(frozen=True)
class Membership:
    id: str
    slug: str
    title: str
    heading: str | None
    watch_url: str


@dataclass
class Video:
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
    memberships: list[Membership] = field(default_factory=list)


@dataclass(frozen=True)
class Meta:
    page_title: str | None
    page_slug: str | None
    next_build_id: str | None


@dataclass
class Catalog:
    metadata: Meta
    videos: list[Video]


def L(v):
    return v if isinstance(v, list) else []


def req(o, k, c):
    v = o.get(k)
    if v is None or not str(v).strip():
        raise CatalogError(f"Missing {k} in {c}")
    return str(v)


def iso(v):
    try:
        return (
            datetime.fromtimestamp(int(v), timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
            if v not in (None, "")
            else None
        )
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def parse_catalog_html(html: str, source_url: str) -> Catalog:
    p = NP()
    p.feed(html)
    p.close()
    if p.n != 1:
        raise CatalogError(f"Expected one __NEXT_DATA__; found {p.n}")
    root = json.loads("".join(p.parts))
    pd = root["props"]["pageProps"]["pageData"]
    mods = []
    for k in ("bodyContentModules", "mastheadContentModules"):
        mods += [x for x in L(pd.get(k)) if isinstance(x, Mapping)]
    vids = {}
    for mod in mods:
        heading = str(mod["heading"]) if mod.get("heading") is not None else None
        for col in L(mod.get("collection")):
            if not isinstance(col, Mapping):
                continue
            cid, cslug = req(col, "id", "collection"), req(col, "slug", "collection")
            ctitle = str(col.get("title") or heading or cslug)
            for e in L(col.get("entries")):
                if not isinstance(e, Mapping) or e.get("__typename") not in (
                    None,
                    "PbsKidsVideo",
                ):
                    continue
                cms, guid, slug, title = (
                    req(e, "id", "video"),
                    req(e, "guid", "video"),
                    req(e, "slug", "video"),
                    req(e, "title", "video"),
                )
                a = (
                    e.get("mediaManagerAsset")
                    if isinstance(e.get("mediaManagerAsset"), Mapping)
                    else {}
                )
                if guid not in vids:
                    try:
                        dur = (
                            int(a["duration"])
                            if a.get("duration") is not None
                            else None
                        )
                    except (ValueError, TypeError):
                        dur = None
                    imgs = []
                    seen = set()
                    for im in L(a.get("images")):
                        if (
                            isinstance(im, Mapping)
                            and im.get("profile")
                            and im.get("image")
                        ):
                            q = (str(im["profile"]), str(im["image"]))
                            if q not in seen:
                                seen.add(q)
                                imgs.append(ImageAsset(*q))
                    vids[guid] = Video(
                        cms,
                        guid,
                        slug,
                        title,
                        str(e.get("videoType") or "unknown"),
                        (
                            str(a["description_short"])
                            if a.get("description_short") is not None
                            else None
                        ),
                        dur,
                        (
                            str(a["legacy_tp_media_id"])
                            if a.get("legacy_tp_media_id") is not None
                            else None
                        ),
                        iso(a.get("premiered_on")),
                        imgs,
                    )
                m = Membership(
                    cid,
                    cslug,
                    ctitle,
                    heading,
                    urljoin(source_url, f"/videos/watch/{cslug}/{cid}/{slug}/{cms}"),
                )
                if m not in vids[guid].memberships:
                    vids[guid].memberships.append(m)
    if not vids:
        raise CatalogError("No video entries found")
    return Catalog(
        Meta(
            str(pd["title"]) if pd.get("title") else None,
            str(pd["slug"]) if pd.get("slug") else None,
            str(root["buildId"]) if root.get("buildId") else None,
        ),
        sorted(vids.values(), key=lambda v: (v.video_type, v.title, v.guid)),
    )
