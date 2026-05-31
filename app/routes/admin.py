"""
app/routes/admin.py
====================
Admin blueprint — terms, fee structures, staff management, data control.

CHANGES vs original:
  - Rate limits applied per endpoint sensitivity.
  - All except blocks now log internally and return safe messages.
    No str(e) or raw exception details reach the client.
  - print() calls removed; replaced with logger calls where appropriate.
  - factory_reset exception handler returns safe message.
"""

import os
import logging
from datetime import timedelta, datetime, date
from functools import wraps
import re
from flask import Blueprint, render_template, request, jsonify, send_file
from flask_jwt_extended import jwt_required, get_jwt
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func

from app.extensions import db, limiter
from app.models.core import UserModule
from app.models.user import User
from app.models.people import Student, Staff, Guardian, MedicalRecord, Document, StudentAcademic
from app.models.finance import Invoice, InvoiceItem, Expenses, Payment, Receipt
from app.models.academic_structure import (
    Term, AcademicYear, AcademicConfig, Class, Stream
)
from app.models.finance import FeeStructure, FeeItem
from app.utils.utilities import generate_invoices_for_term, generate_staff_code
from app.utils.bunny import bunny_upload, bunny_delete, bunny_remote_path_from_url
from app.core.rate_limit import (
    READ_LIMIT, WRITE_LIMIT, BULK_LIMIT,
    FACTORY_RESET_LIMIT, EXPORT_LIMIT, PASSWORD_RESET_LIMIT,
)

import csv
import io
import uuid
import zipfile
import requests as http_requests

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__)


# =====================================================
# HELPERS
# =====================================================

def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        claims = get_jwt()
        if claims.get("role") != "admin":
            return jsonify({"error": "Admin access required"}), 403
        if not claims.get("school_id"):
            return jsonify({"error": "Missing school context"}), 403
        return fn(*args, **kwargs)
    return wrapper


def get_school_id():
    return get_jwt().get("school_id")


def _delete_cdn_file(url):
    """Non-fatal delete of a CDN file given its public URL."""
    if url:
        try:
            bunny_delete(bunny_remote_path_from_url(url))
        except Exception:
            logger.warning("CDN delete failed for URL: %s", url)


# =====================================================
# PASSWORD VALIDATION
# =====================================================

def validate_password_strength(password):
    if len(password) < 8:
        return "Password must be at least 8 characters long"
    if not re.search(r"[A-Z]", password):
        return "Password must contain at least one uppercase letter"
    if not re.search(r"[a-z]", password):
        return "Password must contain at least one lowercase letter"
    if not re.search(r"\d", password):
        return "Password must contain at least one number"
    if not re.search(r"[!@#$%^&*()_\-+=\[{\]};:<>|./?]", password):
        return "Password must contain at least one special character"
    return None


# =====================================================
# PAGES
# =====================================================

@admin_bp.route("/admin/terms")
@admin_required
@limiter.limit(READ_LIMIT)
def terms_page():
    return render_template("admin_pages/term.html")


@admin_bp.route("/admin/fee-structures")
@admin_required
@limiter.limit(READ_LIMIT)
def fee_structures_page():
    return render_template("admin_pages/fee_structure.html")


# =====================================================
# TERMS
# =====================================================

@admin_bp.route("/admin/api/terms", methods=["GET"])
@admin_required
@limiter.limit(READ_LIMIT)
def get_terms():
    school_id = get_school_id()

    terms = Term.query.filter_by(school_id=school_id).order_by(Term.id.desc()).all()
    active_term = Term.query.filter_by(school_id=school_id, status="active").first()

    current_year_name = None
    if active_term:
        year = AcademicYear.query.get(active_term.academic_year_id)
        if year:
            current_year_name = year.name

    return jsonify({
        "has_active_term": bool(active_term),
        "current_term":    active_term.name if active_term else "No active term",
        "current_year":    current_year_name,
        "terms": [
            {
                "id":         t.id,
                "year": (
                    AcademicYear.query.get(t.academic_year_id).name
                    if AcademicYear.query.get(t.academic_year_id) else ""
                ),
                "name":       t.name,
                "start_date": str(t.start_date),
                "end_date":   str(t.end_date),
                "status":     t.status,
            }
            for t in terms
        ],
    })


