from app.extensions import jwt
from flask import jsonify

from app.models.user import User
from app.models.core import School


# =========================================================
# JWT MANAGER
# =========================================================
jwt = JWTManager()


# =========================================================
# USER IDENTITY (what goes into JWT "sub")
# =========================================================
@jwt.user_identity_loader
def user_identity(user):
    return str(user.id)


# =========================================================
# LOAD USER FROM TOKEN
# Converts JWT identity → User object
# Enables current_user
# =========================================================
@jwt.user_lookup_loader
def load_user(_jwt_header, jwt_data):

    identity = jwt_data["sub"]

    return User.query.get(identity)


# =========================================================
# ADDITIONAL CLAIMS (optional role / school info)
# =========================================================
@jwt.additional_claims_loader
def add_claims(user):

    return {
        "role": user.role,
        "school_id": user.school_id
    }


# =========================================================
# EXPIRED TOKEN
# =========================================================
@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):

    return jsonify({
        "success": False,
        "message": "Token has expired"
    }), 401


# =========================================================
# INVALID TOKEN
# =========================================================
@jwt.invalid_token_loader
def invalid_token_callback(error):

    return jsonify({
        "success": False,
        "message": "Invalid token"
    }), 422


# =========================================================
# MISSING TOKEN
# =========================================================
@jwt.unauthorized_loader
def missing_token_callback(error):

    return jsonify({
        "success": False,
        "message": "Authorization token required"
    }), 401


# =========================================================
# USER NOT FOUND (token valid but user deleted)
# =========================================================
@jwt.user_lookup_error_loader
def user_not_found_callback(jwt_header, jwt_data):

    return jsonify({
        "success": False,
        "message": "User no longer exists"
    }), 404