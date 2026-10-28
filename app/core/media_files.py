"""/media: the uploads, and a way on for an address from before WebP.

Every JPEG and PNG the shop had was converted to a WebP of the same name
(scripts/convert_media_to_webp.py) and the original removed. An address to an
original — in a search engine's image index, an old e-mail, a cached page —
is answered with a 301 to its WebP rather than a 404. The Location is relative
("x.webp"), so it resolves on whichever address asked: the API's own /media or
the storefront's /api/backend/media.
"""

from pathlib import PurePosixPath

from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

OLD_SUFFIXES = (".jpg", ".jpeg", ".png")


class MediaFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not path.lower().endswith(OLD_SUFFIXES):
                raise
            webp = str(PurePosixPath(path).with_suffix(".webp"))
            _, found = self.lookup_path(webp)
            if found is None:
                raise
            return RedirectResponse(PurePosixPath(webp).name, status_code=301)