@admin_bp.route("/admin/api/terms", methods=["POST"])
@admin_required
@limiter.limit(WRITE_LIMIT)
def create_term():
    school_id = get_school_id()
    data = request.json

    active_term = Term.query.filter_by(school_id=school_id, status="active").first()
    if active_term:
        return jsonify({"error": "End active term first"}), 400

    current_year = str(datetime.now().year)
    academic_year = AcademicYear.query.filter_by(name=current_year, is_active=True).first()

    if not academic_year:
        academic_year = AcademicYear(
            name=current_year,
            start_date=date(datetime.now().year, 1, 1),
            end_date=date(datetime.now().year, 12, 31),
            is_active=True,
        )
        db.session.add(academic_year)
        db.session.flush()

    exists = Term.query.filter_by(
        school_id=school_id,
        academic_year_id=academic_year.id,
        name=data["name"],
    ).first()
    if exists:
        return jsonify({"error": "Term already exists"}), 400

    try:
        term = Term(
            school_id=school_id,
            academic_year_id=academic_year.id,
            name=data["name"],
            start_date=datetime.strptime(data["start_date"], "%Y-%m-%d").date(),
            end_date=datetime.strptime(data["end_date"],   "%Y-%m-%d").date(),
            status="draft",
        )
        db.session.add(term)
        db.session.commit()
        return jsonify({"message": "Term created", "academic_year": academic_year.name})
    except Exception:
        db.session.rollback()
        logger.exception("create_term failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to create term. Please try again."}), 500


@admin_bp.route("/admin/api/terms/<int:term_id>/activate", methods=["POST"])
@admin_required
@limiter.limit(WRITE_LIMIT)
def activate_term(term_id):
    school_id = get_school_id()

    term = Term.query.filter_by(id=term_id, school_id=school_id).first_or_404()

    fee_exists = FeeStructure.query.filter_by(school_id=school_id, term_id=term.id).first()
    if not fee_exists:
        return jsonify({"error": "Create fee structures before activation"}), 400

    try:
        Term.query.filter_by(school_id=school_id).update({"status": "locked"})
        term.status = "active"

        config = AcademicConfig.query.filter_by(school_id=school_id).first()
        if not config:
            config = AcademicConfig(school_id=school_id)
            db.session.add(config)
            db.session.flush()

        config.current_term_id          = term.id
        config.current_academic_year_id = term.academic_year_id

        created = generate_invoices_for_term(school_id, term)

        db.session.commit()
        return jsonify({"message": "Term activated", "invoices_created": created})
    except Exception:
        db.session.rollback()
        logger.exception("activate_term failed | term_id=%s school_id=%s", term_id, school_id)
        return jsonify({"error": "Failed to activate term. Please try again."}), 500


@admin_bp.route("/admin/api/terms/<int:term_id>/end", methods=["POST"])
@admin_required
@limiter.limit(WRITE_LIMIT)
def end_term(term_id):
    school_id = get_school_id()
    term = Term.query.filter_by(id=term_id, school_id=school_id).first_or_404()
    if term.status != "active":
        return jsonify({"error": "Only active term can be ended"}), 400
    try:
        term.status = "locked"
        db.session.commit()
        return jsonify({"message": "Term ended"})
    except Exception:
        db.session.rollback()
        logger.exception("end_term failed | term_id=%s", term_id)
        return jsonify({"error": "Failed to end term. Please try again."}), 500


@admin_bp.route("/admin/api/terms/<int:term_id>", methods=["DELETE"])
@admin_required
@limiter.limit(WRITE_LIMIT)
def delete_term(term_id):
    school_id = get_school_id()
    term = Term.query.filter_by(id=term_id, school_id=school_id).first_or_404()
    if term.status != "draft":
        return jsonify({"error": "Only draft terms can be deleted"}), 400
    try:
        db.session.delete(term)
        db.session.commit()
        return jsonify({"message": "Term deleted"})
    except Exception:
        db.session.rollback()
        logger.exception("delete_term failed | term_id=%s", term_id)
        return jsonify({"error": "Failed to delete term. Please try again."}), 500


# =====================================================
# CLASSES
# =====================================================

@admin_bp.route("/admin/api/classes")
@admin_required
@limiter.limit(READ_LIMIT)
def get_classes():
    school_id = get_school_id()
    classes = Class.query.filter_by(school_id=school_id).all()
    return jsonify({"classes": [{"id": c.id, "name": c.name} for c in classes]})


# =====================================================
# FEE STRUCTURES
# =====================================================

