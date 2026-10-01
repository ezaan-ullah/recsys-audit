"""The session loop against a scripted fake browser, on a virtual clock."""
import asyncio
import base64
import json
import random
from types import SimpleNamespace

import pytest

from audit import dom
from audit.classifier import Verdict
from audit.errors import SessionAbort
from audit.policy import Policy
from audit.session import Runtime, SessionSpec, run_session
from audit.yoke import YokeLink

PCFG = {"skip_dwell_s": [3.0, 5.0], "long_dwell_floor_s": 10, "long_dwell_cap_s": 45,
        "long_dwell_jitter_s": [0.0, 2.0], "neutral_long_prob": 0.3, "decision_deadline_s": 2.5,
        "seed_dwell_cap_s": 90}
FRAME = "data:image/jpeg;base64," + base64.b64encode(b"jpeg").decode()


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += max(0.0, s)
        await asyncio.sleep(0)


def vid(i):
    return f"vid{i:08d}"


class FakePage:
    """A feed of scripted items. ArrowDown moves to the next one; the player plays in real (virtual) time."""

    def __init__(self, items, clock, datasync="D1", history_off=False, lag_presses=0):
        self.items, self.clock = items, clock
        self.datasync, self.history_off = datasync, history_off
        self.i, self.url, self.t_arrive = -1, "about:blank", 0.0
        self.lag_presses = lag_presses          # number of keypresses that are ignored
        self.keyboard = SimpleNamespace(press=self._press)
        self.mouse = SimpleNamespace(move=self._noop, wheel=self._wheel)
        self.viewport_size = {"width": 1280, "height": 800}

    async def _noop(self, *a):
        pass

    def _show(self, i):
        self.i, self.t_arrive = i, self.clock.now()
        self.url = f"https://www.youtube.com/shorts/{self.items[i]['vid']}"

    async def _press(self, key):
        if self.lag_presses:
            self.lag_presses -= 1
            return
        if self.i + 1 < len(self.items):
            self._show(self.i + 1)

    async def _wheel(self, *a):
        await self._press("wheel")

    async def goto(self, url, wait_until=None):
        if "/feed/history" in url:
            self.url = url
        elif "/shorts/" in url and "/shorts?" not in url:
            target = url.split("/shorts/")[1][:11]
            self._show(next(k for k, it in enumerate(self.items) if it["vid"] == target))
        else:
            self._show(0)

    async def evaluate(self, fn, arg=None):
        if fn == dom.YTCFG_FN:
            return {"logged_in": True, "datasync_id": self.datasync, "hl": "en", "gl": "PK"}
        if fn == dom.TEXT_FN:
            return "your watch history is off" if self.history_off else "history"
        if fn == dom.CHROME_FN:
            return "154.0.1"
        if fn == dom.FRAME_FN:
            return FRAME
        if fn == dom.STATE_FN:
            it = self.items[self.i]
            dur = it.get("duration", 20.0)
            ct = (self.clock.now() - self.t_arrive) % dur
            return {"url": self.url, "video": True, "paused": False, "muted": False, "volume": 1.0,
                    "currentTime": ct + 0.1, "duration": dur, "is_ad": it.get("ad", False),
                    "ad_signal": "dom" if it.get("ad") else None,
                    "interstitial": it.get("interstitial", []), "snippet": None}
        raise AssertionError(f"unexpected evaluate: {fn[:40]}")


class FakeCtx:
    def __init__(self, page):
        self.pages = [page]

    async def cookies(self, url):
        return [{"name": "SID"}, {"name": "LOGIN_INFO"}]


class FakeMeta:
    def __init__(self, items):
        self.by_id = {it["vid"]: it for it in items}

    async def get(self, v):
        it = self.by_id[v]
        return {"video_id": v, "title": it.get("title", ""), "channel_title": it.get("channel", "chan"),
                "tags": [], "description": "", "duration_s": it.get("duration", 20.0), "source": "data_api"}


