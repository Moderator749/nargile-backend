"""A short CDN cache for public reads every storefront page makes.

The storefront asks for the live banner, the popup and the store parameters
on every page view, through its /api/backend proxy (Vercel). Without a
Cache-Control the CDN forwards each one here: a request on this 1 GB server
and a billed origin transfer per page view. With `s-maxage` the CDN answers
them for a minute (and a minute more while it fetches the next), so an admin
edit shows within about a minute, and browsers keep nothing (no max-age).

Only for reads that are the same for everyone: never one that depends on who
asks (prices for a B2B segment, a cart, an account).
"""

from fastapi import Response

EDGE_CACHE_SECONDS = 60
EDGE_CACHE_CONTROL = f"public, s-maxage={EDGE_CACHE_SECONDS}, stale-while-revalidate={EDGE_CACHE_SECONDS}"


def edge_cached(response: Response) -> None:
    """Route dependency: the CDN may keep this answer for a minute."""
    response.headers["Cache-Control"] = EDGE_CACHE_CONTROL
