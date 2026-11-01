"""A small per-client-IP request budget for the endpoints anyone on the internet can call."""

from __future__ import annotations

import time

from starlette.types import ASGIApp, Receive, Scope, Send

# path -> (requests, per seconds). Generous for a person connecting a few apps, tight for a script.
LIMITS: dict[str, tuple[int, float]] = {
    "/register": (20, 3600),
    "/authorize": (60, 600),
    "/oauth/callback": (60, 600),
    "/oauth/consent": (60, 600),
    "/token": (120, 600),
    "/revoke": (60, 600),
}
MAX_KEYS = 10_000


class RateLimit:
    def __init__(self, app: ASGIApp, limits: dict[str, tuple[int, float]] | None = None):
        self.app = app
        self.limits = LIMITS if limits is None else limits
        self.windows: dict[tuple[str, str], tuple[float, int]] = {}

    def allow(self, client: str, path: str, now: float) -> bool:
        count, period = self.limits[path]
        key = (client, path)
        start, used = self.windows.get(key, (now, 0))
        if now - start >= period:
            start, used = now, 0
        if used >= count:
            return False
        if len(self.windows) >= MAX_KEYS and key not in self.windows:
            self.windows = {k: v for k, v in self.windows.items() if now - v[0] < self.limits[k[1]][1]}
            if len(self.windows) >= MAX_KEYS:
                return False
        self.windows[key] = (start, used + 1)
        return True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and path in self.limits:
            client = (scope.get("client") or ("unknown", 0))[0]
            if not self.allow(client, path, time.monotonic()):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 429,
                        "headers": [(b"content-type", b"text/plain"), (b"retry-after", b"600")],
                    }
                )
                await send({"type": "http.response.body", "body": b"too many requests; try again later\n"})
                return
        await self.app(scope, receive, send)
