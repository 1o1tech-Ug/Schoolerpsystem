"""
app/routes/auth.py
==================
Authentication routes — login, logout, token refresh.

CHANGES vs original:
  - Rate limits applied to all sensitive endpoints (Tier 1 / CRITICAL).
  - All except blocks now log internally and return safe messages to clients.
    No str(e) is ever returned to the user.
  - print() calls replaced by logger calls.
"""

import logging

from flask import Blueprint, request, jsonify, render_template, redirect, url_for
from werkzeug.security import check_password_hash

from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    set_access_cookies,
    set_refresh_cookies,
    jwt_required,
    get_jwt,
    get_jwt_identity,
    decode_token,
    unset_jwt_cookies,
)

from app.models.user import User, StudentAuth
from app.models.blocklist import TokenBlocklist
from app.extensions import db, limiter
from app.models.people import Student
from app.core.rate_limit import (
    AUTH_LIMIT,
    LOGOUT_LIMIT,
    PASSWORD_RESET_LIMIT,
    READ_LIMIT,
)

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


# ─────────────────────────────────────────────────────────────
#  LOGIN PAGE  —  GET /auth/login
# ─────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET"])
@limiter.limit(READ_LIMIT)
def login_page():
    return render_template("auth/login.html")


# ─────────────────────────────────────────────────────────────
#  STAFF LOGIN  —  POST /auth/staff/login
# ─────────────────────────────────────────────────────────────

@auth_bp.route("/staff/login", methods=["POST"])
@limiter.limit(AUTH_LIMIT)
def staff_login():
    data = request.get_json()

    school_id = data.get("school_id")
    username  = data.get("username")
    password  = data.get("password")

    if not school_id or not username or not password:
        return jsonify({"message": "All fields are required"}), 400

    user = User.query.filter_by(
        school_id=school_id,
        username=username,
    ).first()

    # Deliberate: same message for "not found" and "wrong password"
    # to prevent username enumeration.
    if not user or not check_password_hash(user.password_hash, password):
        logger.warning(
            "Failed staff login attempt | school_id=%s username=%s",
            school_id, username,
        )
        return jsonify({"message": "Invalid credentials"}), 401

    if user.status in ("disabled", "suspended"):
        logger.warning(
            "Blocked login for %s account | user_id=%s",
            user.status, user.id,
        )
        return jsonify({
            "message": f"Your account has been {user.status}. Contact your administrator."
        }), 403

    try:
        access_token  = create_access_token(identity=str(user.id))
        refresh_token = create_refresh_token(identity=str(user.id))

        redirect_map = {
            "staff":      "/base",
            "superadmin": "/superadmin",
            "admin":      "/admin",
        }
        redirect_url = redirect_map.get(user.role, "/protected")

        response = jsonify({
            "message":  "Login successful",
            "role":     user.role,
            "redirect": redirect_url,
        })

        set_access_cookies(response, access_token)
        set_refresh_cookies(response, refresh_token)

        return response, 200

    except Exception:
        logger.exception(
            "JWT creation failed | user_id=%s school_id=%s", user.id, school_id
        )
        return jsonify({"message": "Login failed. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
#  STUDENT LOGIN  —  POST /auth/student/login
# ─────────────────────────────────────────────────────────────

@auth_bp.route("/student/login", methods=["POST"])
@limiter.limit(AUTH_LIMIT)
def student_login():
    data = request.get_json()

    school_id    = data.get("school_id")
    student_code = data.get("student_code")
    password     = data.get("password")

    if not school_id or not student_code or not password:
        return jsonify({"message": "All fields are required"}), 400

    student = Student.query.filter_by(
        school_id=school_id,
        student_code=student_code,
    ).first()

    if not student:
        logger.warning(
            "Failed student login — student not found | school_id=%s code=%s",
            school_id, student_code,
        )
        return jsonify({"message": "Invalid credentials"}), 401

    auth = StudentAuth.query.filter_by(
        school_id=school_id,
        student_id=student.id,
    ).first()

    if not auth or not check_password_hash(auth.password_hash, password):
        logger.warning(
            "Failed student login — bad credentials | student_id=%s school_id=%s",
            student.id, school_id,
        )
        return jsonify({"message": "Invalid credentials"}), 401

    try:
        access_token  = create_access_token(identity=str(auth.student_id))
        refresh_token = create_refresh_token(identity=str(auth.student_id))

        response = jsonify({
            "message":  "Student login successful",
            "redirect": "/student/portal",
        })

        set_access_cookies(response, access_token)
        set_refresh_cookies(response, refresh_token)

        return response, 200

    except Exception:
        logger.exception(
            "JWT creation failed | student_id=%s school_id=%s",
            auth.student_id, school_id,
        )
        return jsonify({"message": "Login failed. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
#  LOGOUT  —  POST /auth/logout  (GET also accepted for nav)
# ─────────────────────────────────────────────────────────────

@auth_bp.route("/logout", methods=["POST", "GET"])
@jwt_required(verify_type=False)
@limiter.limit(LOGOUT_LIMIT)
def logout():
    try:
        access_jti = get_jwt()["jti"]
        db.session.add(TokenBlocklist(jti=access_jti))

        refresh_token = request.cookies.get("refresh_token_cookie")
        if refresh_token:
            try:
                decoded_refresh = decode_token(refresh_token)
                db.session.add(TokenBlocklist(jti=decoded_refresh["jti"]))
            except Exception:
                # Non-fatal: refresh token may be invalid/expired; log and continue
                logger.warning(
                    "Logout: could not decode refresh token for blocklisting"
                )

        db.session.commit()

    except Exception:
        logger.exception("Logout error during token revocation")
        # Still clear the cookies even if DB write fails
        db.session.rollback()

    response = jsonify({"message": "Logged out successfully"})
    unset_jwt_cookies(response)

    if request.method == "GET":
        return redirect(url_for("auth.login_page"))

    return response, 200


# ─────────────────────────────────────────────────────────────
#  REFRESH  —  POST /auth/refresh
# ─────────────────────────────────────────────────────────────

@auth_bp.route("/refresh", methods=["POST"])
@jwt_required(refresh=True)
@limiter.limit("30 per minute")
def refresh():
    try:
        jwt_data = get_jwt()
        db.session.add(TokenBlocklist(jti=jwt_data["jti"]))

        identity      = get_jwt_identity()
        access_token  = create_access_token(identity=identity)
        refresh_token = create_refresh_token(identity=identity)

        response = jsonify({"message": "Token refreshed"})
        set_access_cookies(response, access_token)
        set_refresh_cookies(response, refresh_token)

        db.session.commit()
        return response, 200

    except Exception:
        logger.exception("Token refresh failed | identity=%s", get_jwt_identity())
        db.session.rollback()
        return jsonify({"message": "Token refresh failed. Please log in again."}), 500


# ─────────────────────────────────────────────────────────────
#  REFRESH HELPER  —  GET /auth/refresh-helper
#  Silent token rotation on F5 / browser page reload.
# ─────────────────────────────────────────────────────────────

@auth_bp.route("/refresh-helper", methods=["GET"])
@jwt_required(refresh=True)
@limiter.limit("30 per minute")
def refresh_get():
    try:
        identity      = get_jwt_identity()
        access_token  = create_access_token(identity=identity)
        refresh_token = create_refresh_token(identity=identity)

        next_url = request.args.get("next", "/")
        response = redirect(next_url)

        set_access_cookies(response, access_token)
        set_refresh_cookies(response, refresh_token)

        return response

    except Exception:
        logger.exception("Refresh-helper failed")
        return redirect(url_for("auth.login_page"))