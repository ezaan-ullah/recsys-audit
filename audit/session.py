"""One account, one session: scroll the Shorts feed and log every item.

Passive only: the bot never likes, comments, shares, follows, or searches.
Its only signal to the recommender is how long it stays on each item.
"""
import asyncio
import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .browser import is_signed_in
from .metadata import MetadataClient
from .policy import Policy

FEED_URL = "https://www.youtube.com/shorts"
SHORT_RE = re.compile(r"/shorts/([A-Za-z0-9_-]{11})")


class SessionAbort(Exception):
    def __init__(self, status: str, msg: str = ""):
        super().__init__(msg or status)
        self.status = status


def _short_id(url: str) -> str | None:
    m = SHORT_RE.search(url)
    return m.group(1) if m else None


def _check_blocked(url: str) -> None:
    if "accounts.google.com" in url:
        raise SessionAbort("signed_out", url)
    if "/sorry/" in url:
        raise SessionAbort("blocked", url)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


async def _wait_for_short(page, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _check_blocked(page.url)
        vid = _short_id(page.url)
        if vid:
            return vid
        await asyncio.sleep(0.25)
    raise SessionAbort("no_feed", f"no Short loaded; last url={page.url}")


async def _advance(page, current: str) -> str:
    """Move to the next Short. Keyboard first; mouse wheel as a fallback.

    Sponsored items in the feed often don't change the URL; the second
    keypress moves past them, so ads are skipped and never logged.
    """
    vp = page.viewport_size or {"width": 1280, "height": 800}
    for attempt in range(3):
        if attempt < 2:
            await page.keyboard.press("ArrowDown")
        else:
            await page.mouse.move(vp["width"] / 2, vp["height"] / 2)
            await page.mouse.wheel(0, 900)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            _check_blocked(page.url)
            vid = _short_id(page.url)
            if vid and vid != current:
                return vid
    raise SessionAbort("stuck", f"could not advance past {current}")


async def _is_playing(page) -> bool | None:
    try:
        return await page.evaluate(
            "() => [...document.querySelectorAll('video')].some(v => !v.paused && v.readyState >= 2)")
    except Exception:
        return None


async def run_session(ctx, *, account_id: str, group: str | None, phase: str, mode: str,
                      session_no: int, n_items: int, policy: Policy, rng: random.Random,
                      meta: MetadataClient, out_path: Path, fingerprint: str, run_id: str) -> None:
    if not await is_signed_in(ctx):
        raise SessionAbort("not_signed_in", "no Google session cookie; run login.py for this account")

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(FEED_URL, wait_until="domcontentloaded")
    vid = await _wait_for_short(page, 20)
    arrived, arrived_utc = time.monotonic(), _utc()

    with open(out_path, "a", encoding="utf-8") as f:
        for pos in range(n_items):
            m = await meta.get(vid)
            d = policy.decide(mode, m, rng)
            remaining = d.dwell_s - (time.monotonic() - arrived)   # metadata latency counts toward dwell
            if remaining > 0:
                await asyncio.sleep(remaining)
            playing = await _is_playing(page)
            row = {
                "run_id": run_id, "account_id": account_id, "group": group, "phase": phase,
                "mode": mode, "session": session_no, "position": pos, "arrived_utc": arrived_utc,
                "video_id": vid, "title": m.get("title"), "channel_id": m.get("channel_id"),
                "channel_title": m.get("channel_title"), "tags": m.get("tags") or [],
                "duration_s": m.get("duration_s"), "meta_source": m.get("source"),
                "long": d.long, "reason": d.reason, "matched": d.matched,
                "planned_dwell_s": round(d.dwell_s, 2),
                "actual_dwell_s": round(time.monotonic() - arrived, 2),
                "playing": playing, "policy_fp": fingerprint,
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            if pos == n_items - 1:
                break
            vid = await _advance(page, vid)
            arrived, arrived_utc = time.monotonic(), _utc()
