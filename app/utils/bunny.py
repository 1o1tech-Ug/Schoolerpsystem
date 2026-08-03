"""
app/utils/bunny.py
==================
Thin wrapper around the BunnyCDN Storage API.

[STORAGE-ARCHITECTURE CHANGE]
This module now follows the same relative-path storage architecture as
the student-photo system:

  * bunny_upload() still uploads to the exact same BunnyCDN location as
    before, but now returns a RELATIVE path (e.g.
    "/uploads/signatures/school_1_headteacher.png") instead of a full
    CDN URL. This is the value that should be persisted to the database.
  * public_file_url() turns a stored relative path into a full public
    URL at READ time, using current_app.config["CDN_BASE_URL"]. Callers
    build URLs for rendering/JSON/PDF generation by calling this — the
    database value itself is never touched.
  * bunny_delete() already accepted a Bunny remote path (not a full
    URL), so no change was needed there: callers now simply pass the
    relative path straight out of the database instead of first parsing
    it out of a full URL.

Usage
-----
    from app.utils.bunny import bunny_upload, bunny_delete, public_file_url

    # Upload raw bytes; returns a RELATIVE path (e.g. "/uploads/images/foo.png")
    # or raises on failure. Store this value directly in the database.
    rel_path = bunny_upload(data=file.read(), remote_path="uploads/images/foo.png")

    # Delete a file by its stored relative (or bare) path — non-fatal,
    # logs on error.
    bunny_delete(rel_path)

    # Build a public URL from a stored relative path, for rendering,
    # JSON responses, or embedding in generated PDFs/HTML.
    url = public_file_url(rel_path)

Environment variables required (already in Config)
---------------------------------------------------
    BUNNY_STORAGE_ZONE      e.g.  school-erp
    BUNNY_STORAGE_PASSWORD  your zone password / AccessKey

The public base URL used to build full links is read from
current_app.config["CDN_BASE_URL"] at request time (see public_file_url
below) rather than from an environment variable, so it can be resolved
per-app-context the same way the student-photo system does it.
"""

import os
import logging
import requests
from flask import current_app

logger = logging.getLogger(__name__)

# ── Read config from environment (works both inside and outside app context) ──
_ZONE     = os.getenv("BUNNY_STORAGE_ZONE",     "")
_PASSWORD = os.getenv("BUNNY_STORAGE_PASSWORD", "")

_STORAGE_ENDPOINT = "https://jh.storage.bunnycdn.com/schoolerpsystem"


# ─────────────────────────────────────────────────────────────
# PUBLIC HELPERS
# ─────────────────────────────────────────────────────────────

def _resolve_cdn_base_url() -> str:
    """
    Find the configured public base URL, checking the places it's
    plausibly set, in order, rather than assuming one exact key exists.

    We hit a KeyError in production because current_app.config["CDN_BASE_URL"]
    was assumed to exist unconditionally — if the student-photo system
    actually stores this under a different config key, or only ever set
    it as an environment variable, that assumption is wrong and a bare
    `config[...]` lookup blows up mid-template-render with a cryptic
    error. This checks, in order:

      1. current_app.config["CDN_BASE_URL"]      (documented/expected key)
      2. current_app.config["BUNNY_BASE_URL"]     (name used before this
                                                    refactor)
      3. os.environ["CDN_BASE_URL"]
      4. os.environ["BUNNY_BASE_URL"]             (the original env var
                                                    this module used
                                                    pre-refactor)

    Raises RuntimeError with an explicit, actionable message if none of
    these are set, instead of letting a KeyError surface from deep
    inside Jinja rendering.
    """
    try:
        cfg = current_app.config
    except RuntimeError:
        cfg = {}

    for key in ("CDN_BASE_URL", "BUNNY_BASE_URL"):
        val = cfg.get(key) if cfg else None
        if val:
            return val

    for key in ("CDN_BASE_URL", "BUNNY_BASE_URL"):
        val = os.getenv(key)
        if val:
            return val

    raise RuntimeError(
        "No CDN base URL is configured. Set CDN_BASE_URL in your Flask "
        "app config (app.config['CDN_BASE_URL'] = 'https://your-zone.b-cdn.net') "
        "or as an environment variable, so public_file_url() can build "
        "full links from stored relative paths."
    )


