from flask import Blueprint, render_template, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity
from werkzeug.security import check_password_hash, generate_password_hash
from app.extensions import db
from app.models.user import User
from app.models.core import UserModule

settings = Blueprint("settings", __name__, url_prefix="/settings")
settings_api = Blueprint("settings_api", __name__, url_prefix="/api/settings")


STAFF_ROLES = {"staff","admin"}


def staff_required():
    claims = get_jwt()
    if claims.get("role") not in STAFF_ROLES:
        return jsonify({"message": "Unauthorized"}), 403
    return None


# ─────────────────────────────────────────────────────────────
# HELPER – load current user from JWT
# ─────────────────────────────────────────────────────────────
def _current_user():
    user_id = get_jwt_identity()
    return User.query.get(int(user_id))


# ─────────────────────────────────────────────────────────────
# PAGE  –  GET /settings/
# ─────────────────────────────────────────────────────────────
@settings.route("/", methods=["GET"])
@jwt_required()
def settings_page():
    guard = staff_required()
    if guard:
        return guard
    claims  = get_jwt()
    user_id = int(claims.get("sub"))

    user    = User.query.get(user_id)
    if not user:
        return "User not found", 404

    modules = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]

    return render_template(
        "settings/settings.html",
        current_user=user,
        modules=modules,
    )


			