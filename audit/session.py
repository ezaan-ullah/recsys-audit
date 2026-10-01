"""One account, one session: scroll the Shorts feed (or watch the seed list) and log every item.

Passive only: the bot never likes, comments, shares, follows, subscribes, or searches.
Its only signal to the recommender is how long it stays on each item.

Timeline of one feed item:
  arrival (URL shows a new /shorts/<id>) -> playback starts -> decision
  (ad? / trigger / yoke / content-blind schedule) -> dwell, timed from playback start
  while polling the active player -> advance -> row written, exposure = arrival to next arrival.
"""
import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import dom
from .browser import is_signed_in
from .errors import SessionAbort
from .policy import Decision, Policy

FEED_URL = "https://www.youtube.com/shorts?hl=en"
SHORT_URL = "https://www.youtube.com/shorts/{}?hl=en"
HISTORY_URL = "https://www.youtube.com/feed/history?hl=en"
SHORT_RE = re.compile(r"/shorts/([A-Za-z0-9_-]{11})")
AD_CHANNEL_RE = re.compile(r"^video ad upload channel", re.IGNORECASE)
POLL_S = 0.5
PLAY_START_TIMEOUT_S = 5.0
ADVANCE_WAIT_S = 6.0
FRAME_AT_S = (1.0, 2.5)        # capture player frames at these watched-seconds marks


class Clock:
    """Real time. Tests substitute a virtual clock."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(s)


@dataclass
class SessionSpec:
    account_id: str
    group: str | None
    pair: str | None
    phase: str
    mode: str                       # neutral | treatment | yoked | seed
    session_no: int
    attempt: int
    n_items: int                    # organic items (ads not counted)
    run_id: str
    fingerprint: str
    out_path: Path
    expected_datasync: str | None = None
    require_identity: bool = False
    seed_ids: tuple = ()


@dataclass
class Runtime:
    policy: Policy
    pair_rng: random.Random         # shared stream: identical draws for both partners
    ad_rng: random.Random           # per-account stream for ad skips
    meta: object                    # MetadataClient
    classifier: object              # TriggerClassifier
    media: object | None = None     # MediaStore
    yoke: object | None = None      # YokeLink: written by treatment, read by yoked control
    partner_timeout_s: float = 240.0
    check_history: bool = True
    clock: Clock = field(default_factory=Clock)
    background: set = field(default_factory=set)    # fire-and-forget tasks (thumbnail downloads)

    def keep_thumbnail(self, vid: str) -> None:
        """Save the thumbnail for labeling, without delaying the session."""
        if self.media is not None:
            task = asyncio.ensure_future(self.media.thumbnail(vid))
            self.background.add(task)
            task.add_done_callback(self.background.discard)


def _short_id(url: str | None) -> str | None:
    m = SHORT_RE.search(url or "")
    return m.group(1) if m else None


def _check_blocked(url: str) -> None:
    if "accounts.google.com" in url:
        raise SessionAbort("signed_out", url)
    if "/sorry/" in url:
        raise SessionAbort("blocked", url)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _Watch:
    """What the active player did while the bot stayed on one item."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.polls = self.found = self.playing = self.loops = 0
        self.watched_s = 0.0
        self.player_duration = self.muted = self.volume = None
        self.ad_signal = self.snippet = None
        self.interstitial: list[str] = []
        self.started_at: float | None = None
        self._last_ct = self._last_t = None

    def update(self, st: dict) -> None:
        now = self.clock.now()
        self.polls += 1
        if st.get("is_ad") and not self.ad_signal:
            self.ad_signal = st.get("ad_signal") or "dom"
        for t in st.get("interstitial") or []:
            if t not in self.interstitial:
                self.interstitial.append(t)
        self.snippet = self.snippet or st.get("snippet")
        if not st.get("video"):
            self._last_ct = None
            return
        self.found += 1
        if st.get("duration"):
            self.player_duration = float(st["duration"])
        self.muted, self.volume = st.get("muted"), st.get("volume")
        ct = float(st.get("currentTime") or 0.0)
        playing = not st.get("paused", True)
        self.playing += playing
        if self.started_at is None and playing and ct > 0.05:
            self.started_at = now
        if self._last_ct is not None:
            limit = (now - self._last_t) + 0.25          # never credit more than wall time
            d = ct - self._last_ct
            if d >= 0:
                self.watched_s += min(d, limit)
            elif self.player_duration and self._last_ct > self.player_duration / 2 > ct:
                self.loops += 1
                self.watched_s += min(self.player_duration - self._last_ct + ct, limit)
        self._last_ct, self._last_t = ct, now