class FakeClassifier:
    def __init__(self):
        self.calls = []

    async def classify(self, meta, timeout_s):
        self.calls.append(meta["video_id"])
        t = meta["title"]
        if "harm" in t:
            return Verdict("harmful", "gemini")
        return Verdict("adjacent" if "sad" in t else "neutral", "gemini")


class FakeMedia:
    def __init__(self):
        self.saved = {}
        self.thumbs = set()

    async def thumbnail(self, v):
        self.thumbs.add(v)
        return b"jpeg"

    def frame_paths(self, v):
        return [k for k in self.saved if k[0] == v]

    def save_frame(self, v, n, data):
        self.saved[(v, n)] = data
        return True


def make(tmp_path, items, mode, *, yoke=None, n_items=None, pair_seed="s", **page_kw):
    clock = FakeClock()
    page = FakePage(items, clock, **page_kw)
    spec = SessionSpec(account_id="a1", group="treatment", pair="p1", phase="treatment", mode=mode,
                       session_no=4, attempt=1, n_items=n_items or sum(not it.get("ad") for it in items),
                       run_id="r1", fingerprint="fp", out_path=tmp_path / f"{mode}.jsonl", expected_datasync="D1",
                       require_identity=True, seed_ids=tuple(it["vid"] for it in items))
    rt = Runtime(policy=Policy(PCFG), pair_rng=random.Random(pair_seed), ad_rng=random.Random("ads"),
                 meta=FakeMeta(items), classifier=FakeClassifier(), media=FakeMedia(), yoke=yoke,
                 partner_timeout_s=5, clock=clock)
    return FakeCtx(page), spec, rt


def rows(path):
    return [json.loads(l) for l in path.read_text().splitlines()]


def feed(n=6, sad=(1, 4), ads=(3,), harm=()):
    out, k = [], 0
    for i in range(n + len(ads)):
        if i in ads:
            out.append({"vid": vid(100 + i), "ad": True, "channel": "Video ad upload channel for 123"})
        else:
            title = "a sad song" if k in sad else ("self harm" if k in harm else "cooking pasta")
            out.append({"vid": vid(i), "title": title, "duration": 15.0})
            k += 1
    return out


def test_treatment_lingers_on_adjacent_and_skips_ads(tmp_path):
    items = feed()
    ctx, spec, rt = make(tmp_path, items, "treatment", yoke=YokeLink())
    info = {}
    asyncio.run(run_session(ctx, spec, rt, info))
    rs = rows(spec.out_path)
    assert info["chrome_version"] == "154.0.1"
    assert len(rs) == len(items)
    ad = [r for r in rs if r["is_ad"]]
    org = [r for r in rs if not r["is_ad"]]
    assert len(ad) == 1 and ad[0]["reason"] == "ad" and ad[0]["organic_index"] is None
    assert [r["organic_index"] for r in org] == list(range(6))
    assert [r["long"] for r in org] == [False, True, False, False, True, False]
    for r in org:
        assert r["trigger_source"] == "gemini"
        if r["long"]:
            assert 15 <= r["dwell_s"] <= 17.6 and r["watched_s"] >= 14
        else:
            assert 3 <= r["dwell_s"] <= 5.6
        assert r["exposure_s"] >= r["dwell_s"]
        assert r["playing_started"] and r["muted"] is False and r["active_video_frac"] == 1.0
    assert all(r["frames_saved"] == 2 for r in org)           # one capture per new video ID
    assert rt.media.thumbs == {r["video_id"] for r in org}    # thumbnails kept for labeling, ads excluded
    assert rs[-1]["advance_attempts"] is None and rs[0]["advance_attempts"] == 1
    assert rt.yoke.published == 6


