"""Real-time video metadata, fetched outside the logged-in browser.

Primary source: YouTube Data API v3 (title, description, tags, duration).
Fallback: oEmbed (title and channel only, no key needed).
Data API results are cached on disk and shared across accounts to save quota.
"""
import asyncio
import json
import re
from pathlib import Path

import httpx

_DUR = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$")


def parse_iso_duration(s: str | None) -> float | None:
    if not s:
        return None
    m = _DUR.match(s)
    if not m:
        return None
    d, h, mi, sec = m.groups()
    total = int(d or 0) * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)
    return total or None


def _empty(video_id: str) -> dict:
    return {"video_id": video_id, "title": None, "description": None, "tags": [],
            "channel_id": None, "channel_title": None, "duration_s": None, "source": "none"}


class MetadataClient:
    API = "https://www.googleapis.com/youtube/v3/videos"
    OEMBED = "https://www.youtube.com/oembed"

    def __init__(self, api_key: str | None, cache_path: Path):
        self.api_key = api_key
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache: dict[str, dict] = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    self.cache[rec["video_id"]] = rec
                except (json.JSONDecodeError, KeyError):
                    continue
        self._inflight: dict[str, asyncio.Future] = {}
        self.client = httpx.AsyncClient(timeout=5.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def get(self, video_id: str) -> dict:
        if video_id in self.cache:
            return self.cache[video_id]
        if video_id in self._inflight:          # another account is fetching it right now
            return await self._inflight[video_id]
        fut = asyncio.ensure_future(self._fetch(video_id))
        self._inflight[video_id] = fut
        try:
            return await fut
        finally:
            self._inflight.pop(video_id, None)

    async def _fetch(self, video_id: str) -> dict:
        if self.api_key:
            rec = await self._from_api(video_id)
            if rec is not None:
                self.cache[video_id] = rec
                with open(self.cache_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                return rec
        rec = await self._from_oembed(video_id)
        if rec is not None:
            self.cache[video_id] = rec        # memory only, so a later API run can upgrade it
            return rec
        return _empty(video_id)

    async def _from_api(self, video_id: str) -> dict | None:
        try:
            r = await self.client.get(self.API, params={
                "part": "snippet,contentDetails", "id": video_id, "key": self.api_key})
            r.raise_for_status()
            items = r.json().get("items", [])
        except (httpx.HTTPError, ValueError):
            return None
        if not items:
            return None
        sn = items[0].get("snippet", {})
        cd = items[0].get("contentDetails", {})
        return {
            "video_id": video_id,
            "title": sn.get("title"),
            "description": sn.get("description"),
            "tags": sn.get("tags", []),
            "channel_id": sn.get("channelId"),
            "channel_title": sn.get("channelTitle"),
            "duration_s": parse_iso_duration(cd.get("duration")),
            "source": "data_api",
        }

    async def _from_oembed(self, video_id: str) -> dict | None:
        try:
            r = await self.client.get(self.OEMBED, params={
                "url": f"https://www.youtube.com/shorts/{video_id}", "format": "json"})
            if r.status_code != 200:
                return None
            j = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        rec = _empty(video_id)
        rec.update(title=j.get("title"), channel_title=j.get("author_name"), source="oembed")
        return rec