@admin_bp.route("/admin/api/fee-structures")
@jwt_required()
@admin_required
@limiter.limit(READ_LIMIT)
def get_fee_structures():
    school_id = get_school_id()

    year_filter = request.args.get("year")
    term_filter = request.args.get("term")

    query = FeeStructure.query.filter_by(school_id=school_id)

    if year_filter:
        year_obj = AcademicYear.query.filter_by(name=year_filter).first()
        if year_obj:
            query = query.filter_by(academic_year_id=year_obj.id)

    if term_filter:
        term_ids = [
            t.id for t in
            Term.query.filter_by(school_id=school_id, name=term_filter).all()
        ]
        if term_ids:
            query = query.filter(FeeStructure.term_id.in_(term_ids))

    structures = query.order_by(FeeStructure.id.desc()).all()

    data = []
    for s in structures:
        term  = Term.query.get(s.term_id)
        year  = AcademicYear.query.get(s.academic_year_id)
        cls   = Class.query.get(s.class_id)
        items = [{"fee_type": i.fee_type, "amount": i.amount} for i in s.items]

        data.append({
            "id":           s.id,
            "class_name":   cls.name  if cls  else "",
            "term_name":    term.name if term else "",
            "year_name":    year.name if year else "",
            "student_type": s.student_type,
            "total_amount": s.total_amount,
            "status":       term.status if term else "draft",
            "items":        items,
        })

    return jsonify({"fee_structures": data})


@admin_bp.route("/admin/api/fee-structures", methods=["POST"])
@jwt_required()
@admin_required
@limiter.limit(WRITE_LIMIT)
def create_fee_structures():
    data = request.get_json()
    school_id = get_school_id()

    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return jsonify({"error": "Invalid payload format"}), 400
    if not data:
        return jsonify({"error": "No data"}), 400

    try:
        for item in data:
            term = Term.query.filter_by(id=item["term_id"], school_id=school_id).first()
            if not term:
                return jsonify({"error": "Invalid term"}), 400
            if term.status == "active":
                return jsonify({"error": "Cannot edit active term"}), 400

            existing = FeeStructure.query.filter_by(
                school_id=school_id,
                class_id=item["class_id"],
                term_id=item["term_id"],
                student_type=item["student_type"],
            ).first()
            if existing:
                continue

            structure = FeeStructure(
                school_id=school_id,
                class_id=item["class_id"],
                term_id=item["term_id"],
                academic_year_id=term.academic_year_id,
                student_type=item["student_type"],
                total_amount=item["total_amount"],
                status="draft",
            )
            db.session.add(structure)
            db.session.flush()

            for it in item.get("items", []):
                db.session.add(FeeItem(
                    fee_structure_id=structure.id,
                    fee_type=it["fee_type"],
                    amount=it["amount"],
                ))

        db.session.commit()
        return jsonify({"message": "Fee structures saved"})
    except Exception:
        db.session.rollback()
        logger.exception("create_fee_structures failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to save fee structures. Please try again."}), 500


@admin_bp.route("/admin/api/fee-structures/<int:id>", methods=["DELETE"])
@jwt_required()
@admin_required
@limiter.limit(WRITE_LIMIT)
def delete_fee_structure(id):
    school_id = get_school_id()

    fs   = FeeStructure.query.filter_by(id=id, school_id=school_id).first_or_404()
    term = Term.query.filter_by(id=fs.term_id, school_id=school_id).first()

    if not term:
        return jsonify({"error": "Associated term not found"}), 404

    if term.status in ("active", "locked"):
        return jsonify({"error": "Cannot delete fee structures for active or locked terms"}), 400

    try:
        db.session.delete(fs)
        db.session.commit()
        return jsonify({"message": "Deleted successfully"})
    except Exception:
        db.session.rollback()
        logger.exception("delete_fee_structure failed | id=%s school_id=%s", id, school_id)
        return jsonify({"error": "Failed to delete fee structure. Please try again."}), 500


# =====================================================
# STAFF
# =====================================================

@admin_bp.route("/admin/api/staff", methods=["POST"])
@jwt_required()
@admin_required
@limiter.limit(WRITE_LIMIT)
def create_staff():
    school_id = get_school_id()
    data      = request.form
    photo     = request.files.get("photo")
    staff_id  = data.get("staff_id")

    try:
        if staff_id:
            staff = Staff.query.filter_by(
                id=int(staff_id), school_id=school_id
            ).first_or_404()

            staff.first_name = data.get("first_name")
            staff.last_name  = data.get("last_name")
            staff.gender     = data.get("gender")
            staff.phone      = data.get("contact")
            staff.staff_type = data.get("staff_type")

            if photo and photo.filename:
                _delete_cdn_file(staff.photo_url)
                staff.photo_url = _upload_staff_photo(photo, school_id, staff.staff_code)

            db.session.commit()
            return jsonify({"message": "Staff updated successfully"})

        staff = Staff(
            school_id=school_id,
            staff_code=generate_staff_code(school_id),
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
            gender=data.get("gender"),
            phone=data.get("contact"),
            staff_type=data.get("staff_type"),
            photo_url=None,
        )
        db.session.add(staff)
        db.session.flush()

        if photo and photo.filename:
            staff.photo_url = _upload_staff_photo(photo, school_id, staff.staff_code)

        db.session.commit()
        return jsonify({"message": "Staff created successfully"})

    except Exception:
        db.session.rollback()
        logger.exception("create_staff failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to save staff record. Please try again."}), 500