async def _wait_for_short(page, rt: Runtime, timeout_s: float, other_than: str | None = None) -> str:
    deadline = rt.clock.now() + timeout_s
    while True:
        _check_blocked(page.url)
        vid = _short_id(page.url)
        if vid and vid != other_than:
            return vid
        if rt.clock.now() >= deadline:
            break
        await rt.clock.sleep(0.25)
    if await dom.challenge(page):
        raise SessionAbort("blocked", "bot challenge shown")
    raise SessionAbort("no_feed", f"no Short loaded; last url={page.url}")


async def _advance(page, rt: Runtime, current: str) -> tuple[str, int]:
    """Next Short and the number of attempts it took. Keyboard first; mouse wheel as a fallback."""
    vp = page.viewport_size or {"width": 1280, "height": 800}
    for attempt in range(1, 4):
        if attempt < 3:
            await page.keyboard.press("ArrowDown")
        else:
            await page.mouse.move(vp["width"] / 2, vp["height"] / 2)
            await page.mouse.wheel(0, 900)
        deadline = rt.clock.now() + ADVANCE_WAIT_S
        while rt.clock.now() < deadline:
            await rt.clock.sleep(0.25)
            _check_blocked(page.url)
            vid = _short_id(page.url)
            if vid and vid != current:
                return vid, attempt
    if await dom.challenge(page):
        raise SessionAbort("blocked", "bot challenge shown")
    raise SessionAbort("stuck", f"could not advance past {current}")


async def _await_playback(page, rt: Runtime, watch: _Watch, vid: str, t_arr: float) -> float:
    """Time playback of `vid` started; arrival time if it never visibly started."""
    while rt.clock.now() < t_arr + PLAY_START_TIMEOUT_S:
        st = await dom.state(page)
        if _short_id(st.get("url")) in (vid, None):
            watch.update(st)
        if watch.started_at is not None:
            return watch.started_at
        await rt.clock.sleep(0.25)
    return t_arr


async def _dwell(page, rt: Runtime, vid: str, watch: _Watch, until: float, need_frames: bool):
    """Stay on `vid` until `until`, polling the player. Returns (id the page moved to, frames saved)."""
    frames_tried = frames_saved = 0
    while rt.clock.now() < until:
        await rt.clock.sleep(min(POLL_S, max(0.0, until - rt.clock.now())))
        st = await dom.state(page)
        url = st.get("url") or page.url
        _check_blocked(url)
        cur = _short_id(url)
        if cur and cur != vid:                  # page moved on by itself (late keypress, autoscroll)
            return cur, frames_saved
        watch.update(st)
        if need_frames and frames_tried < len(FRAME_AT_S) and watch.watched_s >= FRAME_AT_S[frames_tried]:
            frames_saved += rt.media.save_frame(vid, frames_tried, await dom.grab_frame(page))
            frames_tried += 1
    return None, frames_saved


def _row(spec: SessionSpec, *, position, organic_index, arrived_utc, vid, meta, watch: _Watch,
         decision: Decision, verdict, partner_long, yoke_wait_s, is_ad, ad_signal,
         t_arr, t_play, t_end, left_early, frames_saved) -> dict:
    r2 = lambda x: None if x is None else round(x, 2)
    return {
        "run_id": spec.run_id, "account_id": spec.account_id, "group": spec.group, "pair": spec.pair,
        "phase": spec.phase, "mode": spec.mode, "session": spec.session_no, "attempt": spec.attempt,
        "position": position, "organic_index": organic_index, "arrived_utc": arrived_utc, "video_id": vid,
        "title": meta.get("title"), "channel_id": meta.get("channel_id"), "channel_title": meta.get("channel_title"),
        "tags": meta.get("tags") or [], "duration_s": meta.get("duration_s"), "player_duration_s": r2(watch.player_duration),
        "category_id": meta.get("category_id"), "audio_language": meta.get("audio_language"),
        "meta_source": meta.get("source"),
        "is_ad": is_ad, "ad_signal": ad_signal,
        "trigger_label": verdict.label if verdict else None,
        "trigger_source": verdict.source if verdict else None,
        "trigger_latency_s": r2(verdict.latency_s) if verdict else None,
        "kw_adjacent": verdict.kw_adjacent if verdict else None,
        "kw_exclude": verdict.kw_exclude if verdict else None,
        "partner_long": partner_long, "yoke_wait_s": r2(yoke_wait_s),
        "long": decision.long, "reason": decision.reason, "planned_dwell_s": r2(decision.dwell_s),
        "start_delay_s": r2(t_play - t_arr), "playing_started": watch.started_at is not None,
        "dwell_s": r2(t_end - t_play), "watched_s": r2(watch.watched_s), "loops": watch.loops,
        "playing_frac": r2(watch.playing / watch.found) if watch.found else None,
        "active_video_frac": r2(watch.found / watch.polls) if watch.polls else None,
        "muted": watch.muted, "volume": watch.volume,
        "interstitial": watch.interstitial, "interstitial_snippet": watch.snippet,
        "left_early": left_early, "frames_saved": frames_saved,
        "advance_attempts": None, "exposure_s": None,
        "policy_fp": spec.fingerprint,
    }


