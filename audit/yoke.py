"""Live coupling between a treatment account and its yoked control.

The treatment account publishes, for each organic position k, whether it lingered.
The control lingers at its own position k exactly when its partner did, so both arms
get the same number and placement of long dwells without the control ever choosing
by content.
"""
import asyncio

from .errors import SessionAbort


class YokeLink:
    def __init__(self):
        self._decisions: dict[int, bool] = {}
        self._closed: str | None = None
        self._changed = asyncio.Event()

    def publish(self, index: int, long: bool) -> None:
        self._decisions[index] = bool(long)
        self._changed.set()

    def close(self, status: str) -> None:
        """Called when the treatment session ends, normally or not."""
        if self._closed is None:
            self._closed = status
            self._changed.set()

    @property
    def closed(self) -> str | None:
        """The partner's final status once its session has ended, else None."""
        return self._closed

    @property
    def published(self) -> int:
        return len(self._decisions)

    async def receive(self, index: int, timeout_s: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            if index in self._decisions:
                return self._decisions[index]
            if self._closed is not None:
                raise SessionAbort("partner_aborted",
                                   f"treatment partner ended ({self._closed}) before item {index}")
            self._changed.clear()
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SessionAbort("partner_timeout", f"no partner decision for item {index} in {timeout_s:.0f}s")
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except asyncio.TimeoutError:
                pass
