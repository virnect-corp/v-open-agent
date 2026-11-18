"""Bounded per-session video input queue (SPEC 7.5, COM-05).

Only detection/track observations (and, from Stage C, image frames) enter the
queue; auth, alert_sync, ack and health take the control path and never wait
here. When full, the oldest droppable item is dropped; if none is droppable the
new item is rejected. Either way the stream's drop is counted and the next
video item accepted for that stream carries `frame_dropped`.
"""

import asyncio
from collections import Counter, deque
from dataclasses import dataclass

from voa.contracts.observation import ObservationDraft


@dataclass(frozen=True)
class QueuedObservation:
    draft: ObservationDraft
    received_mono_ns: int  # server receipt time; freshness counts queueing delay
    droppable: bool = True


class InputQueue:
    def __init__(self, maxsize: int) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self.dropped: Counter[str] = Counter()
        self.drop_pending: set[str] = set()
        self._items: deque[QueuedObservation] = deque()
        self._ready = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    def offer(
        self, draft: ObservationDraft, *, received_mono_ns: int, droppable: bool = True
    ) -> bool:
        """Enqueue a video draft; False if it was rejected because nothing could be dropped."""
        if len(self._items) >= self.maxsize:
            victim = next((item for item in self._items if item.droppable), None)
            if victim is None:
                self._count_drop(draft.stream_id)
                return False
            self._items.remove(victim)
            self._count_drop(victim.draft.stream_id)
        if draft.stream_id in self.drop_pending:
            self.drop_pending.discard(draft.stream_id)
            draft = draft.model_copy(
                update={"quality_flags": draft.quality_flags | {"frame_dropped"}}
            )
        self._items.append(QueuedObservation(draft, received_mono_ns, droppable))
        self._ready.set()
        return True

    def pop(self) -> QueuedObservation | None:
        return self._items.popleft() if self._items else None

    async def get(self) -> QueuedObservation:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()

    def _count_drop(self, stream_id: str) -> None:
        self.dropped[stream_id] += 1
        self.drop_pending.add(stream_id)
