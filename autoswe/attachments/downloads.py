"""Raw HTTP download helpers for the attachment layer (issue #290).

Kept deliberately small and urllib-based: attachment bytes are untrusted
input, and the only invariants enforced here are the size cap (checked
*before* any bytes touch disk) and a hard timeout. All failures surface as
:class:`DownloadError` with a machine-readable ``code`` so callers can
distinguish the GitHub Mechanism-A 404 (which triggers the Mechanism-B
fallback) from anything else.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib import error as url_error
from urllib import request
from urllib.parse import urlsplit

_DOWNLOAD_TIMEOUT_SEC = 60.0
_CHUNK = 64 * 1024


@dataclass
class DownloadError(Exception):
    """A failed attachment download.

    ``code`` is one of ``"http_<status>"``, ``"too_large"``,
    ``"bad_scheme"`` (non-http(s) URL rejected by the scheme allowlist), or
    ``"transport"`` (network failure / timeout / malformed-response error).
    """

    url: str
    code: str
    message: str = ""

    def __str__(self) -> str:
        # The url may be a Mechanism-B signed private-image URL carrying a
        # short-lived JWT in the query string — a credential. Strip the query
        # so the signed segment never reaches a log line (the path is not
        # secret and is kept for correlation).
        return (
            f"{type(self).__name__}({self.code}): "
            f"{_safe_url(self.url)}: {self.message}"
        )


def _safe_url(url: str) -> str:
    """Return *url* with its query string removed, for safe logging.

    Signed ``private-user-images`` URLs carry a scoped, ~300s-TTL JWT in the
    query string; it is a credential and must never appear in logs. Only the
    non-secret path is retained. Idempotent on query-free URLs.
    """
    return (url or "").split("?", 1)[0]


_ALLOWED_SCHEMES = ("http", "https")


def _bad_scheme_target(newurl: str) -> str | None:
    """Return the offending scheme when a *redirect target* is not
    http/https, else ``None``. An empty scheme (relative ``Location``)
    resolves against the http(s) base and is fine."""
    scheme = urlsplit(str(newurl).strip()).scheme.lower()
    if scheme and scheme not in _ALLOWED_SCHEMES:
        return scheme
    return None


class _SchemeSafeRedirectHandler(request.HTTPRedirectHandler):
    """Follows redirects only to http/https targets.

    urllib's stock handler would let a ``Location: file:///etc/passwd`` hop
    read a LOCAL file through this sink. The check lives in ``http_error_302``
    (the single dispatch entry the 301/302/303/307/308 aliases share), not in
    ``redirect_request``, because CPython ≥3.10.12 pre-checks the target there
    and raises its own ``HTTPError`` (surfacing here as ``http_302``, not
    ``bad_scheme``) while still allowing ``ftp://`` targets through to
    ``redirect_request``. Overriding the dispatch entry keeps the sink's
    ``bad_scheme`` contract deterministic across versions.

    A raised :class:`DownloadError` propagates out of ``OpenerDirector.open``
    (urllib only swallows ``HTTPError`` in this path), so callers see exactly
    the same failure shape as the initial-URL guard.
    """

    def http_error_302(self, req, fp, code, msg, headers):
        target = None
        if "location" in headers:
            target = headers["location"]
        elif "uri" in headers:
            target = headers["uri"]
        if target:
            bad = _bad_scheme_target(target)
            if bad is not None:
                raise DownloadError(
                    req.full_url, "bad_scheme",
                    f"redirect target scheme {bad!r} not allowed (http/https only)",
                )
        return super().http_error_302(req, fp, code, msg, headers)


# Every redirect code the base class dispatches must go through the hardened
# override. 308 handling only exists on newer CPythons — alias only if present
# so this never *adds* redirect support a base Python does not have.
for _code in (301, 303, 307, 308):
    if hasattr(request.HTTPRedirectHandler, f"http_error_{_code}"):
        setattr(
            _SchemeSafeRedirectHandler, f"http_error_{_code}",
            _SchemeSafeRedirectHandler.http_error_302,
        )


def _build_opener() -> request.OpenerDirector:
    """Opener that follows redirects (http/https targets only) but sends no
    cookies (fresh per call).

    Passing a *subclass instance* of ``HTTPRedirectHandler`` makes
    ``build_opener`` skip the stock redirect handler entirely (it removes any
    default whose class the passed instance is an instance of), so the scheme
    check cannot be shadowed by a second handler in the dispatch chain.
    """
    return request.build_opener(_SchemeSafeRedirectHandler())


def download_bytes(
    url: str,
    *,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    opener: request.OpenerDirector | None = None,
) -> bytes:
    """GET *url* following redirects, returning at most *max_bytes*.

    Raises :class:`DownloadError` — never returns partial data. The size cap
    is enforced while streaming so a file larger than *max_bytes* never
    occupies more than one chunk of memory and nothing is returned.

    This is the single download sink, so the scheme allowlist lives here —
    for the initial URL *and*, via :func:`_build_opener`, for every redirect
    target: any URL — including Azure ``relations[]`` URLs handed over
    verbatim — must stay ``http``/``https`` end to end (urllib's default
    opener carries a ``file://`` handler; a crafted relation or a crafted
    ``Location:`` hop would otherwise read a local file into the temp dir
    and advertise it in the manifest).
    """
    scheme = urlsplit(url or "").scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise DownloadError(
            url, "bad_scheme",
            f"scheme {scheme!r} not allowed (http/https only)",
        )
    opener = opener or _build_opener()
    req = request.Request(url, headers=headers or {}, method="GET")
    try:
        with opener.open(req, timeout=_DOWNLOAD_TIMEOUT_SEC) as resp:
            parts: list[bytes] = []
            total = 0
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise DownloadError(url, "too_large", f"exceeds {max_bytes} bytes")
                parts.append(chunk)
            return b"".join(parts)
    except DownloadError:
        raise
    except url_error.HTTPError as e:
        raise DownloadError(url, f"http_{e.code}", f"HTTP {e.code}") from e
    except url_error.URLError as e:
        raise DownloadError(url, "transport", str(e.reason)) from e
    except Exception as e:
        # The docstring's contract: *every* failure surfaces as
        # DownloadError. Covers TimeoutError/OSError and urllib leaks that
        # are neither — e.g. http.client.HTTPException (BadStatusLine /
        # RemoteDisconnected on a malformed response) — so no raw error
        # escapes this sink.
        raise DownloadError(url, "transport", str(e)) from e


def is_http_error_code(code: str | None, *status: int) -> bool:
    """True when a :class:`DownloadError` code is HTTP *status* (any of)."""
    if not code:
        return False
    return code in {f"http_{s}" for s in status}