def public_file_url(path: str | None) -> str | None:
    """
    Turn a stored RELATIVE path (e.g. "/uploads/images/foo.png") into a
    full public CDN URL.

    This is the single place URL-building logic lives — every read
    path (template rendering, JSON responses, PDF/HTML generation)
    should call this rather than concatenating strings itself.

    Idempotent: if `path` already looks like a full URL (legacy rows,
    or any caller that accidentally passes one through), it's returned
    unchanged rather than double-prefixed.

    Returns None for an empty/missing path rather than raising —
    templates commonly do `{{ student.photo_url }}` for students who
    have no photo at all, and that should render as an empty src, not
    crash the whole page/report.
    """
    if not path:
        return None
    if path.startswith("http://") or path.startswith("https://"):
        return path
    base = _resolve_cdn_base_url().rstrip("/")
    return f"{base}/{path.lstrip('/')}"


# Backwards-compatible alias. Several existing modules across the
# codebase import `bunny_public_url` directly (studentManagement.py,
# app/models/people.py, likely others not yet audited). Rather than
# tracking down every call site one ImportError at a time, the old
# name is kept as a plain alias for the new helper — same behaviour.
# New/edited code should prefer public_file_url(); this alias exists
# purely so untouched files don't break.
bunny_public_url = public_file_url


def bunny_upload(data: bytes, remote_path: str, cache_control: str | None = None) -> str:
    """
    Upload *data* (bytes) to BunnyCDN at *remote_path*.

    The upload destination inside Bunny Storage is unchanged — this
    still PUTs to the same zone/path as before. Only the return value
    changed: this now returns a RELATIVE path (leading slash, no
    scheme/host) instead of a full CDN URL, so callers can store it
    directly in the database following the same architecture as the
    student-photo system.

    Raises RuntimeError on HTTP errors.

    Parameters
    ----------
    data          : raw file bytes
    remote_path   : path inside the storage zone, e.g. "uploads/images/foo.png"
    cache_control : optional Cache-Control value to store as file metadata.
                    BunnyCDN's Pull Zone serves this header back to clients
                    and edge nodes on every request for the file, so it
                    controls both browser and CDN-edge caching behaviour.
                    e.g. "no-cache, no-store, must-revalidate" for files
                    that get regenerated/overwritten at the same path
                    (report cards), or "public, max-age=2592000" for
                    files that rarely change once uploaded (logos,
                    signatures).
                    Leave as None to fall back to BunnyCDN's zone default.
    """
    if not _ZONE or not _PASSWORD:
        raise RuntimeError(
            "BunnyCDN is not configured. "
            "Set BUNNY_STORAGE_ZONE and BUNNY_STORAGE_PASSWORD env vars."
        )

    clean_remote_path = remote_path.lstrip("/")
    url = f"{_STORAGE_ENDPOINT}/{_ZONE}/{clean_remote_path}"

    headers = {
        "AccessKey":    _PASSWORD,
        "Content-Type": "application/octet-stream",
    }
    if cache_control:
        headers["Cache-Control"] = cache_control

    response = requests.put(
        url,
        headers=headers,
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

    relative_path = f"/{clean_remote_path}"
    logger.info(
        "bunny_upload: uploaded → %s (cache_control=%s)", relative_path, cache_control
    )
    return relative_path


def bunny_delete(remote_path: str) -> bool:
    """
    Delete a file from BunnyCDN.  Non-fatal: logs errors but does not raise.

    *remote_path* is expected to be the stored relative path (with or
    without a leading slash — both are accepted). This never expects
    or parses a full URL; the caller passes the database value straight
    through.

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


def bunny_remote_path_from_url(stored_value: str) -> str:
    """
    Backwards-compatible helper, kept for any un-audited caller still
    importing it (mirrors the bunny_public_url alias above — same
    reasoning). Historically this stripped a full CDN URL down to a
    bare remote path so it could be passed to bunny_delete().

    Under the new architecture bunny_delete() already accepts a stored
    relative path directly, so this is now just a thin normalizer: it
    strips a known base URL prefix if one is present (legacy rows that
    still hold a full URL), otherwise it returns the value unchanged
    with any leading slash removed. Either result is safe to pass
    straight to bunny_delete().
    """
    if not stored_value:
        return stored_value
    if stored_value.startswith("http://") or stored_value.startswith("https://"):
        try:
            base = current_app.config.get("CDN_BASE_URL", "")
        except RuntimeError:
            base = ""
        if base and stored_value.startswith(base.rstrip("/")):
            return stored_value[len(base.rstrip("/")):].lstrip("/")
        # Unknown host — fall back to everything after the third "/"
        # (i.e. strip the scheme + host, keep the path).
        parts = stored_value.split("/", 3)
        return parts[3] if len(parts) == 4 else stored_value
    return stored_value.lstrip("/")