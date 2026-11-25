"""Alerts: every alert is logged, stored in `events`, and optionally pushed to a webhook."""

from __future__ import annotations

import logging
import urllib.request
from datetime import datetime
from typing import Callable

from .store import Store

log = logging.getLogger("trader.alerts")

INFO, WARN, ERROR = "INFO", "WARN", "ERROR"
_LEVELS = {INFO: logging.INFO, WARN: logging.WARNING, ERROR: logging.ERROR}


class Alerter:
    def __init__(self, store: Store, clock: Callable[[], datetime], webhook_url: str = "",
                 prefix: str = ""):
        self._store = store
        self._clock = clock
        self._url = webhook_url
        self._prefix = prefix

    def send(self, level: str, kind: str, message: str, payload: dict | None = None) -> None:
        log.log(_LEVELS.get(level, logging.INFO), "[%s] %s", kind, message)
        self._store.add_event(self._clock(), level, kind, {"message": message, **(payload or {})})
        if self._url:
            self._push(f"{self._prefix}{level} {kind}: {message}")

    def _push(self, text: str) -> None:
        """Plain-text POST (works with ntfy.sh and similar). Never raises."""
        try:
            req = urllib.request.Request(self._url, data=text.encode("utf-8"), method="POST")
            urllib.request.urlopen(req, timeout=5).close()
        except Exception as e:  # an alert channel outage must not stop trading logic
            log.warning("alert push failed: %s", e)
