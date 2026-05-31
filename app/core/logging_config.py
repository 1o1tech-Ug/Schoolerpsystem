"""
app/core/logging_config.py
==========================
Centralized logging configuration for the School ERP application.

Design decisions:
- WARNING level and above only (no INFO noise in production).
- Rotating file handler: 10 MB per file, 5 backups retained.
- Log destination: logs/application.log (relative to project root).
- Rich format includes timestamp, level, HTTP context, user ID, and
  full stack traces on exceptions.
- A custom ContextFilter injects request-level data (path, method, IP,
  user_id) into every log record so every handler gets it automatically.
- Thread-safe: RotatingFileHandler uses the default Python file lock.

Usage:
    from app.core.logging_config import setup_logging, get_logger

    setup_logging(app)               # call once in create_app()
    logger = get_logger(__name__)    # in any module
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from flask import has_request_context, request

# ── Constants ─────────────────────────────────────────────────────────────────

LOG_DIR      = "logs"
LOG_FILE     = os.path.join(LOG_DIR, "application.log")
MAX_BYTES    = 10 * 1024 * 1024   # 10 MB per file
BACKUP_COUNT = 5                  # keep 5 rotated files
LOG_LEVEL    = logging.WARNING    # WARNING and above only

LOG_FORMAT = (
    "[%(asctime)s] %(levelname)s | "
    "%(method)s %(path)s | "
    "IP=%(ip)s | USER=%(user_id)s | "
    "%(name)s | %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


# ── Context filter ─────────────────────────────────────────────────────────────

class RequestContextFilter(logging.Filter):
    """
    Injects HTTP request context into every log record.

    When called outside a request context (e.g. startup, background tasks)
    the fields fall back to safe placeholder strings so formatting never fails.

    Injected attributes:
        record.method   — HTTP verb (GET, POST, …) or "NO_REQUEST"
        record.path     — URL path or "NO_REQUEST"
        record.ip       — Client IP from X-Forwarded-For or REMOTE_ADDR
        record.user_id  — Value extracted from JWT cookie claim (best-effort)
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if has_request_context():
            record.method  = request.method
            record.path    = request.path
            record.ip      = _get_client_ip()
            record.user_id = _get_user_id()
        else:
            record.method  = "NO_REQUEST"
            record.path    = "NO_REQUEST"
            record.ip      = "-"
            record.user_id = "-"
        return True


def _get_client_ip() -> str:
    """
    Extract the real client IP, honouring the X-Forwarded-For header
    set by Render's load balancer.

    Security note: we trust only the *leftmost* address in X-Forwarded-For
    which is the original client IP appended by the first proxy.  This
    prevents IP spoofing by malicious clients who craft their own header.
    """
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        # leftmost entry = original client
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "-"


def _get_user_id() -> str:
    """
    Best-effort extraction of the authenticated user ID from the JWT
    stored in the access-token cookie.  Never raises — returns "-" on
    any failure (unauthenticated requests, malformed tokens, etc.).
    """
    try:
        from flask_jwt_extended import decode_token
        token = request.cookies.get("access_token_cookie")
        if token:
            decoded = decode_token(token)
            return str(decoded.get("sub", "-"))
    except Exception:
        pass
    return "-"


# ── Setup function ─────────────────────────────────────────────────────────────

def setup_logging(app) -> None:
    """
    Configure application-wide logging.  Call once inside create_app().

    Steps:
      1. Create the logs/ directory if it does not exist.
      2. Build a RotatingFileHandler writing to logs/application.log.
      3. Attach the RequestContextFilter to inject HTTP context.
      4. Set the root logger to WARNING so third-party libraries are quiet.
      5. Set Flask's own logger to WARNING.
      6. Suppress noisy but harmless loggers (werkzeug access log, etc.)
    """
    # Ensure log directory exists
    os.makedirs(LOG_DIR, exist_ok=True)

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)
    ctx_filter = RequestContextFilter()

    # ── Rotating file handler ────────────────────────────────────────────────
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(ctx_filter)

    # ── Root logger ──────────────────────────────────────────────────────────
    root_logger = logging.getLogger()
    root_logger.setLevel(LOG_LEVEL)

    # Avoid adding duplicate handlers if setup_logging is called more than once
    if not any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers):
        root_logger.addHandler(file_handler)

    # ── Flask app logger ─────────────────────────────────────────────────────
    app.logger.setLevel(LOG_LEVEL)
    if not any(isinstance(h, RotatingFileHandler) for h in app.logger.handlers):
        app.logger.addHandler(file_handler)

    # ── Suppress noisy third-party loggers ──────────────────────────────────
    # werkzeug logs every HTTP request at INFO — not useful in production
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    # SQLAlchemy engine is very chatty at DEBUG/INFO
    logging.getLogger("sqlalchemy.engine").setLevel(logging.ERROR)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.ERROR)

    app.logger.warning(
        "Logging system initialised — writing WARNING+ to %s", LOG_FILE
    )


# ── Module-level helper ────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger.  Use this instead of logging.getLogger()
    throughout the application so all loggers share the same configuration.

    Example:
        logger = get_logger(__name__)
        logger.warning("Something worth knowing about")
        logger.error("Something went wrong: %s", detail)
        logger.exception("Unexpected exception in route_name")
    """
    return logging.getLogger(name)