def _upload_staff_photo(file_storage, school_id, staff_code):
    ext         = file_storage.filename.rsplit(".", 1)[1].lower()
    fname       = f"{school_id}_{staff_code}_{uuid.uuid4().hex}.{ext}"
    remote_path = f"uploads/images/{fname}"
    data        = file_storage.read()
    file_storage.seek(0)
    return bunny_upload(data=data, remote_path=remote_path)


@admin_bp.route("/admin/api/staff", methods=["GET"])
@admin_required
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_staff():
    school_id  = get_school_id()
    staff_list = Staff.query.filter_by(school_id=school_id).order_by(Staff.id.desc()).all()

    return jsonify({
        "staff": [
            {
                "id":         s.id,
                "staff_code": s.staff_code,
                "first_name": s.first_name,
                "last_name":  s.last_name,
                "contact":    s.phone,
                "staff_type": s.staff_type,
                "photo":      s.photo_url,
            }
            for s in staff_list
        ]
    })


@admin_bp.route("/admin/api/staff/<int:staff_id>")
@admin_required
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_single_staff_api(staff_id):
    school_id = get_school_id()
    staff = Staff.query.filter_by(id=staff_id, school_id=school_id).first_or_404()
    return render_template("admin_pages/staff_profile_view.html", staff=staff)


@admin_bp.route("/admin/api/staff/<int:staff_id>", methods=["DELETE"])
@admin_required
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_staff(staff_id):
    school_id = get_school_id()
    staff = Staff.query.filter_by(id=staff_id, school_id=school_id).first_or_404()

    try:
        _delete_cdn_file(staff.photo_url)
        db.session.delete(staff)
        db.session.commit()
        return jsonify({"message": "Staff deleted"})
    except Exception:
        db.session.rollback()
        logger.exception("delete_staff failed | staff_id=%s school_id=%s", staff_id, school_id)
        return jsonify({"error": "Failed to delete staff. Please try again."}), 500


@admin_bp.route("/admin/staff/add")
@jwt_required()
@admin_required
@limiter.limit(READ_LIMIT)
def add_staff_page():
    return render_template("admin_pages/add_staff.html")


@admin_bp.route("/admin/staff/profiles")
@jwt_required()
@admin_required
@limiter.limit(READ_LIMIT)
def staff_profiles_page():
    return render_template("admin_pages/staff_profiles.html")


@admin_bp.route("/admin/roles-permissions")
@jwt_required()
@admin_required
@limiter.limit(READ_LIMIT)
def roles_permissions_page():
    school_id  = get_school_id()
    staff_list = Staff.query.filter_by(school_id=school_id).all()
    users      = User.query.filter_by(school_id=school_id, role="staff").all()
    user_map   = {u.username: u for u in users}

    return render_template(
        "admin_pages/roles_permissions.html",
        staff_list=staff_list,
        user_map=user_map,
    )


@admin_bp.route("/admin/api/staff-account/<staff_code>")
@jwt_required()
@admin_required
@limiter.limit(READ_LIMIT)
def get_staff_account(staff_code):
    school_id = get_school_id()

    user = User.query.filter_by(
        school_id=school_id, role="staff", username=staff_code
    ).first()

    if not user:
        return jsonify({"user": None, "modules": []})

    return jsonify({
        "user": {"id": user.id, "username": user.username, "status": user.status},
        "modules": [m.module_name for m in user.user_modules],
    })


@admin_bp.route("/admin/staff/edit/<int:staff_id>")
@admin_required
@limiter.limit(READ_LIMIT)
def edit_staff_page(staff_id):
    school_id = get_school_id()
    staff = Staff.query.filter_by(id=staff_id, school_id=school_id).first_or_404()
    return render_template("admin_pages/add_staff.html", staff=staff)


