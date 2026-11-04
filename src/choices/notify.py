"""Ping a human when the pipeline needs one (Slack or Discord incoming webhook).

The review gate only works if someone notices it. One POST to a webhook puts
"Day 4 is ready: approve or reject" on the reviewer's phone. Failures are
logged and swallowed, because a notification must never break a run.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


def notify(webhook_env: str, text: str, client: httpx.Client | None = None) -> bool:
    url = os.environ.get(webhook_env, "").strip()
    if not url:
        return False
    try:
        c = client or httpx.Client(timeout=10)
        # Slack reads "text", Discord reads "content"; each ignores the other's key.
        r = c.post(url, json={"text": text, "content": text[:1900]})
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("notify failed: %s", e)
        return False
