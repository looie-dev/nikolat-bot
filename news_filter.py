"""USD High-Impact economic calendar filter using FairEconomy/ForexFactory feed."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import time
from typing import List, Optional, Tuple
import urllib.request

logger = logging.getLogger(__name__)

NEWS_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


@dataclass(frozen=True)
class NewsEvent:
    title: str
    country: str
    impact: str
    timestamp_sec: int


class NewsFilter:
    """Fetches and caches USD High-Impact events to block entries around news spikes."""

    def __init__(
        self,
        buffer_minutes: int = 15,
        refresh_seconds: int = 1800,
        feed_url: str = NEWS_FEED_URL,
    ):
        self.buffer_minutes = buffer_minutes
        self.refresh_seconds = refresh_seconds
        self.feed_url = feed_url
        self._events: List[NewsEvent] = []
        self._last_fetch: float = 0.0
        self._frozen_for_test: bool = False

    def refresh(self, force: bool = False) -> None:
        if self.buffer_minutes <= 0 or self._frozen_for_test:
            return
        now = time.time()
        if not force and (now - self._last_fetch) < self.refresh_seconds:
            return
        try:
            req = urllib.request.Request(
                self.feed_url,
                headers={"User-Agent": "xauusd-bot/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            parsed: List[NewsEvent] = []
            for item in raw:
                if item.get("country") != "USD" or item.get("impact") != "High":
                    continue
                date_str = item.get("date")
                if not date_str:
                    continue
                dt = datetime.fromisoformat(date_str)
                ts = int(dt.astimezone(timezone.utc).timestamp())
                parsed.append(
                    NewsEvent(
                        title=str(item.get("title", "USD High Impact")),
                        country="USD",
                        impact="High",
                        timestamp_sec=ts,
                    )
                )
            self._events = parsed
            self._last_fetch = now
            logger.info("Loaded %d USD High-Impact news events", len(parsed))
        except Exception as exc:
            self._last_fetch = now  # Back off until next interval
            logger.warning("Failed to fetch economic calendar: %s", exc)

    def check_blocked(self, now_sec: Optional[int] = None) -> Tuple[bool, Optional[NewsEvent]]:
        """Return (True, event) if now_sec falls within +/- buffer_minutes of a USD High event."""
        if self.buffer_minutes <= 0:
            return False, None
        self.refresh()
        ts = int(now_sec if now_sec is not None else time.time())
        window = self.buffer_minutes * 60
        for ev in self._events:
            if abs(ts - ev.timestamp_sec) <= window:
                return True, ev
        return False, None

    def upcoming_events(self, hours: int = 24, now_sec: Optional[int] = None) -> List[NewsEvent]:
        self.refresh()
        ts = int(now_sec if now_sec is not None else time.time())
        end = ts + hours * 3600
        return sorted(
            [ev for ev in self._events if ts <= ev.timestamp_sec <= end],
            key=lambda e: e.timestamp_sec,
        )

    def set_events_for_test(self, events: List[NewsEvent]) -> None:
        self._events = events
        self._last_fetch = time.time()
        self._frozen_for_test = True
