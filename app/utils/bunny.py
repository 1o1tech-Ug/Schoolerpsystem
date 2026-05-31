"""
app/utils/bunny.py
==================
Thin wrapper around the BunnyCDN Storage API.

Usage
-----
    from app.utils.bunny import bunny_upload, bunny_delete, bunny_public_url

    # Upload raw bytes; returns the public CDN URL or raises on failure.
    url = bunny_upload(data=file.read(), remote_path="uploads/images/foo.png")

    # Delete a file by its remote path (non-fatal — logs on error).
    bunny_delete("uploads/images/foo.png")

    # Build a public URL from a remote path without uploading.
    url = bunny_public_url("uploads/images/foo.png")

Environment variables required (already in Config)
---------------------------------------------------
    BUNNY_STORAGE_ZONE      e.g.  school-erp
    BUNNY_STORAGE_PASSWORD  your zone password / AccessKey
    BUNNY_BASE_URL          e.g.  https://school-erp.b-cdn.net  (no trailing slash)
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)

# ── Read config from environment (works both inside and outside app context) ──
_ZONE     = os.getenv("BUNNY_STORAGE_ZONE",     "")
_PASSWORD = os.getenv("BUNNY_STORAGE_PASSWORD", "")
_BASE_URL = os.getenv("BUNNY_BASE_URL",         "").rstrip("/")

_STORAGE_ENDPOINT = "https://storage.bunnycdn.com"


# ─────────────────────────────────────────────────────────────
# PUBLIC HELPERS
# ─────────────────────────────────────────────────────────────

def bunny_public_url(remote_path: str) -> str:
    """
    Return the public CDN URL for a given remote_path.

    Example
    -------
        bunny_public_url("uploads/images/foo.png")
        → "https://school-erp.b-cdn.net/uploads/images/foo.png"
    """
    return f"{_BASE_URL}/{remote_path.lstrip('/')}"


def bunny_upload(data: bytes, remote_path: str) -> str:
    """
    Upload *data* (bytes) to BunnyCDN at *remote_path*.

    Returns the public CDN URL on success.
    Raises RuntimeError on HTTP errors.

    Parameters
    ----------
    data        : raw file bytes
    remote_path : path inside the storage zone, e.g. "uploads/images/foo.png"
    """
    if not _ZONE or not _PASSWORD:
        raise RuntimeError(
            "BunnyCDN is not configured. "
            "Set BUNNY_STORAGE_ZONE and BUNNY_STORAGE_PASSWORD env vars."
        )

    url = f"{_STORAGE_ENDPOINT}/{_ZONE}/{remote_path.lstrip('/')}"

    response = requests.put(
        url,
        headers={
            "AccessKey":     _PASSWORD,
            "Content-Type":  "application/octet-stream",
        },
        data=data,
        timeout=60,
    )

    if response.status_code not in (200, 201):
        logger.error(
            "bunny_upload: PUT %s → HTTP %s  body=%s",
            url, response.status_code, response.text[:200],
        )
        raise RuntimeError(
            f"BunnyCDN upload failed (HTTP {response.status_code}): {response.text[:200]}"
        )

    public_url = bunny_public_url(remote_path)
    logger.info("bunny_upload: uploaded → %s", public_url)
    return public_url


def bunny_delete(remote_path: str) -> bool:
    """
    Delete a file from BunnyCDN.  Non-fatal: logs errors but does not raise.

    Returns True on success, False on failure.
    """
    if not _ZONE or not _PASSWORD or not remote_path:
        return False

    url = f"{_STORAGE_ENDPOINT}/{_ZONE}/{remote_path.lstrip('/')}"

    try:
        response = requests.delete(
            url,
            headers={"AccessKey": _PASSWORD},
            timeout=30,
        )
        if response.status_code in (200, 204, 404):
            logger.info("bunny_delete: removed %s (HTTP %s)", url, response.status_code)
            return True
        else:
            logger.warning(
                "bunny_delete: DELETE %s → HTTP %s  body=%s",
                url, response.status_code, response.text[:200],
            )
            return False
    except Exception as exc:
        logger.warning("bunny_delete: exception for %s — %s", remote_path, exc)
        return False


def bunny_remote_path_from_url(public_url: str) -> str:
    """
    Derive the remote_path from a public CDN URL so it can be passed to
    bunny_delete().

    Example
    -------
        bunny_remote_path_from_url(
            "https://school-erp.b-cdn.net/uploads/images/foo.png"
        )
        → "uploads/images/foo.png"

    Falls back to returning the original string unchanged if the base URL
    prefix is not recognised (safe to pass to bunny_delete which is non-fatal).
    """
    if _BASE_URL and public_url.startswith(_BASE_URL):
        return public_url[len(_BASE_URL):].lstrip("/")
    return public_url