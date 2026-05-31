"""
app/apis/sendreportcards.py
============================
Send Report Cards API.

CHANGES vs original:
  - Rate limits applied per endpoint sensitivity.
  - All except blocks log internally and return safe client messages.
    No str(e) / str(exc) ever reaches the client.
"""

import os
import random
import string
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, render_template, current_app
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import generate_password_hash

from app.extensions import db, limiter
from app.models.user import User, StudentAuth
from app.models.core import School, UserModule
from app.models.reportcards import ReportCard, SchoolDetail
from app.models.people import Student
from app.models.academic_structure import (
    AcademicYear, Term, Stream, Class,
    StudentStream,
)
from app.core.rate_limit import READ_LIMIT, WRITE_LIMIT, BULK_LIMIT

logger = logging.getLogger(__name__)

send_reports_api = Blueprint("send_reports_api", __name__)

VALID_EXAM_TYPES = {"BOT", "MID", "EOT"}
PASSWORD_LENGTH  = 5
PASSWORD_CHARS   = string.digits


# ── Auth guards ───────────────────────────────────────────────────────────────

def _staff_required(claims):
    if claims.get("role") not in {"staff", "teacher"}:
        return None, (jsonify({"message": "Unauthorised — staff access required"}), 403)

    school_id = claims.get("school_id")
    user_id   = claims.get("sub")

    user = User.query.filter_by(id=user_id, school_id=school_id, role="staff").first()
    if not user:
        return None, (jsonify({"message": "User not found"}), 403)
    if not user.staff_id:
        return None, (jsonify({"message": "staff_id missing from user profile"}), 403)

    return user.staff_id, None


def _get_context(claims):
    school_id = claims.get("school_id")
    user_id   = claims.get("sub")
    modules   = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]
    return school_id, user_id, modules


def _school_or_404(school_id):
    school = School.query.get(school_id)
    if not school:
        return None, (jsonify({"message": "School not found"}), 404)
    return school, None


def _generate_password() -> str:
    return "".join(random.choices(PASSWORD_CHARS, k=PASSWORD_LENGTH))


def _bust(url: str, ts: datetime) -> str:
    if not url:
        return url
    return f"{url}?v={int(ts.timestamp())}"


# ═══════════════════════════════════════════════════════════════
#  PAGE  —  GET /send-report-cards
# ═══════════════════════════════════════════════════════════════

