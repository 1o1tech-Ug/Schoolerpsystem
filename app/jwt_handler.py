from app.extensions import jwt
from flask import jsonify, redirect, request, url_for


# =========================================================
# USER IDENTITY
# =========================================================

@jwt.user_identity_loader
def user_identity(user):
    return str(user)


# =========================================================
# LOAD USER FROM TOKEN
# =========================================================

@jwt.user_lookup_loader
def load_user(_jwt_header, jwt_data):
    from app.models.user import User, StudentAuth

    identity = jwt_data["sub"]
    role     = jwt_data.get("role")

    # Student tokens: return the StudentAuth row — NOT None.
    # Returning None triggers user_lookup_error_loader → 404
    # "User no longer exists", which was the original bug.
    if role == "student":
        try:
            student_id = int(identity)
        except (ValueError, TypeError):
            return None
        return StudentAuth.query.filter_by(student_id=student_id).first()

    # Staff tokens
    try:
        user_id = int(identity)
    except (ValueError, TypeError):
        return None

    return User.query.get(user_id)


# =========================================================
# ADDITIONAL CLAIMS
# =========================================================

@jwt.additional_claims_loader
def add_claims(identity):
    from app.models.user import User, StudentAuth

    try:
        uid = int(identity)
    except (ValueError, TypeError):
        return {"role": "unknown", "school_id": None}

    # Staff first
    user = User.query.get(uid)
    if user is not None:
        return {
            "role":      user.role,
            "school_id": user.school_id,
        }

    # Student fallback
    auth = StudentAuth.query.filter_by(student_id=uid).first()
    if auth is not None:
        return {
            "role":      "student",
            "school_id": auth.school_id,
        }

    return {"role": "unknown", "school_id": None}


# =========================================================
# EXPIRED TOKEN
# =========================================================

@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    if (
        request.blueprint != "auth"
        and "text/html" in request.headers.get("Accept", "")
    ):
        return redirect(url_for("auth.refresh_get", next=request.url))

    return jsonify({
        "success":     False,
        "code":        "TOKEN_EXPIRED",
        "refresh_url": "/auth/refresh",
    }), 401


# =========================================================
# INVALID TOKEN
# =========================================================

@jwt.invalid_token_loader
def invalid_token_callback(error):
    return jsonify({
        "success": False,
        "message": "Invalid token",
    }), 422


# =========================================================
# MISSING TOKEN
# =========================================================

from flask import redirect, url_for

@jwt.unauthorized_loader
def missing_token_callback(error):
    return redirect(url_for("auth.login_page"))


# =========================================================
# USER NOT FOUND
# =========================================================

@jwt.user_lookup_error_loader
def user_not_found_callback(jwt_header, jwt_data):
    return jsonify({
        "success": False,
        "message": "User no longer exists",
    }), 404


# =========================================================
# BLOCKLIST CHECK
# =========================================================

@jwt.token_in_blocklist_loader
def check_if_token_revoked(jwt_header, jwt_payload):
    from app.models.blocklist import TokenBlocklist

    jti   = jwt_payload["jti"]
    token = TokenBlocklist.query.filter_by(jti=jti).first()
    return token is not None


# =========================================================
# REVOKED TOKEN
# =========================================================

@jwt.revoked_token_loader
def revoked_callback(jwt_header, jwt_payload):
    return jsonify({"message": "Token has been revoked"}), 401