async def _feed_item(page, spec: SessionSpec, rt: Runtime, vid: str, t_arr: float, arrived_utc: str,
                     position: int, organic: int) -> tuple[dict, str | None]:
    watch = _Watch(rt.clock)
    meta_task = asyncio.ensure_future(rt.meta.get(vid))
    t_play = await _await_playback(page, rt, watch, vid, t_arr)
    meta = await meta_task
    ad_signal = watch.ad_signal or ("channel" if AD_CHANNEL_RE.match(meta.get("channel_title") or "") else None)
    is_ad = ad_signal is not None
    duration = watch.player_duration or meta.get("duration_s")
    verdict = partner_long = yoke_wait = organic_index = None

    if is_ad:   # both arms skip ads; they do not consume the shared draws or the yoke index
        decision = Decision(False, rt.policy.ad_skip(rt.ad_rng), "ad")
    else:
        organic_index = organic
        draw = rt.policy.draw(rt.pair_rng)
        if spec.mode == "treatment":
            remaining = rt.policy.deadline - (rt.clock.now() - t_play)
            verdict = await rt.classifier.classify(meta, remaining)
            decision = rt.policy.decide("treatment", draw, adjacent=verdict.adjacent,
                                        excluded=verdict.excluded, duration_s=duration)
            if rt.yoke is not None:
                rt.yoke.publish(organic_index, decision.long)
        else:
            if spec.mode == "yoked":
                t0 = rt.clock.now()
                partner_long = await rt.yoke.receive(organic_index, rt.partner_timeout_s)
                yoke_wait = rt.clock.now() - t0
            if rt.policy.wants_long(spec.mode, draw, partner_long):
                # The only content check outside treatment: never linger on an excluded item.
                verdict = await rt.classifier.classify(meta, rt.policy.deadline)
            decision = rt.policy.decide(spec.mode, draw, excluded=bool(verdict and verdict.excluded),
                                        duration_s=duration, partner_long=partner_long)

    if not is_ad:
        rt.keep_thumbnail(vid)
    need_frames = rt.media is not None and not is_ad and not rt.media.frame_paths(vid)
    moved_to, frames_saved = await _dwell(page, rt, vid, watch, t_play + decision.dwell_s, need_frames)
    row = _row(spec, position=position, organic_index=organic_index, arrived_utc=arrived_utc, vid=vid,
               meta=meta, watch=watch, decision=decision, verdict=verdict, partner_long=partner_long,
               yoke_wait_s=yoke_wait, is_ad=is_ad, ad_signal=ad_signal, t_arr=t_arr, t_play=t_play,
               t_end=rt.clock.now(), left_early=moved_to is not None,
               frames_saved=frames_saved if need_frames else None)
    return row, moved_to


async def _run_feed(page, spec: SessionSpec, rt: Runtime, write) -> None:
    await page.goto(FEED_URL, wait_until="domcontentloaded")
    vid = await _wait_for_short(page, rt, 20)
    t_arr, arrived_utc = rt.clock.now(), _utc()
    organic = position = 0
    max_positions = spec.n_items * 2 + 10      # guard against a feed that is all ads
    while True:
        row, moved_to = await _feed_item(page, spec, rt, vid, t_arr, arrived_utc, position, organic)
        organic += row["organic_index"] is not None
        position += 1
        if organic >= spec.n_items or position >= max_positions:
            row["exposure_s"] = round(rt.clock.now() - t_arr, 2)
            write(row)
            if organic < spec.n_items:
                raise SessionAbort("ad_flood", f"only {organic} organic items in {position} positions")
            return
        try:
            if moved_to:
                nxt, attempts = moved_to, 0
            else:
                nxt, attempts = await _advance(page, rt, vid)
            t_next, next_utc = rt.clock.now(), _utc()
            row.update(advance_attempts=attempts, exposure_s=round(t_next - t_arr, 2))
        finally:
            write(row)    # even if advancing failed, the item was shown
        vid, t_arr, arrived_utc = nxt, t_next, next_utc


