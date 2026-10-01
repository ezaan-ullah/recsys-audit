"""Thumbnails and player frames, stored once per video ID for labeling.

Captured at collection time because flagged Shorts are often removed before they can
be labeled. data/ is git-ignored: this directory may hold distressing imagery.
"""
import asyncio
import base64
from pathlib import Path

import httpx

THUMB_URL = "https://i.ytimg.com/vi/{}/hqdefault.jpg"


class MediaStore:
    def __init__(self, media_dir: Path, client: httpx.AsyncClient | None = None):
        self.thumbs = Path(media_dir) / "thumbs"
        self.frames = Path(media_dir) / "frames"
        self.thumbs.mkdir(parents=True, exist_ok=True)
        self.frames.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.AsyncClient(timeout=3.0)
        self._inflight: dict[str, asyncio.Future] = {}

    def thumb_path(self, video_id: str) -> Path:
        return self.thumbs / f"{video_id}.jpg"

    def frame_path(self, video_id: str, n: int) -> Path:
        return self.frames / f"{video_id}_{n}.jpg"

    def frame_paths(self, video_id: str) -> list[Path]:
        return sorted(self.frames.glob(f"{video_id}_*.jpg"))

    async def thumbnail(self, video_id: str) -> bytes | None:
        path = self.thumb_path(video_id)
        if path.exists():
            return path.read_bytes()
        if video_id in self._inflight:
            return await self._inflight[video_id]
        fut = asyncio.ensure_future(self._fetch(video_id))
        self._inflight[video_id] = fut
        try:
            return await fut
        finally:
            self._inflight.pop(video_id, None)

    async def _fetch(self, video_id: str) -> bytes | None:
        try:
            r = await self.client.get(THUMB_URL.format(video_id))
        except httpx.HTTPError:
            return None
        if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
            return None
        self.thumb_path(video_id).write_bytes(r.content)
        return r.content

    def save_frame(self, video_id: str, n: int, data_url: str | None) -> bool:
        if not data_url or not data_url.startswith("data:image/jpeg;base64,"):
            return False
        self.frame_path(video_id, n).write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        return True

    async def close(self) -> None:
        await self.client.aclose()
