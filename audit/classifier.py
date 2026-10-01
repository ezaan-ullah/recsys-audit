"""Live trigger: is this item mental-health-adjacent, harmful, or neither?

Gemini decides when it answers within the deadline; otherwise the multilingual
keyword lists decide, and the row records which one did (`trigger_source`).
Exclusion is the union of both: a keyword exclude hit, a model `harmful` label, or a
model safety block all count, so the safety rule errs on the side of skipping.
"""
import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .gemini import GeminiResult
from .policy import Keywords

LABELS = ("adjacent", "harmful", "neutral")
SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(LABELS)},
        "language": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["label", "language", "rationale"],
}
MODEL_SOURCES = ("gemini", "cache")


@dataclass(frozen=True)
class Verdict:
    label: str                  # adjacent | harmful | neutral | blocked | none (model did not answer)
    source: str                 # gemini | cache | keyword_exclude | keyword_nokey | keyword_timeout | keyword_ratelimited | keyword_error
    kw_adjacent: str | None = None
    kw_exclude: str | None = None
    latency_s: float = 0.0

    @property
    def from_model(self) -> bool:
        return self.source in MODEL_SOURCES

    @property
    def excluded(self) -> bool:
        return self.kw_exclude is not None or self.label in ("harmful", "blocked")

    @property
    def adjacent(self) -> bool:
        return self.label == "adjacent" if self.from_model else self.kw_adjacent is not None


def item_prompt(meta: dict, max_desc: int = 1500) -> str:
    tags = ", ".join(meta.get("tags") or [])
    return (f"Title: {meta.get('title') or ''}\n"
            f"Channel: {meta.get('channel_title') or ''}\n"
            f"Tags: {tags}\n"
            f"Description: {(meta.get('description') or '')[:max_desc]}")


class TriggerClassifier:
    def __init__(self, keywords: Keywords, gemini, prompt_text: str, cache_path: Path, media=None):
        self.keywords = keywords
        self.gemini = gemini                     # GeminiClient or None
        self.prompt = prompt_text
        self.media = media
        self.key = f"{gemini.model if gemini else None}|{hashlib.sha256(prompt_text.encode()).hexdigest()[:12]}"
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache: dict[str, dict] = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("key") == self.key:
                    self.cache[rec["video_id"]] = rec
        self._inflight: dict[str, asyncio.Task] = {}

    async def classify(self, meta: dict, timeout_s: float) -> Verdict:
        kw_adj, kw_exc = self.keywords.match(meta)
        if kw_exc:
            return Verdict("none", "keyword_exclude", kw_adj, kw_exc)
        vid = meta["video_id"]
        if vid in self.cache:
            return Verdict(self.cache[vid]["label"], "cache", kw_adj, kw_exc)
        if self.gemini is None:
            return Verdict("none", "keyword_nokey", kw_adj, kw_exc)
        task = self._inflight.get(vid)
        if task is None:
            task = asyncio.ensure_future(self._infer(vid, meta, timeout_s))
            self._inflight[vid] = task
            task.add_done_callback(lambda _t, v=vid: self._inflight.pop(v, None))
        try:
            # shield: a late answer still lands in the cache for the next account and for analysis
            res = await asyncio.wait_for(asyncio.shield(task), max(0.0, timeout_s))
        except asyncio.TimeoutError:
            return Verdict("none", "keyword_timeout", kw_adj, kw_exc, timeout_s)
        if res.status in ("ok", "blocked"):
            return Verdict(res.data["label"] if res.status == "ok" else "blocked", "gemini", kw_adj, kw_exc, res.latency_s)
        if res.status == "ratelimited":
            return Verdict("none", "keyword_ratelimited", kw_adj, kw_exc, res.latency_s)
        return Verdict("none", "keyword_error", kw_adj, kw_exc, res.latency_s)

    async def _infer(self, vid: str, meta: dict, acquire_timeout_s: float):
        parts = [item_prompt(meta)]
        if self.media is not None:
            thumb = await self.media.thumbnail(vid)
            if thumb:
                parts.insert(0, thumb)
        res = await self.gemini.generate_json(system=self.prompt, parts=parts, schema=SCHEMA,
                                              acquire_timeout_s=acquire_timeout_s)
        if res.status == "ok" and (res.data or {}).get("label") not in LABELS:
            res = GeminiResult("invalid", detail=str(res.data)[:200], latency_s=res.latency_s)
        if res.status in ("ok", "blocked"):
            rec = {"video_id": vid, "key": self.key,
                   "label": res.data["label"] if res.status == "ok" else "blocked",
                   "language": (res.data or {}).get("language"), "rationale": (res.data or {}).get("rationale"),
                   "detail": res.detail, "latency_s": round(res.latency_s, 3), "had_thumbnail": len(parts) == 2}
            self.cache[vid] = rec
            with open(self.cache_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return res