async def _run_seed(page, spec: SessionSpec, rt: Runtime, write) -> None:
    """Watch each pre-registered seed Short to completion, by direct URL (no search)."""
    for i, sid in enumerate(spec.seed_ids):
        await page.goto(SHORT_URL.format(sid), wait_until="domcontentloaded")
        t_arr, arrived_utc = rt.clock.now(), _utc()
        try:
            vid = await _wait_for_short(page, rt, 20)
        except SessionAbort as e:
            if e.status != "no_feed":
                raise
            vid = None
        draw = rt.policy.draw(rt.pair_rng)
        watch = _Watch(rt.clock)
        meta = await rt.meta.get(sid)
        if vid != sid:
            decision = Decision(False, 0.0, "seed_unavailable")
            t_play = t_arr
        else:
            t_play = await _await_playback(page, rt, watch, sid, t_arr)
            decision = Decision(True, rt.policy.seed_dwell(watch.player_duration or meta.get("duration_s"), draw), "seed")
        rt.keep_thumbnail(sid)
        need_frames = vid == sid and rt.media is not None and not rt.media.frame_paths(sid)
        frames_saved = None
        if vid == sid:
            _, frames_saved = await _dwell(page, rt, sid, watch, t_play + decision.dwell_s, need_frames)
        row = _row(spec, position=i, organic_index=i, arrived_utc=arrived_utc, vid=sid, meta=meta, watch=watch,
                   decision=decision, verdict=None, partner_long=None, yoke_wait_s=None, is_ad=False,
                   ad_signal=None, t_arr=t_arr, t_play=t_play, t_end=rt.clock.now(), left_early=False,
                   frames_saved=frames_saved if need_frames else None)
        row["exposure_s"] = round(rt.clock.now() - t_arr, 2)
        write(row)


async def _preflight(page, spec: SessionSpec, rt: Runtime, info: dict) -> None:
    """Confirm the right account is signed in, history is on, and no bot challenge is up."""
    await page.goto(HISTORY_URL, wait_until="domcontentloaded")
    _check_blocked(page.url)
    cfg, deadline = None, rt.clock.now() + 15
    while rt.clock.now() < deadline:
        cfg = await dom.ytcfg(page)
        if cfg and cfg.get("logged_in") is not None:
            break
        await rt.clock.sleep(0.5)
    if not cfg:
        raise SessionAbort("check_failed", "could not read ytcfg; has YouTube's front end changed?")
    info.update(datasync_id=cfg.get("datasync_id"), hl=cfg.get("hl"), gl=cfg.get("gl"),
                chrome_version=await dom.chrome_version(page))
    if not cfg.get("logged_in"):
        raise SessionAbort("signed_out", "YouTube reports LOGGED_IN=false")
    if spec.expected_datasync and cfg.get("datasync_id") != spec.expected_datasync:
        raise SessionAbort("wrong_account", f"signed-in account {cfg.get('datasync_id')} != recorded {spec.expected_datasync}")
    if spec.require_identity and not spec.expected_datasync:
        raise SessionAbort("identity_unknown", "no datasync_id recorded for this account; run login.py")
    if await dom.challenge(page):
        raise SessionAbort("blocked", "bot challenge shown")
    if rt.check_history:
        await rt.clock.sleep(2.0)      # let the history page render its notice
        if await dom.history_off(page):
            raise SessionAbort("history_off", "YouTube watch history is off for this account")


async def run_session(ctx, spec: SessionSpec, rt: Runtime, info: dict) -> None:
    """Run one session, appending rows to spec.out_path. `info` collects session-level facts."""
    if not await is_signed_in(ctx):
        raise SessionAbort("not_signed_in", "no Google session cookie; run login.py for this account")
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await _preflight(page, spec, rt, info)
    with open(spec.out_path, "a", encoding="utf-8") as f:
        def write(row: dict) -> None:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
        try:
            if spec.mode == "seed":
                await _run_seed(page, spec, rt, write)
            else:
                await _run_feed(page, spec, rt, write)
        finally:
            if rt.background:     # let pending thumbnail downloads finish (each has a short timeout)
                await asyncio.wait(list(rt.background), timeout=10)
