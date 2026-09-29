"""Live events for the web UI: a small in-process pub/sub hub and its Server-Sent Events stream.

Event types (the SSE `event:` field; `data:` is JSON):
  ready            first message of every stream: {"version", "last_event_id", "server_time"}
  run_started      {"run_id", "started_at", "searches"}
  run_finished     the RunSummary of the pass
  deal_found       {"ad_id", "search_name", "verdict", "action", "score", "card": DealCard}
  health_alert     {"kind", "text", "at"}
  settings_changed {"sections": [...], "keys": [...]}
  searches_changed {"count", "ids"}
  deal_updated     {"ad_id", "card": DealCard}
  monitor_paused / monitor_resumed   {"paused": bool}
  job_progress     {"id", "kind", "stage", "text_ru", "ad_id"} (a step of a running job)
  job_finished     a Job (see jobs.py)
  run_progress     {"run_id", "index", "total", "search_name"} (search N of M started)
  log              {"time", "level", "logger", "message"} (only on /api/v1/logs/stream)
Each event has an increasing `id:`; reconnecting with Last-Event-ID (header or ?last_event_id=)
replays the missed ones still in memory (the last EVENT_HISTORY).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator

from ...models import utcnow

log = logging.getLogger(__name__)

EVENT_HISTORY = 200
QUEUE_SIZE = 500
PING_SECONDS = 15.0
RETRY_MS = 3000

EVENT_TYPES = (
    "ready", "run_started", "run_finished", "deal_found", "health_alert", "settings_changed",
    "searches_changed", "deal_updated", "monitor_paused", "monitor_resumed", "job_progress", "job_finished",
    "run_progress",
)


@dataclass
class Event:
    id: int
    type: str
    data: dict[str, Any]
    at: datetime = field(default_factory=utcnow)

    def as_sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False, default=_json_default)
        return f"id: {self.id}\nevent: {self.type}\ndata: {payload}\n\n"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "data": self.data, "at": self.at.isoformat()}


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


class EventHub:
    """publish() from anywhere (the monitor's hooks, API handlers, other threads);
    subscribe() gives each SSE client its own bounded queue (a slow client drops events,
    it never blocks the monitor)."""

    def __init__(self, history: int = EVENT_HISTORY) -> None:
        self._subscribers: set[asyncio.Queue[Event | None]] = set()
        self._history: deque[Event] = deque(maxlen=history)
        self._next_id = 1
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.closed = False

    @property
    def last_id(self) -> int:
        return self._next_id - 1

    def publish(self, type_: str, data: dict[str, Any] | None = None) -> Event:
        with self._lock:
            event = Event(self._next_id, type_, dict(data or {}))
            self._next_id += 1
            self._history.append(event)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = self._loop
        if loop is not None and running is not loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._deliver, event)
        else:
            self._deliver(event)
        return event

    def _deliver(self, event: Event | None) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.debug("SSE client too slow, event %s dropped", event.id if event else None)

    def subscribe(self) -> asyncio.Queue[Event | None]:
        self._loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event | None]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def since(self, last_id: int) -> list[Event]:
        with self._lock:
            return [e for e in self._history if e.id > last_id]

    def recent(self, limit: int = 50) -> list[Event]:
        with self._lock:
            return list(self._history)[-limit:]

    def close(self) -> None:
        """Server shutdown: every open stream ends (uvicorn would otherwise wait for them)."""
        self.closed = True
        self._deliver(None)

    def close_threadsafe(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self.close)
                return
            except RuntimeError:
                pass
        self.closed = True


async def sse_stream(
    hub: EventHub,
    *,
    last_id: int | None = None,
    is_disconnected: Any = None,
    ready: dict[str, Any] | None = None,
    max_events: int | None = None,
    timeout: float | None = None,
    ping: float = PING_SECONDS,
) -> AsyncIterator[str]:
    """The text/event-stream body. Ends on hub.close(), client disconnect, after `max_events`
    events or `timeout` seconds (both optional — for scripts and tests)."""
    start = last_id if last_id is not None else hub.last_id  # before subscribing: nothing slips through
    queue = hub.subscribe()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout if timeout else None
    sent = 0
    seen = start
    try:
        yield f"retry: {RETRY_MS}\n\n"
        hello = {"last_event_id": hub.last_id, "server_time": utcnow().isoformat(), **(ready or {})}
        yield f"event: ready\ndata: {json.dumps(hello, ensure_ascii=False, default=_json_default)}\n\n"
        for event in hub.since(start):
            yield event.as_sse()
            seen = max(seen, event.id)
            sent += 1
            if max_events and sent >= max_events:
                return
        while not hub.closed:
            wait = ping
            if deadline is not None:
                wait = min(wait, deadline - loop.time())
                if wait <= 0:
                    return
            try:
                item: Event | None = await asyncio.wait_for(queue.get(), timeout=wait)
            except asyncio.TimeoutError:
                if deadline is not None and loop.time() >= deadline:
                    return
                if is_disconnected is not None and await is_disconnected():
                    return
                yield ": ping\n\n"
                continue
            if item is None:
                return
            if item.id <= seen:
                continue
            seen = item.id
            yield item.as_sse()
            sent += 1
            if max_events and sent >= max_events:
                return
    finally:
        hub.unsubscribe(queue)


__all__ = ["EVENT_TYPES", "Event", "EventHub", "sse_stream"]
