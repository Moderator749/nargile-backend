"""Answer ``/path/`` as ``/path``, without a redirect.

Starlette's own fix for a path that differs from a route only by its trailing
slash is a 307 to the other spelling, with an absolute ``Location`` built from
the request's ``Host``. Behind a proxy that is the API's own address: the
storefront's ``/api/backend/…`` rewrite (Vercel → ``http://<droplet>:8000``)
handed the browser ``http://<droplet>/api/v1/products/``, which an HTTPS page
refuses as mixed content. Next.js, for its part, strips the trailing slash
before it proxies, so the two redirects can never agree.

So the app is built with ``redirect_slashes=False``, routes are declared
without a trailing slash, and this middleware drops one from the request path
before routing: both spellings reach the route, and no redirect is ever sent.
"""

from starlette.types import ASGIApp, Receive, Scope, Send


class StripTrailingSlashMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and len(scope["path"]) > 1 and scope["path"].endswith("/"):
            scope = {**scope, "path": scope["path"].rstrip("/") or "/"}
            if raw_path := scope.get("raw_path"):
                scope["raw_path"] = raw_path.rstrip(b"/") or b"/"
        await self.app(scope, receive, send)
