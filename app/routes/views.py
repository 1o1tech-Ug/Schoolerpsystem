from flask import Blueprint, jsonify, render_template
from flask_jwt_extended import jwt_required, current_user, get_jwt, get_jwt_identity
from app.models.user import User
from app.extensions import limiter
from app.core.rate_limit import READ_LIMIT

import logging

logger = logging.getLogger(__name__)

views_bp = Blueprint("views", __name__)


# =========================================================
# PROTECTED TEST ROUTE
# =========================================================
@views_bp.route("/protected")
@jwt_required()
@limiter.limit(READ_LIMIT)
def protected():
    # Only students should access this route
    if current_user.role not in ["admin", "superadmin", "staff"]:
        return jsonify({"message": "students only"}), 403
    return jsonify({
        "message": "JWT working with user object",
        "user_id": current_user.id,
        "username": current_user.username,
        "role": current_user.role,
        "school_id": current_user.school_id
    }), 200
@views_bp.route("/")
def home():
    return "School ERP is running 🚀"

# =========================================================
# BASE ROUTE (non-superadmin roles)
# =========================================================
@views_bp.route("/base")
@jwt_required()
@limiter.limit(READ_LIMIT)
def base():
    if current_user.role in ["admin", "superadmin", "student"]:
        return jsonify({"message": "staff only"}), 403
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    # extract allowed modules
    modules = [
        m.module_name for m in user.user_modules
    ]
    return render_template(
        "base.html",
        modules=modules,
        user=user
    )


# =========================================================
# ADMIN ROUTE
# =========================================================
@views_bp.route("/admin")
@jwt_required()
@limiter.limit(READ_LIMIT)
def admin():
    claims = get_jwt()
    role = claims.get("role")
    if role != "admin":
        return jsonify({"message": "Admins only"}), 403
    return render_template("admin.html")


# =========================================================
# SUPER ADMIN ROUTE
# =========================================================
@views_bp.route("/superadmin")
@jwt_required()
@limiter.limit(READ_LIMIT)
def superadmin():
    claims = get_jwt()
    role = claims.get("role")
    if role != "superadmin":
        return jsonify({"message": "Superadmin only"}), 403
    return render_template("super_admin.html")