@admin_bp.route("/admin/api/staff-account/save", methods=["POST"])
@jwt_required()
@admin_required
@limiter.limit(WRITE_LIMIT)
def save_staff_account():
    data       = request.get_json()
    school_id  = get_school_id()
    staff_code = data["staff_code"]
    password   = data.get("password", "").strip()

    staff = Staff.query.filter_by(
        school_id=school_id,
        staff_code=staff_code
    ).first_or_404()

    user = User.query.filter_by(
        school_id=school_id,
        role="staff",
        staff_id=staff.id,
        username=staff_code
    ).first()

    try:
        if not user:
            if not password:
                return jsonify({"error": "Password required"}), 400

            validation_error = validate_password_strength(password)
            if validation_error:
                return jsonify({"error": validation_error}), 400

            user = User(
                username=staff_code,
                role="staff",
                staff_id=staff.id,
                school_id=school_id,
                password_hash=generate_password_hash(password),
                status="active",
            )
            db.session.add(user)
            db.session.flush()

        else:
            if password:
                validation_error = validate_password_strength(password)
                if validation_error:
                    return jsonify({"error": validation_error}), 400
                user.password_hash = generate_password_hash(password)

        UserModule.query.filter_by(user_id=user.id).delete()
        for m in data.get("modules", []):
            db.session.add(UserModule(user_id=user.id, module_name=m))

        db.session.commit()
        return jsonify({"message": "Account saved"})

    except Exception:
        db.session.rollback()
        logger.exception("save_staff_account failed | school_id=%s staff_code=%s", school_id, staff_code)
        return jsonify({"error": "Failed to save account. Please try again."}), 500


@admin_bp.route("/admin/api/staff-account/<int:user_id>", methods=["DELETE"])
@jwt_required()
@admin_required
@limiter.limit(WRITE_LIMIT)
def delete_staff_account(user_id):
    school_id = get_school_id()
    user = User.query.filter_by(id=user_id, school_id=school_id, role="staff").first_or_404()
    try:
        UserModule.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
        return jsonify({"message": "Account deleted"})
    except Exception:
        db.session.rollback()
        logger.exception("delete_staff_account failed | user_id=%s", user_id)
        return jsonify({"error": "Failed to delete account. Please try again."}), 500


@admin_bp.route("/admin/api/staff-account/<int:user_id>/lock", methods=["POST"])
@jwt_required()
@admin_required
@limiter.limit(WRITE_LIMIT)
def lock_staff_account(user_id):
    school_id = get_school_id()
    user = User.query.filter_by(id=user_id, school_id=school_id, role="staff").first_or_404()
    try:
        user.status = "disabled"
        db.session.commit()
        return jsonify({"message": "Account locked"})
    except Exception:
        db.session.rollback()
        logger.exception("lock_staff_account failed | user_id=%s", user_id)
        return jsonify({"error": "Failed to lock account. Please try again."}), 500


@admin_bp.route("/admin/api/staff-account/<int:user_id>/unlock", methods=["POST"])
@jwt_required()
@admin_required
@limiter.limit(WRITE_LIMIT)
def unlock_staff_account(user_id):
    school_id = get_school_id()
    user = User.query.filter_by(id=user_id, school_id=school_id, role="staff").first_or_404()
    try:
        user.status = "active"
        db.session.commit()
        return jsonify({"message": "Account unlocked"})
    except Exception:
        db.session.rollback()
        logger.exception("unlock_staff_account failed | user_id=%s", user_id)
        return jsonify({"error": "Failed to unlock account. Please try again."}), 500


@admin_bp.route("/admin/system-settings")
@jwt_required()
@admin_required
@limiter.limit(READ_LIMIT)
def system_settings_page():
    return render_template("admin_pages/system_settings.html")


@admin_bp.route("/api/settings/change-password", methods=["POST"])
@jwt_required()
@admin_required
@limiter.limit(PASSWORD_RESET_LIMIT)
def change_admin_password():
    data      = request.get_json()
    school_id = get_school_id()
    user_id   = get_jwt()["sub"]

    user = User.query.filter_by(
        id=user_id, school_id=school_id, role="admin"
    ).first_or_404()

    if not check_password_hash(user.password_hash, data["current_password"]):
        return jsonify({"error": "Current password is incorrect"}), 400

    try:
        user.password_hash = generate_password_hash(data["new_password"])
        db.session.commit()
        return jsonify({"message": "Password updated"})
    except Exception:
        db.session.rollback()
        logger.exception("change_admin_password failed | user_id=%s", user_id)
        return jsonify({"error": "Failed to update password. Please try again."}), 500