@send_reports_api.route("/send-report-cards", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def send_report_cards_page():
    try:
        claims = get_jwt()
        _, err = _staff_required(claims)
        if err:
            return err

        school_id, user_id, modules = _get_context(claims)
        school, err = _school_or_404(school_id)
        if err:
            return err

        classes   = Class.query.filter_by(school_id=school_id).all()
        class_ids = [c.id for c in classes]
        streams   = (
            Stream.query.filter(Stream.class_id.in_(class_ids)).all()
            if class_ids else []
        )
        terms          = Term.query.filter_by(school_id=school_id).order_by(Term.name).all()
        ay_ids         = list({t.academic_year_id for t in terms if t.academic_year_id})
        academic_years = (
            AcademicYear.query.filter(AcademicYear.id.in_(ay_ids)).all()
            if ay_ids else []
        )

        return render_template(
            "modules/academics/send_report_cards.html",
            school=school,
            streams=streams,
            classes=classes,
            terms=terms,
            academic_years=academic_years,
            modules=modules,
        )

    except Exception:
        logger.exception("send_report_cards_page failed")
        return jsonify({"success": False, "message": "Failed to load page. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  STUDENTS LIST  —  GET /api/send-report-cards/students
# ═══════════════════════════════════════════════════════════════

@send_reports_api.route("/send-report-cards/students", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_students_push_status():
    claims = get_jwt()
    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)
    school, err     = _school_or_404(school_id)
    if err:
        return err

    stream_id = request.args.get("stream_id", type=int)
    term_id   = request.args.get("term_id",   type=int)
    exam_type = request.args.get("exam_type", "").strip().upper()

    if not stream_id:
        return jsonify({"message": "stream_id is required"}), 400
    if not term_id:
        return jsonify({"message": "term_id is required"}), 400
    if not exam_type:
        return jsonify({"message": "exam_type is required"}), 400
    if exam_type not in VALID_EXAM_TYPES:
        return jsonify({"message": f"exam_type must be one of {sorted(VALID_EXAM_TYPES)}"}), 400

    stream = Stream.query.get(stream_id)
    if not stream:
        return jsonify({"message": "Stream not found"}), 404

    try:
        ss_rows     = StudentStream.query.filter_by(school_id=school_id, stream_id=stream_id).all()
        student_ids = [ss.student_id for ss in ss_rows]

        if not student_ids:
            return jsonify({"success": True, "students": []}), 200

        students = (
            Student.query
            .filter(Student.school_id == school_id, Student.id.in_(student_ids))
            .order_by(Student.first_name, Student.last_name)
            .all()
        )

        report_map = {
            rc.student_id: rc
            for rc in ReportCard.query.filter(
                ReportCard.school_id  == school_id,
                ReportCard.term_id    == term_id,
                ReportCard.exam_type  == exam_type,
                ReportCard.student_id.in_(student_ids),
            ).all()
        }

        auth_map = {
            sa.student_id: sa
            for sa in StudentAuth.query.filter(
                StudentAuth.school_id  == school_id,
                StudentAuth.student_id.in_(student_ids),
            ).all()
        }

        class_name  = stream.class_.name if stream.class_ else ""
        stream_name = stream.name or ""

        results = []
        for student in students:
            report = report_map.get(student.id)
            auth   = auth_map.get(student.id)
            results.append({
                "id":               student.id,
                "student_code":     student.student_code or "",
                "admission_number": student.admission_number or "",
                "name":             f"{student.first_name} {student.last_name}".strip(),
                "class_name":       class_name,
                "stream_name":      stream_name,
                "report_generated": report is not None,
                "report_id":        report.id if report else None,
                "report_url":       _bust(report.firebase_url, report.generated_at) if report and report.firebase_url else None,
                "pushed":           auth is not None,
                "pushed_exam_type": auth.exam_type if auth and hasattr(auth, "exam_type") else None,
                "pushed_at":        auth.created_at.isoformat() if auth and hasattr(auth, "created_at") and auth.created_at else None,
                "student_login_code": student.student_code or "",
            })

        return jsonify({"success": True, "students": results}), 200

    except Exception:
        logger.exception("get_students_push_status failed | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Failed to load students."}), 500


# ═══════════════════════════════════════════════════════════════
#  PUSH REPORT CARDS  —  POST /api/send-report-cards/push
# ═══════════════════════════════════════════════════════════════

@send_reports_api.route("/send-report-cards/push", methods=["POST"])
@jwt_required()
@limiter.limit(BULK_LIMIT)
def push_report_cards():
    claims = get_jwt()
    _, err = _staff_required(claims)
    if err:
        return err

    school_id, user_id, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    data        = request.get_json(force=True) or {}
    stream_id   = data.get("stream_id")
    term_id     = data.get("term_id")
    exam_type   = str(data.get("exam_type", "")).strip().upper()
    student_ids = data.get("student_ids", "all")

    if not stream_id or not term_id or not exam_type:
        return jsonify({"message": "stream_id, term_id and exam_type are required"}), 400
    if exam_type not in VALID_EXAM_TYPES:
        return jsonify({"message": f"exam_type must be one of {sorted(VALID_EXAM_TYPES)}"}), 400

    stream = Stream.query.get(stream_id)
    if not stream:
        return jsonify({"message": "Stream not found"}), 404

    term = Term.query.filter_by(id=term_id, school_id=school_id).first()
    if not term:
        return jsonify({"message": "Term not found"}), 404

    if student_ids == "all":
        ss_rows    = StudentStream.query.filter_by(school_id=school_id, stream_id=stream_id).all()
        target_ids = [ss.student_id for ss in ss_rows]
    else:
        if not isinstance(student_ids, list):
            return jsonify({"message": "student_ids must be a list or 'all'"}), 400
        target_ids = [int(i) for i in student_ids]

    if not target_ids:
        return jsonify({"success": True, "message": "No students to push", "pushed": [], "failed": []}), 200

    try:
        report_map = {
            rc.student_id: rc
            for rc in ReportCard.query.filter(
                ReportCard.school_id  == school_id,
                ReportCard.term_id    == term_id,
                ReportCard.exam_type  == exam_type,
                ReportCard.student_id.in_(target_ids),
            ).all()
        }

        students = (
            Student.query
            .filter(Student.school_id == school_id, Student.id.in_(target_ids))
            .all()
        )
        student_map = {s.id: s for s in students}

        existing_auth = {
            sa.student_id: sa
            for sa in StudentAuth.query.filter(
                StudentAuth.school_id  == school_id,
                StudentAuth.student_id.in_(target_ids),
            ).all()
        }

        pushed  = []
        failed  = []
        now     = datetime.utcnow()

        for sid in target_ids:
            student = student_map.get(sid)
            if not student:
                failed.append({"student_id": sid, "reason": "Student not found"})
                continue

            if sid not in report_map:
                failed.append({
                    "student_id": sid,
                    "name": f"{student.first_name} {student.last_name}",
                    "reason": "No report card generated for this term/exam yet",
                })
                continue

            plain_password = _generate_password()
            hashed         = generate_password_hash(plain_password)

            auth = existing_auth.get(sid)
            if auth:
                auth.password_hash = hashed
                auth.term_id       = term_id
                auth.status        = "active"
                if hasattr(auth, "exam_type"):
                    auth.exam_type = exam_type
                if hasattr(auth, "updated_at"):
                    auth.updated_at = now
                if hasattr(auth, "created_at") and auth.created_at is None:
                    auth.created_at = now
            else:
                auth = StudentAuth(
                    school_id     = school_id,
                    student_id    = sid,
                    password_hash = hashed,
                    term_id       = term_id,
                    status        = "active",
                )
                if hasattr(auth, "exam_type"):
                    auth.exam_type  = exam_type
                if hasattr(auth, "created_at"):
                    auth.created_at = now
                db.session.add(auth)

            pushed.append({
                "student_id":   sid,
                "name":         f"{student.first_name} {student.last_name}".strip(),
                "student_code": student.student_code or "",
                "password":     plain_password,
                "exam_type":    exam_type,
            })

        db.session.commit()

        return jsonify({
            "success": True,
            "message": f"{len(pushed)} report card(s) pushed, {len(failed)} skipped.",
            "pushed":  pushed,
            "failed":  failed,
        }), 200

    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("push_report_cards DB error | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500
    except Exception:
        db.session.rollback()
        logger.exception("push_report_cards failed | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Failed to push report cards. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  CREDENTIALS LIST  —  GET /api/send-report-cards/credentials
# ═══════════════════════════════════════════════════════════════

@send_reports_api.route("/send-report-cards/credentials", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_credentials():
    claims = get_jwt()
    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)
    _, err = _school_or_404(school_id)
    if err:
        return err

    stream_id = request.args.get("stream_id", type=int)
    if not stream_id:
        return jsonify({"message": "stream_id is required"}), 400

    try:
        ss_rows     = StudentStream.query.filter_by(school_id=school_id, stream_id=stream_id).all()
        student_ids = [ss.student_id for ss in ss_rows]

        if not student_ids:
            return jsonify({"success": True, "credentials": []}), 200

        students = Student.query.filter(
            Student.school_id == school_id,
            Student.id.in_(student_ids),
        ).all()
        student_map = {s.id: s for s in students}

        auth_map = {
            sa.student_id: sa
            for sa in StudentAuth.query.filter(
                StudentAuth.school_id  == school_id,
                StudentAuth.student_id.in_(student_ids),
            ).all()
        }

        results = []
        for sid in student_ids:
            student = student_map.get(sid)
            if not student:
                continue
            auth = auth_map.get(sid)
            results.append({
                "student_id":   sid,
                "name":         f"{student.first_name} {student.last_name}".strip(),
                "student_code": student.student_code or "",
                "pushed":       auth is not None,
                "status":       auth.status if auth else "not_pushed",
                "term_id":      auth.term_id if auth else None,
            })

        return jsonify({"success": True, "credentials": results}), 200

    except Exception:
        logger.exception("get_credentials failed | school_id=%s stream_id=%s", school_id, stream_id)
        return jsonify({"success": False, "message": "Failed to load credentials."}), 500


# ═══════════════════════════════════════════════════════════════
#  REVOKE  —  DELETE /api/send-report-cards/revoke/<student_id>
# ═══════════════════════════════════════════════════════════════

@send_reports_api.route("/send-report-cards/revoke/<int:student_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def revoke_student_access(student_id: int):
    claims = get_jwt()
    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    try:
        auth = StudentAuth.query.filter_by(
            school_id=school_id,
            student_id=student_id,
        ).first()

        if not auth:
            return jsonify({"message": "No active credential found for this student"}), 404

        db.session.delete(auth)
        db.session.commit()

        return jsonify({"success": True, "message": "Student access revoked"}), 200

    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("revoke_student_access DB error | student_id=%s", student_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500
    except Exception:
        db.session.rollback()
        logger.exception("revoke_student_access failed | student_id=%s", student_id)
        return jsonify({"success": False, "message": "Failed to revoke access. Please try again."}), 500