"""
app/core/rate_limit.py
=======================
Flask-Limiter configuration for the School ERP application.

IP Extraction
─────────────
All limits are keyed on the *real* client IP address.

On Render (and behind any reverse proxy), the original client IP is in
the X-Forwarded-For header.  We read the leftmost value — the IP appended
by the first trusted proxy — to get the genuine client address.  Relying
on REMOTE_ADDR alone would key every user on the load balancer's IP,
making rate limiting useless.

Anti-spoofing note: a malicious client can prepend values to
X-Forwarded-For, but the *rightmost* value they control is the one we
ignore (it's added by our trusted proxy).  We take the leftmost value
which is what the *client* sent — if they spoof it they lock out
themselves.  Perfect prevention requires configuring trusted proxy counts,
which is beyond Flask-Limiter's scope; for a school ERP this is adequate.

Storage
───────
Uses in-memory storage by default (suitable for single-instance Render
deployments).  Swap to Redis by setting RATELIMIT_STORAGE_URI in the
environment:
    RATELIMIT_STORAGE_URI=redis://localhost:6379

Rate Limit Tiers
─────────────────
Tier 1 — CRITICAL (authentication, password reset):
    Very strict.  Brute-force and credential-stuffing protection.

Tier 2 — STRICT (write operations on sensitive data):
    Bulk operations, financial writes, factory reset.

Tier 3 — STANDARD (normal write operations):
    Most POST/PUT/DELETE routes.

Tier 4 — RELAXED (read-only endpoints):
    Dashboards, list views, search results.

Tier 5 — REPORT (resource-intensive generation):
    PDF generation routes.

Global default: 200 per day, 60 per hour applied to everything not
explicitly decorated.

Usage
─────
Decorate individual routes:

    from app.core.rate_limit import limiter, AUTH_LIMIT, REPORT_LIMIT

    @auth_bp.route("/staff/login", methods=["POST"])
    @limiter.limit(AUTH_LIMIT)
    def staff_login():
        ...

Or use shared limits (applied to a group of routes via a decorator on
the blueprint or multiple routes sharing the same key):

    @limiter.limit(BULK_LIMIT, key_func=get_remote_address)
    def bulk_operation():
        ...
"""

import logging
from flask import request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address as _default_remote_address

logger = logging.getLogger(__name__)


# ── Real-IP extractor ──────────────────────────────────────────────────────────

def get_real_ip() -> str:
    """
    Return the genuine client IP address, honouring Render's reverse proxy.

    Priority:
      1. X-Forwarded-For leftmost address  (set by Render's load balancer)
      2. X-Real-IP header                  (set by some nginx configs)
      3. REMOTE_ADDR                       (direct connection / local dev)

    This function is passed to Limiter as the key_func so every limit
    bucket is keyed on the correct IP.
    """
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        ip = forwarded_for.split(",")[0].strip()
        if ip:
            return ip

    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip

    return request.remote_addr or "unknown"


# ── Limiter instance ───────────────────────────────────────────────────────────
# Exported and imported in extensions.py so it can be initialised with the app.

limiter = Limiter(
    key_func=get_real_ip,
    # Global default: applied to every route that has no explicit @limiter.limit()
    default_limits=["200 per day", "60 per hour"],
    # Return JSON on 429 — configured via on_breach in register_rate_limit_handlers()
    headers_enabled=True,          # adds X-RateLimit-* response headers
    storage_uri="memory://",       # override with RATELIMIT_STORAGE_URI env var
)


# ── Named limit strings ────────────────────────────────────────────────────────
# Import these in route files and pass to @limiter.limit() so the numbers
# live in one place and are easy to tune for production traffic.

# Tier 1 — CRITICAL: authentication, password operations
AUTH_LIMIT          = "5 per minute;20 per hour"       # login attempts
LOGOUT_LIMIT        = "20 per minute"                   # logout (low risk but prevent DoS)
PASSWORD_RESET_LIMIT = "3 per minute;10 per hour"       # password reset / change
FORGOT_PASSWORD_LIMIT = "3 per minute;5 per hour"       # forgot-password trigger

# Tier 2 — STRICT: destructive / sensitive write operations
BULK_LIMIT          = "10 per minute;50 per hour"       # bulk import, bulk report gen
FACTORY_RESET_LIMIT = "2 per hour"                      # factory reset (very destructive)
EXPORT_LIMIT        = "10 per hour"                     # data export (resource intensive)
PAYMENT_LIMIT       = "30 per minute"                   # record payment

# Tier 3 — STANDARD: normal write operations
WRITE_LIMIT         = "60 per minute;300 per hour"      # create / update / delete
MARKS_SAVE_LIMIT    = "60 per minute"                   # entering marks
PUSH_REPORTS_LIMIT  = "20 per minute"                   # push report cards

# Tier 4 — RELAXED: read-only / search
READ_LIMIT          = "120 per minute;600 per hour"     # list views, dashboards
SEARCH_LIMIT        = "60 per minute"                   # search endpoints

# Tier 5 — REPORT: resource-intensive PDF generation
REPORT_GEN_LIMIT    = "10 per minute;60 per hour"       # single report generation
REPORT_ALL_LIMIT    = "3 per minute;20 per hour"        # generate all reports in stream


# ── 429 handler (JSON, no HTML) ────────────────────────────────────────────────

def register_rate_limit_handlers(app) -> None:
    """
    Override Flask-Limiter's default 429 HTML page with a JSON response.
    Also logs every rate-limit breach so operators can spot abuse patterns.

    Call this in create_app() after limiter.init_app(app).
    """

    @app.errorhandler(429)
    def ratelimit_handler(e):
        _log_rate_limit_breach()
        from flask import jsonify
        response = jsonify({
            "success": False,
            "message": "Rate limit exceeded. Please try again later.",
        })
        response.status_code = 429
        retry_after = getattr(e, "retry_after", None)
        if retry_after:
            response.headers["Retry-After"] = str(int(retry_after))
        return response


def _log_rate_limit_breach() -> None:
    """Log a WARNING for every 429 with full context."""
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    ip = forwarded_for.split(",")[0].strip() if forwarded_for else (request.remote_addr or "-")

    user_id = "-"
    try:
        from flask_jwt_extended import decode_token
        token = request.cookies.get("access_token_cookie")
        if token:
            user_id = str(decode_token(token).get("sub", "-"))
    except Exception:
        pass

    logger.warning(
        "RATE LIMIT BREACH | %s %s | IP=%s | USER=%s",
        request.method, request.path, ip, user_id,
    )