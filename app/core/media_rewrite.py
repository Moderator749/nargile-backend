"""Answer stored local media URLs with the address the request came in on.

The catalogue was imported with absolute photo URLs on this machine
(``http://127.0.0.1:8000/media/products/…``). Served through a tunnel or a
reverse proxy, those addresses point at the visitor's own computer. Rather than
rewriting the database for every public address the API is given — a quick
tunnel gets a new one on each start — JSON responses swap the local media
prefix for the request's own origin on the way out.

Only ``<local origin>/media/`` is replaced, only in JSON bodies, and only when
the request did not arrive on a local origin, so local development sees the
stored data byte for byte. The request's origin is what uvicorn reports after
its proxy-header handling (X-Forwarded-Proto / Host from a trusted proxy such
as cloudflared on 127.0.0.1).
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def request_origin(scope: Scope) -> str | None:
    host = Headers(scope=scope).get("host")
    if not host:
        return None
    return f"{scope.get('scheme', 'http')}://{host}"


class LocalMediaRewriteMiddleware:
    def __init__(self, app: ASGIApp, local_origins: list[str]) -> None:
        self.app = app
        self.local = [o.rstrip("/") for o in local_origins]
        self.needles = [f"{o}/media/".encode() for o in self.local]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        origin = request_origin(scope) if scope["type"] == "http" else None
        if origin is None or origin.rstrip("/") in self.local:
            await self.app(scope, receive, send)
            return
        replacement = f"{origin.rstrip('/')}/media/".encode()

        start: Message | None = None
        chunks: list[bytes] = []
        rewriting = False

        async def wrapped(message: Message) -> None:
            nonlocal start, rewriting
            if message["type"] == "http.response.start":
                content_type = Headers(raw=message.get("headers", [])).get("content-type", "")
                rewriting = content_type.startswith("application/json")
                if not rewriting:
                    await send(message)
                else:
                    start = message
                return
            if message["type"] != "http.response.body" or not rewriting:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            for needle in self.needles:
                body = body.replace(needle, replacement)
            assert start is not None
            headers = MutableHeaders(raw=list(start.get("headers", [])))
            headers["content-length"] = str(len(body))
            await send({**start, "headers": headers.raw})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)