# =====================================================
# DASHBOARD
# =====================================================

@admin_bp.route("/admin/dashboard")
@admin_required
@limiter.limit(READ_LIMIT)
def dashboard_page():
    return render_template("admin_pages/dashboard.html")


@admin_bp.route("/admin/api/dashboard")
@jwt_required()
@admin_required
@limiter.limit(READ_LIMIT)
def dashboard():
    school_id = get_school_id()

    try:
        active_term = Term.query.filter_by(school_id=school_id, status="active").first()
        active_term_name = active_term.name if active_term else None
        active_term_id   = active_term.id   if active_term else None

        total_students = Student.query.filter_by(school_id=school_id).count()
        teaching_staff = Staff.query.filter_by(school_id=school_id, staff_type="teaching").count()

        fees_collected = 0.0
        if active_term_id:
            result = db.session.query(func.sum(Payment.amount)).join(
                Invoice, Invoice.id == Payment.invoice_id
            ).filter(
                Invoice.school_id == school_id,
                Invoice.term_id   == active_term_id,
                Payment.status    == "completed",
            ).scalar()
            fees_collected = float(result or 0)

        total_expenses = 0.0
        if active_term_id:
            result = db.session.query(func.sum(Expenses.amount)).filter(
                Expenses.school_id == school_id,
                Expenses.term_id   == active_term_id,
            ).scalar()
            total_expenses = float(result or 0)

        fees_outstanding = 0.0
        if active_term_id:
            total_invoiced = db.session.query(func.sum(Invoice.total_amount)).filter(
                Invoice.school_id == school_id,
                Invoice.term_id   == active_term_id,
            ).scalar() or 0
            fees_outstanding = float(total_invoiced) - fees_collected

        recent_terms = list(reversed(
            Term.query.filter_by(school_id=school_id).order_by(Term.id.desc()).limit(6).all()
        ))
        enrollment_trend = []
        for t in recent_terms:
            count = db.session.query(
                func.count(Invoice.student_id.distinct())
            ).filter(
                Invoice.school_id == school_id,
                Invoice.term_id   == t.id,
            ).scalar() or 0
            year = AcademicYear.query.get(t.academic_year_id)
            enrollment_trend.append({
                "label": f"{t.name} {year.name if year else ''}",
                "count": count,
            })

        return jsonify({
            "active_term": active_term_name,
            "kpis": {
                "total_students":   total_students,
                "teaching_staff":   teaching_staff,
                "fees_collected":   fees_collected,
                "total_expenses":   total_expenses,
                "fees_outstanding": max(fees_outstanding, 0),
            },
            "enrollment_trend": enrollment_trend,
            "financial_summary": {
                "collected":   fees_collected,
                "expenses":    total_expenses,
                "outstanding": max(fees_outstanding, 0),
            },
        })

    except Exception:
        logger.exception("dashboard failed | school_id=%s", school_id)
        return jsonify({"error": "Failed to load dashboard data."}), 500


# =====================================================
# DATA CONTROL
# =====================================================

@admin_bp.route("/admin/data-control")
@admin_required
@limiter.limit(READ_LIMIT)
def data_control_page():
    return render_template("admin_pages/data_control.html")


# ── CSV helpers ───────────────────────────────────────────────

