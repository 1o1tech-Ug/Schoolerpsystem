"""
app/core/error_handlers.py
===========================
Centralized Flask error handling for the School ERP application.

Design decisions:
- Every handler logs the full details internally (method, path, IP, user).
- Clients receive ONLY safe, human-readable JSON — no stack traces, SQL
  errors, file paths, or Python internals ever reach the response body.
- The generic Exception handler catches anything not matched by a more
  specific handler, ensuring no raw exception leaks through.
- 429 responses include a Retry-After hint when Flask-Limiter provides one.
- HTML template errors (Jinja2) are caught and returned as JSON so the
  API always speaks JSON regardless of the error type.

Register once in create_app():
    from app.core.error_handlers import register_error_handlers
    register_error_handlers(app)
"""

import logging
from flask import jsonify, request

logger = logging.getLogger(__name__)


# ── Safe client messages ────────────────────────────────────────────────────────

_CLIENT_MESSAGES = {
    400: "Bad request. Please check your input and try again.",
    401: "Authentication required. Please log in.",
    403: "You do not have permission to access this resource.",
    404: "The requested resource was not found.",
    405: "This HTTP method is not allowed for this endpoint.",
    429: "Rate limit exceeded. Please try again later.",
    500: "An unexpected error occurred. Please try again later.",
}


# ── Shared context extractor ────────────────────────────────────────────────────

def _request_context() -> dict:
    """Return a dict of useful request attributes for log messages."""
    return {
        "method":  request.method,
        "path":    request.path,
        "ip":      _client_ip(),
        "user_id": _user_id(),
    }


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "-"


def _user_id() -> str:
    try:
        from flask_jwt_extended import decode_token
        token = request.cookies.get("access_token_cookie")
        if token:
            return str(decode_token(token).get("sub", "-"))
    except Exception:
        pass
    return "-"


def _safe_json(http_status: int, message: str = None):
    """Return a standardised JSON error response."""
    return jsonify({
        "success": False,
        "message": message or _CLIENT_MESSAGES.get(http_status, "An error occurred."),
    }), http_status


# ── Handler registration ────────────────────────────────────────────────────────

def register_error_handlers(app) -> None:
    """
    Attach all error handlers to the Flask application.
    Call this once in create_app() after all blueprints are registered.
    """

    # ── 400 Bad Request ──────────────────────────────────────────────────────
    @app.errorhandler(400)
    def bad_request(error):
        ctx = _request_context()
        logger.warning(
            "400 Bad Request | %s %s | IP=%s | USER=%s | %s",
            ctx["method"], ctx["path"], ctx["ip"], ctx["user_id"],
            str(error),
        )
        return _safe_json(400)

    # ── 401 Unauthorized ─────────────────────────────────────────────────────
    @app.errorhandler(401)
    def unauthorized(error):
        ctx = _request_context()
        logger.warning(
            "401 Unauthorized | %s %s | IP=%s | USER=%s | %s",
            ctx["method"], ctx["path"], ctx["ip"], ctx["user_id"],
            str(error),
        )
        return _safe_json(401)

    # ── 403 Forbidden ────────────────────────────────────────────────────────
    @app.errorhandler(403)
    def forbidden(error):
        ctx = _request_context()
        logger.warning(
            "403 Forbidden | %s %s | IP=%s | USER=%s | %s",
            ctx["method"], ctx["path"], ctx["ip"], ctx["user_id"],
            str(error),
        )
        return _safe_json(403)

    # ── 404 Not Found ────────────────────────────────────────────────────────
    @app.errorhandler(404)
    def not_found(error):
        ctx = _request_context()
        logger.warning(
            "404 Not Found | %s %s | IP=%s | USER=%s",
            ctx["method"], ctx["path"], ctx["ip"], ctx["user_id"],
        )
        return _safe_json(404, "The requested resource was not found.")

    # ── 405 Method Not Allowed ───────────────────────────────────────────────
    @app.errorhandler(405)
    def method_not_allowed(error):
        ctx = _request_context()
        logger.warning(
            "405 Method Not Allowed | %s %s | IP=%s | USER=%s",
            ctx["method"], ctx["path"], ctx["ip"], ctx["user_id"],
        )
        return _safe_json(405)

    # ── 429 Too Many Requests ────────────────────────────────────────────────
    @app.errorhandler(429)
    def rate_limit_exceeded(error):
        ctx = _request_context()
        logger.warning(
            "429 Rate Limit Exceeded | %s %s | IP=%s | USER=%s | limit=%s",
            ctx["method"], ctx["path"], ctx["ip"], ctx["user_id"],
            getattr(error, "description", "unknown"),
        )
        response = jsonify({
            "success": False,
            "message": "Rate limit exceeded. Please try again later.",
        })
        response.status_code = 429
        # Propagate Retry-After if Flask-Limiter set it
        retry_after = getattr(error, "retry_after", None)
        if retry_after:
            response.headers["Retry-After"] = str(int(retry_after))
        return response

    # ── 500 Internal Server Error ────────────────────────────────────────────
    @app.errorhandler(500)
    def internal_server_error(error):
        ctx = _request_context()
        logger.error(
            "500 Internal Server Error | %s %s | IP=%s | USER=%s | %s",
            ctx["method"], ctx["path"], ctx["ip"], ctx["user_id"],
            str(error),
            exc_info=True,
        )
        return _safe_json(500)

    # ── Generic Exception catch-all ──────────────────────────────────────────
    # This fires for any unhandled exception that Flask doesn't route to a
    # numbered handler above.  It logs with exc_info=True so the full
    # traceback appears in the log file but never in the response.
    @app.errorhandler(Exception)
    def handle_unexpected_exception(error):
        # Let Flask's own HTTP exception handling take priority for 4xx/5xx
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            # Re-raise to let the numbered handlers above deal with it
            return handle_http_exception(error)

        ctx = _request_context()
        logger.exception(
            "UNHANDLED EXCEPTION | %s %s | IP=%s | USER=%s | %s: %s",
            ctx["method"], ctx["path"], ctx["ip"], ctx["user_id"],
            type(error).__name__, str(error),
        )
        return _safe_json(500)


def handle_http_exception(error):
    """Delegate werkzeug HTTPExceptions to their numbered handlers."""
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException):
        ctx = _request_context()
        logger.warning(
            "%s HTTP Exception | %s %s | IP=%s | USER=%s | %s",
            error.code,
            ctx["method"], ctx["path"], ctx["ip"], ctx["user_id"],
            error.description,
        )
        return _safe_json(error.code, _CLIENT_MESSAGES.get(error.code))
    raise error