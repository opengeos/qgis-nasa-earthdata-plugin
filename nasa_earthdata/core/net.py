"""HTTPS-only network helpers for plugin downloads.

Centralizes the security guard so any future tightening (header policy,
redirect handling, etc.) is applied everywhere consistently. The helpers
reject non-https URLs both on the initial request and on any redirect
target, so an https URL that 301s to http is still refused.
"""

import urllib.request
from typing import Callable, Optional


def require_https(url: str) -> None:
    """Reject any URL that is not ``https://``.

    Args:
        url: The URL to validate.

    Raises:
        ValueError: If ``url`` does not start with ``https://``.
    """
    if not url.lower().startswith("https://"):
        raise ValueError(f"Refusing non-https URL: {url!r}")


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTPRedirectHandler that refuses any non-https redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Validate ``newurl`` is https before following the redirect."""
        require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    """Build a urllib opener that enforces https on redirects."""
    return urllib.request.build_opener(_HttpsOnlyRedirectHandler())


def https_only_urlopen(url: str, timeout: float = 30):
    """``urlopen`` wrapper that rejects non-https URLs and redirects.

    Args:
        url: The URL to open. Must use the ``https`` scheme.
        timeout: Socket timeout in seconds.

    Returns:
        A response object compatible with ``urllib.request.urlopen``.
    """
    require_https(url)
    opener = _build_opener()
    return opener.open(url, timeout=timeout)  # nosec B310 - https enforced


def https_only_urlretrieve(
    url: str,
    filename: str,
    reporthook: Optional[Callable[[int, int, int], None]] = None,
    timeout: float = 60,
    chunk_size: int = 64 * 1024,
) -> None:
    """``urlretrieve`` replacement that enforces https everywhere.

    Streams the response to ``filename`` in chunks. ``reporthook`` is
    invoked with ``(block_num, block_size, total_size)`` like the stdlib
    callback so existing progress UIs keep working.

    Args:
        url: The URL to download. Must use the ``https`` scheme.
        filename: Destination path on disk.
        reporthook: Optional progress callback.
        timeout: Socket timeout in seconds.
        chunk_size: Number of bytes to read per loop iteration.
    """
    require_https(url)
    opener = _build_opener()
    with opener.open(url, timeout=timeout) as resp:  # nosec B310 - https enforced
        total_size = int(resp.headers.get("Content-Length") or 0)
        block_num = 0
        with open(filename, "wb") as out:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out.write(chunk)
                block_num += 1
                if reporthook is not None:
                    reporthook(block_num, len(chunk), total_size)
