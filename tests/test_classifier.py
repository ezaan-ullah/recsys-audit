import asyncio

from audit.classifier import TriggerClassifier, Verdict
from audit.gemini import GeminiResult, RateLimiter
from audit.policy import Keywords, compile_terms


def kw():
    return Keywords(compile_terms(["sad"]), compile_terms(["suicide"]))


class FakeGemini:
    model = "fake-model"

    def __init__(self, result: GeminiResult, delay: float = 0.0):
        self.result, self.delay, self.calls = result, delay, 0

    async def generate_json(self, **_):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.result


def meta(vid="aaaaaaaaaaa", title="a sad day"):
    return {"video_id": vid, "title": title, "description": "", "tags": []}


def run(coro):
    return asyncio.run(coro)


def test_model_label_wins_and_is_cached(tmp_path):
    g = FakeGemini(GeminiResult("ok", {"label": "neutral", "language": "en", "rationale": "x"}))
    c = TriggerClassifier(kw(), g, "prompt", tmp_path / "cache.jsonl")
    v = run(c.classify(meta(), 2.5))
    assert v.source == "gemini" and v.label == "neutral" and not v.adjacent and v.kw_adjacent == "sad"
    c2 = TriggerClassifier(kw(), g, "prompt", tmp_path / "cache.jsonl")      # reloads from disk
    assert run(c2.classify(meta(), 2.5)).source == "cache" and g.calls == 1
    c3 = TriggerClassifier(kw(), g, "a different prompt", tmp_path / "cache.jsonl")
    assert run(c3.classify(meta(), 2.5)).source == "gemini"                  # cache is keyed by prompt


def test_timeout_falls_back_to_keywords_and_late_answer_is_cached(tmp_path):
    g = FakeGemini(GeminiResult("ok", {"label": "adjacent", "language": "en", "rationale": "x"}), delay=0.2)
    c = TriggerClassifier(kw(), g, "prompt", tmp_path / "cache.jsonl")

    async def go():
        v = await c.classify(meta(), 0.05)
        await asyncio.sleep(0.3)
        return v, await c.classify(meta(), 0.05)
    first, second = run(go())
    assert first.source == "keyword_timeout" and first.adjacent        # keyword 'sad' decides
    assert second.source == "cache" and second.label == "adjacent"


def test_safety_block_counts_as_excluded(tmp_path):
    c = TriggerClassifier(kw(), FakeGemini(GeminiResult("blocked", detail="SAFETY")), "p", tmp_path / "c.jsonl")
    v = run(c.classify(meta(title="nothing here"), 2.5))
    assert v.label == "blocked" and v.excluded and not v.adjacent


def test_keyword_exclude_short_circuits(tmp_path):
    g = FakeGemini(GeminiResult("ok", {"label": "neutral", "language": "en", "rationale": "x"}))
    c = TriggerClassifier(kw(), g, "p", tmp_path / "c.jsonl")
    v = run(c.classify(meta(title="sad suicide"), 2.5))
    assert v.source == "keyword_exclude" and v.excluded and g.calls == 0


def test_ratelimit_error_and_nokey_fall_back(tmp_path):
    for status, source in (("ratelimited", "keyword_ratelimited"), ("error", "keyword_error"),
                           ("invalid", "keyword_error")):
        c = TriggerClassifier(kw(), FakeGemini(GeminiResult(status)), "p", tmp_path / f"{status}.jsonl")
        assert run(c.classify(meta(), 2.5)).source == source
    c = TriggerClassifier(kw(), None, "p", tmp_path / "none.jsonl")
    assert run(c.classify(meta(), 2.5)).source == "keyword_nokey"


def test_unknown_label_is_rejected(tmp_path):
    g = FakeGemini(GeminiResult("ok", {"label": "maybe", "language": "en", "rationale": "x"}))
    c = TriggerClassifier(kw(), g, "p", tmp_path / "c.jsonl")
    assert run(c.classify(meta(), 2.5)).source == "keyword_error"


def test_verdict_semantics():
    assert Verdict("adjacent", "gemini").adjacent
    assert not Verdict("none", "keyword_timeout").adjacent
    assert Verdict("none", "keyword_timeout", kw_adjacent="sad").adjacent
    assert Verdict("harmful", "cache").excluded


def test_rate_limiter_rpm_and_rpd(tmp_path):
    async def go():
        lim = RateLimiter(rpm=2, rpd=3, usage_path=tmp_path / "usage.json")
        got = [await lim.acquire("m", 0.05) for _ in range(3)]       # third exceeds rpm within the window
        lim2 = RateLimiter(rpm=100, rpd=3, usage_path=tmp_path / "usage.json")
        more = [await lim2.acquire("m", 0.05) for _ in range(2)]     # rpd is shared via the usage file
        return got, more, lim2.used_today("m")
    got, more, used = run(go())
    assert got == [True, True, False]
    assert more == [True, False] and used == 3