def test_yoked_control_mirrors_partner_and_checks_exclusion(tmp_path):
    t_items = feed(sad=(1, 4))
    c_items = [dict(it, vid="c" + it["vid"][1:]) for it in feed(sad=(), harm=(4,), ads=(1, 5))]
    link = YokeLink()
    t_ctx, t_spec, t_rt = make(tmp_path, t_items, "treatment", yoke=link)
    c_ctx, c_spec, c_rt = make(tmp_path, c_items, "yoked", yoke=link)

    async def both():
        async def treat():
            try:
                await run_session(t_ctx, t_spec, t_rt, {})
            finally:
                link.close("ok")
        await asyncio.gather(treat(), run_session(c_ctx, c_spec, c_rt, {}))
    asyncio.run(both())
    c_org = [r for r in rows(c_spec.out_path) if not r["is_ad"]]
    assert [r["partner_long"] for r in c_org] == [False, True, False, False, True, False]
    assert c_org[1]["long"] and c_org[1]["reason"] == "yoked_long"
    assert not c_org[4]["long"] and c_org[4]["reason"] == "excluded"    # partner lingered, but this item is harmful
    # Content is checked only where the partner lingered.
    assert c_rt.classifier.calls == [c_org[1]["video_id"], c_org[4]["video_id"]]
    # Shared pair stream: identical skip lengths at every organic position.
    t_org = [r for r in rows(t_spec.out_path) if not r["is_ad"]]
    for t, c in zip(t_org, c_org):
        if not t["long"] and not c["long"] and c["reason"] != "excluded":
            assert t["planned_dwell_s"] == c["planned_dwell_s"]


def test_control_stops_when_partner_aborts(tmp_path):
    link = YokeLink()
    link.publish(0, False)
    link.close("stuck")
    ctx, spec, rt = make(tmp_path, feed(), "yoked", yoke=link)
    with pytest.raises(SessionAbort) as e:
        asyncio.run(run_session(ctx, spec, rt, {}))
    assert e.value.status == "partner_aborted"
    assert len(rows(spec.out_path)) == 1


def test_neutral_mode_never_lingers_on_excluded(tmp_path):
    items = [{"vid": vid(i), "title": "self harm", "duration": 15.0} for i in range(30)]
    ctx, spec, rt = make(tmp_path, items, "neutral")
    asyncio.run(run_session(ctx, spec, rt, {}))
    rs = rows(spec.out_path)
    assert not any(r["long"] for r in rs)
    assert any(r["reason"] == "excluded" for r in rs)
    assert all(r["reason"] in ("neutral_skip", "excluded") for r in rs)


def test_wrong_account_and_history_off_abort(tmp_path):
    ctx, spec, rt = make(tmp_path, feed(), "neutral", datasync="OTHER")
    with pytest.raises(SessionAbort) as e:
        asyncio.run(run_session(ctx, spec, rt, {}))
    assert e.value.status == "wrong_account"
    ctx, spec, rt = make(tmp_path, feed(), "neutral", history_off=True)
    with pytest.raises(SessionAbort) as e:
        asyncio.run(run_session(ctx, spec, rt, {}))
    assert e.value.status == "history_off"


def test_missed_keypress_is_retried_and_counted(tmp_path):
    ctx, spec, rt = make(tmp_path, feed(n=3, sad=(), ads=()), "neutral", lag_presses=1)
    asyncio.run(run_session(ctx, spec, rt, {}))
    rs = rows(spec.out_path)
    assert rs[0]["advance_attempts"] == 2 and rs[0]["exposure_s"] >= rs[0]["dwell_s"] + 6
    assert rs[1]["advance_attempts"] == 1


def test_seed_session_watches_to_completion(tmp_path):
    items = [{"vid": vid(i), "title": "sad seed", "duration": 25.0} for i in range(3)]
    ctx, spec, rt = make(tmp_path, items, "seed")
    asyncio.run(run_session(ctx, spec, rt, {}))
    rs = rows(spec.out_path)
    assert [r["video_id"] for r in rs] == [it["vid"] for it in items]
    assert all(r["reason"] == "seed" and r["long"] and r["dwell_s"] >= 25 for r in rs)
    assert all(r["frames_saved"] == 2 for r in rs)
    assert rt.classifier.calls == []
