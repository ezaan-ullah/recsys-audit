"""Gemini wrapper shared by the live trigger and the offline outcome labeler.

Every call returns a GeminiResult with an explicit status, so callers can log exactly
why a decision did not come from the model (rate limit, safety block, error).
"""
import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Free-tier daily quotas reset at midnight Pacific time.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")
SAFETY_CATEGORIES = ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                     "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
BLOCK_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT"}


class RateLimiter:
    """Sliding-window requests/minute plus a persisted requests/day counter per model."""

    def __init__(self, rpm: int, rpd: int, usage_path: Path):
        self.rpm, self.rpd = int(rpm), int(rpd)
        self.usage_path = Path(usage_path)
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0

    def _day(self) -> str:
        return datetime.now(QUOTA_TZ).date().isoformat()

    def _usage(self) -> dict:
        try:
            return json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def used_today(self, model: str) -> int:
        return self._usage().get(self._day(), {}).get(model, 0)

    def _record(self, model: str) -> None:
        usage = self._usage()
        day = usage.setdefault(self._day(), {})
        day[model] = day.get(model, 0) + 1
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_path.write_text(json.dumps(usage, indent=1), encoding="utf-8")

    def cooldown(self, seconds: float) -> None:
        self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)

    async def _acquire(self, model: str) -> bool:
        async with self._lock:
            while True:
                if self.used_today(model) >= self.rpd:
                    return False
                now = time.monotonic()
                while self._times and now - self._times[0] >= 60:
                    self._times.popleft()
                if now < self._cooldown_until:
                    wait = self._cooldown_until - now
                elif len(self._times) >= self.rpm:
                    wait = 60 - (now - self._times[0]) + 0.01
                else:
                    self._times.append(now)
                    self._record(model)
                    return True
                await asyncio.sleep(wait)

    async def acquire(self, model: str, timeout_s: float | None = None) -> bool:
        """True if a request slot was granted within timeout_s (None = wait as long as needed)."""
        if timeout_s is not None and timeout_s <= 0:
            return False
        try:
            return await asyncio.wait_for(self._acquire(model), timeout_s)
        except asyncio.TimeoutError:
            return False


@dataclass(frozen=True)
class GeminiResult:
    status: str                 # ok | blocked | ratelimited | error | invalid
    data: dict | None = None
    detail: str | None = None
    latency_s: float = 0.0


class GeminiClient:
    def __init__(self, api_key: str, model: str, thinking_budget: int | None, limiter: RateLimiter,
                 timeout_s: float = 30.0):
        from google import genai
        from google.genai import types
        self._types = types
        self.model = model
        self.thinking_budget = thinking_budget
        self.limiter = limiter
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))

    def _config(self, system: str, schema: dict):
        t = self._types
        kw = dict(system_instruction=system, temperature=0, response_mime_type="application/json",
                  response_json_schema=schema,
                  safety_settings=[t.SafetySetting(category=c, threshold="BLOCK_NONE") for c in SAFETY_CATEGORIES])
        if self.thinking_budget is not None:
            kw["thinking_config"] = t.ThinkingConfig(thinking_budget=int(self.thinking_budget))
        return t.GenerateContentConfig(**kw)

    def _parts(self, parts: list):
        """str -> text part; bytes -> JPEG image part."""
        return [self._types.Part.from_bytes(data=p, mime_type="image/jpeg") if isinstance(p, bytes) else p
                for p in parts]

    async def generate_json(self, *, system: str, parts: list, schema: dict,
                            acquire_timeout_s: float | None = None) -> GeminiResult:
        from google.genai import errors
        if not await self.limiter.acquire(self.model, acquire_timeout_s):
            return GeminiResult("ratelimited", detail="local limiter (rpm/rpd)")
        t0 = time.monotonic()
        try:
            resp = await self.client.aio.models.generate_content(
                model=self.model, contents=self._parts(parts), config=self._config(system, schema))
        except errors.APIError as e:
            if e.code == 429:
                self.limiter.cooldown(30)
                return GeminiResult("ratelimited", detail=f"429: {e.message}", latency_s=time.monotonic() - t0)
            return GeminiResult("error", detail=f"{e.code}: {e.message}", latency_s=time.monotonic() - t0)
        except Exception as e:  # timeouts, network
            return GeminiResult("error", detail=f"{type(e).__name__}: {e}", latency_s=time.monotonic() - t0)
        latency = time.monotonic() - t0

        pf = getattr(resp, "prompt_feedback", None)
        if pf is not None and getattr(pf, "block_reason", None):
            return GeminiResult("blocked", detail=f"prompt: {pf.block_reason}", latency_s=latency)
        cands = resp.candidates or []
        if not cands:
            return GeminiResult("blocked", detail="no candidates", latency_s=latency)
        fr = getattr(cands[0].finish_reason, "name", str(cands[0].finish_reason))
        if fr in BLOCK_FINISH:
            return GeminiResult("blocked", detail=f"finish: {fr}", latency_s=latency)
        try:
            data = json.loads(resp.text)
        except (TypeError, ValueError):
            return GeminiResult("invalid", detail=(resp.text or "")[:200], latency_s=latency)
        return GeminiResult("ok", data=data, latency_s=latency)