def rows_to_csv(headers, rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def model_to_rows(objects, fields):
    return [{f: getattr(obj, f, "") for f in fields} for obj in objects]


def _export_guardians(school_id):
    student_ids = [
        s.id for s in
        Student.query.filter_by(school_id=school_id).with_entities(Student.id).all()
    ]
    guardians = Guardian.query.filter(Guardian.student_id.in_(student_ids)).all()
    return model_to_rows(
        guardians,
        ["id", "student_id", "name", "relationship", "contact", "address", "occupation"],
    )


def _export_receipts(school_id):
    payment_ids = [
        p.id for p in
        Payment.query.filter_by(school_id=school_id).with_entities(Payment.id).all()
    ]
    receipts = Receipt.query.filter(Receipt.payment_id.in_(payment_ids)).all()
    return model_to_rows(receipts, ["id", "payment_id", "receipt_number", "issued_at"])


def get_table_exporters(school_id):
    return {
        "students": (
            "students.csv",
            lambda: model_to_rows(
                Student.query.filter_by(school_id=school_id).all(),
                ["id", "student_code", "admission_number", "first_name", "last_name",
                 "gender", "date_of_birth", "nationality", "residence",
                 "enrollment_type", "student_type", "class_id", "created_at"],
            )
        ),
        "staff": (
            "staff.csv",
            lambda: model_to_rows(
                Staff.query.filter_by(school_id=school_id).all(),
                ["id", "staff_code", "first_name", "last_name",
                 "gender", "phone", "staff_type", "created_at"],
            )
        ),
        "invoices": (
            "invoices.csv",
            lambda: model_to_rows(
                Invoice.query.filter_by(school_id=school_id).all(),
                ["id", "student_id", "term_id", "year_id", "total_amount", "created_at"],
            )
        ),
        "payments": (
            "payments.csv",
            lambda: model_to_rows(
                Payment.query.filter_by(school_id=school_id).all(),
                ["id", "student_id", "invoice_id", "amount", "method",
                 "reference", "status", "created_at"],
            )
        ),
        "expenses": (
            "expenses.csv",
            lambda: model_to_rows(
                Expenses.query.filter_by(school_id=school_id).all(),
                ["id", "term_id", "year_id", "title", "category",
                 "amount", "date", "status", "payment_method", "created_at"],
            )
        ),
        "fee_structures": (
            "fee_structures.csv",
            lambda: model_to_rows(
                FeeStructure.query.filter_by(school_id=school_id).all(),
                ["id", "class_id", "term_id", "academic_year_id",
                 "student_type", "total_amount", "status", "created_at"],
            )
        ),
        "terms": (
            "terms.csv",
            lambda: model_to_rows(
                Term.query.filter_by(school_id=school_id).all(),
                ["id", "academic_year_id", "name", "start_date", "end_date", "status"],
            )
        ),
        "classes": (
            "classes.csv",
            lambda: model_to_rows(
                Class.query.filter_by(school_id=school_id).all(),
                ["id", "name"],
            )
        ),
        "guardians": ("guardians.csv", lambda: _export_guardians(school_id)),
        "receipts":  ("receipts.csv",  lambda: _export_receipts(school_id)),
    }


@admin_bp.route("/admin/api/data-control/export", methods=["POST"])
@admin_required
@limiter.limit(EXPORT_LIMIT)
def export_data():
    school_id     = get_school_id()
    body          = request.get_json() or {}
    tables        = body.get("tables", [])
    include_files = body.get("include_files", False)

    if not tables:
        return jsonify({"error": "No tables selected"}), 400

    try:
        exporters  = get_table_exporters(school_id)
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for table_key in tables:
                if table_key not in exporters:
                    continue
                filename, row_fn = exporters[table_key]
                rows = row_fn()
                if not rows:
                    zf.writestr(f"data/{filename}", "")
                    continue
                headers = list(rows[0].keys())
                zf.writestr(f"data/{filename}", rows_to_csv(headers, rows))

            if include_files:
                cdn_urls = _collect_cdn_urls(school_id)
                for cdn_url, arcname in cdn_urls:
                    try:
                        resp = http_requests.get(cdn_url, timeout=30)
                        if resp.status_code == 200:
                            zf.writestr(f"files/{arcname}", resp.content)
                    except Exception:
                        logger.warning("export_data: failed to fetch CDN file %s", cdn_url)

            zf.writestr("README.txt", (
                f"1O1 School ERP — Data Export\n"
                f"School ID : {school_id}\n"
                f"Tables    : {', '.join(tables)}\n"
                f"Files     : {'included' if include_files else 'excluded'}\n"
                f"Generated : {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            ))

        zip_buffer.seek(0)
        return send_file(
            zip_buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"school_{school_id}_export.zip",
        )

    except Exception:
        logger.exception("export_data failed | school_id=%s", school_id)
        return jsonify({"error": "Export failed. Please try again."}), 500


def _collect_cdn_urls(school_id):
    urls = []
    student_ids = [
        s.id for s in
        Student.query.filter_by(school_id=school_id).with_entities(Student.id).all()
    ]

    for s in Student.query.filter_by(school_id=school_id).all():
        if s.photo_url:
            urls.append((s.photo_url, f"images/student_{s.id}_{_fname(s.photo_url)}"))

    if student_ids:
        for g in Guardian.query.filter(Guardian.student_id.in_(student_ids)).all():
            photo = getattr(g, "photo_url", None)
            if photo:
                urls.append((photo, f"images/guardian_{g.id}_{_fname(photo)}"))

        for doc in Document.query.filter(Document.student_id.in_(student_ids)).all():
            if doc.file_url:
                urls.append((doc.file_url, f"documents/{doc.id}_{_fname(doc.file_url)}"))

    for st in Staff.query.filter_by(school_id=school_id).all():
        if st.photo_url:
            urls.append((st.photo_url, f"images/staff_{st.id}_{_fname(st.photo_url)}"))

    return urls


def _fname(url: str) -> str:
    return url.rstrip("/").split("/")[-1].split("?")[0] or "file"


@admin_bp.route("/admin/api/data-control/reset", methods=["POST"])
@admin_required
@limiter.limit(FACTORY_RESET_LIMIT)
def factory_reset():
    school_id = get_school_id()

    try:
        student_ids = [
            s.id for s in
            Student.query.filter_by(school_id=school_id).with_entities(Student.id).all()
        ]
        payment_ids = [
            p.id for p in
            Payment.query.filter_by(school_id=school_id).with_entities(Payment.id).all()
        ]
        invoice_ids = [
            i.id for i in
            Invoice.query.filter_by(school_id=school_id).with_entities(Invoice.id).all()
        ]
        fee_structure_ids = [
            f.id for f in
            FeeStructure.query.filter_by(school_id=school_id).with_entities(FeeStructure.id).all()
        ]

        _purge_school_cdn_files(school_id, student_ids)

        if payment_ids:
            Receipt.query.filter(
                Receipt.payment_id.in_(payment_ids)
            ).delete(synchronize_session=False)

        Payment.query.filter_by(school_id=school_id).delete(synchronize_session=False)

        if invoice_ids:
            InvoiceItem.query.filter(
                InvoiceItem.invoice_id.in_(invoice_ids)
            ).delete(synchronize_session=False)

        Invoice.query.filter_by(school_id=school_id).delete(synchronize_session=False)
        Expenses.query.filter_by(school_id=school_id).delete(synchronize_session=False)

        if fee_structure_ids:
            FeeItem.query.filter(
                FeeItem.fee_structure_id.in_(fee_structure_ids)
            ).delete(synchronize_session=False)

        FeeStructure.query.filter_by(school_id=school_id).delete(synchronize_session=False)

        if student_ids:
            Guardian.query.filter(
                Guardian.student_id.in_(student_ids)
            ).delete(synchronize_session=False)
            MedicalRecord.query.filter(
                MedicalRecord.student_id.in_(student_ids)
            ).delete(synchronize_session=False)
            Document.query.filter(
                Document.student_id.in_(student_ids)
            ).delete(synchronize_session=False)
            StudentAcademic.query.filter(
                StudentAcademic.student_id.in_(student_ids)
            ).delete(synchronize_session=False)

        Student.query.filter_by(school_id=school_id).delete(synchronize_session=False)

        staff_list  = Staff.query.filter_by(school_id=school_id).all()
        staff_codes = [s.staff_code for s in staff_list]

        staff_users = User.query.filter(
            User.school_id == school_id,
            User.role      == "staff",
            User.username.in_(staff_codes),
        ).all()
        for u in staff_users:
            UserModule.query.filter_by(user_id=u.id).delete()
            db.session.delete(u)

        Staff.query.filter_by(school_id=school_id).delete(synchronize_session=False)
        Term.query.filter_by(school_id=school_id).delete(synchronize_session=False)
        AcademicConfig.query.filter_by(school_id=school_id).delete(synchronize_session=False)

        class_ids = [
            c.id for c in
            Class.query.filter_by(school_id=school_id).with_entities(Class.id).all()
        ]
        if class_ids:
            Stream.query.filter(
                Stream.class_id.in_(class_ids)
            ).delete(synchronize_session=False)

        Class.query.filter_by(school_id=school_id).delete(synchronize_session=False)

        db.session.commit()
        logger.warning(
            "FACTORY RESET completed | school_id=%s", school_id
        )
        return jsonify({"message": "Factory reset complete"})

    except Exception:
        db.session.rollback()
        logger.exception("factory_reset failed | school_id=%s", school_id)
        return jsonify({"error": "Reset failed. Please try again or contact support."}), 500


def _purge_school_cdn_files(school_id, student_ids):
    for s in Student.query.filter_by(school_id=school_id).all():
        _delete_cdn_file(s.photo_url)

    if student_ids:
        for g in Guardian.query.filter(Guardian.student_id.in_(student_ids)).all():
            _delete_cdn_file(getattr(g, "photo_url", None))

        for doc in Document.query.filter(Document.student_id.in_(student_ids)).all():
            _delete_cdn_file(doc.file_url)

    for st in Staff.query.filter_by(school_id=school_id).all():
        _delete_cdn_file(st.photo_url)