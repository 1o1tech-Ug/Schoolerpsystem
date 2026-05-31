"""
app/apis/studentportal.py
"""

import os
import logging

from flask import (
    Blueprint, render_template, jsonify,
    redirect, url_for, request, send_file,
    current_app, make_response,
)
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity

from app.extensions import db, limiter
from app.core.rate_limit import READ_LIMIT
from app.models.user import StudentAuth
from app.models.people import Student
from app.models.reportcards import ReportCard, SchoolDetail
from app.models.academic_structure import Term, AcademicYear
from app.models.core import School

logger = logging.getLogger(__name__)

student_portal = Blueprint("student_portal", __name__)


# ── Guard: only students may access portal routes ────────────────────────────

def _student_only():
    """
    Returns (student_id, school_id, None) on success.
    Returns (None, None, error_response) if the token does not belong to a student.
    """
    claims    = get_jwt()
    role      = claims.get("role")
    school_id = claims.get("school_id")
    identity  = get_jwt_identity()

    if role != "student":
        return None, None, (
            jsonify({"message": "Student access only"}), 403
        )

    try:
        student_id = int(identity)
    except (ValueError, TypeError):
        return None, None, (jsonify({"message": "Invalid token identity"}), 422)

    return student_id, school_id, None


# ═══════════════════════════════════════════════════════════════
#  STUDENT LOGIN PAGE  —  GET /student/login
# ═══════════════════════════════════════════════════════════════

@student_portal.route("/student/login", methods=["GET"])
@limiter.limit(READ_LIMIT)
def student_login_page():
    return render_template("student/login.html")


# ═══════════════════════════════════════════════════════════════
#  PORTAL HOME  —  GET /student/portal
# ═══════════════════════════════════════════════════════════════

@student_portal.route("/student/portal", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def student_portal_page():
    student_id, school_id, err = _student_only()
    if err:
        return redirect(url_for("student_portal.student_login_page"))

    try:
        # ── Student record ───────────────────────────────────────
        student = Student.query.filter_by(
            id=student_id,
            school_id=school_id,
        ).first()

        if not student:
            return render_template(
                "student/portal.html",
                error="Student record not found.",
                student=None, report=None, school=None,
                school_detail=None,
            )

        # ── School + detail ──────────────────────────────────────
        school        = School.query.get(school_id)
        school_detail = SchoolDetail.query.filter_by(school_id=school_id).first()

        # ── Active StudentAuth ───────────────────────────────────
        auth = StudentAuth.query.filter_by(
            student_id=student_id,
            school_id=school_id,
            status="active",
        ).first()

        if not auth:
            return render_template(
                "student/portal.html",
                error="No report card has been shared with you yet. Please check back later.",
                student=student, report=None, school=school,
                school_detail=school_detail,
            )

        # ── Most recently pushed ReportCard ─────────────────────
        report = (
            ReportCard.query
            .filter_by(
                school_id=school_id,
                student_id=student_id,
                term_id=auth.term_id,
                status="generated",
            )
            .order_by(ReportCard.generated_at.desc())
            .first()
        )

        # Fallback: any generated report for this student
        if not report:
            report = (
                ReportCard.query
                .filter_by(
                    school_id=school_id,
                    student_id=student_id,
                    status="generated",
                )
                .order_by(ReportCard.generated_at.desc())
                .first()
            )

        if not report:
            return render_template(
                "student/portal.html",
                error="Your report card has not been generated yet.",
                student=student, report=None, school=school,
                school_detail=school_detail,
            )

        # ── Term / academic year labels ──────────────────────────
        term          = Term.query.get(report.term_id) if report.term_id else None
        academic_year = AcademicYear.query.get(term.academic_year_id) \
                        if (term and term.academic_year_id) else None

        # ── Viewer URL with cache-busting param ──────────────────
        # Use report.id + generated_at timestamp so the browser re-fetches
        # whenever a new report is generated, but not on every page load.
        cache_key = report.id
        if report.generated_at:
            cache_key = f"{report.id}-{int(report.generated_at.timestamp())}"

        viewer_url = url_for(
            "student_portal.serve_report_file",
            _=cache_key,
        )

        return render_template(
            "modules/students/portal.html",
            error=None,
            student=student,
            report=report,
            term=term,
            academic_year=academic_year,
            school=school,
            school_detail=school_detail,
            viewer_url=viewer_url,
        )

    except Exception:
        logger.exception("student_portal_page failed | student_id=%s", student_id)
        return render_template(
            "modules/students/portal.html",
            error="An unexpected error occurred. Please try again.",
            student=None, report=None, school=None,
            school_detail=None,
        )


# ═══════════════════════════════════════════════════════════════
#  SERVE REPORT FILE  —  GET /student/report-file
# ═══════════════════════════════════════════════════════════════

@student_portal.route("/student/report-file", methods=["GET"])
@jwt_required()
@limiter.limit("10 per minute")
def serve_report_file():
    student_id, school_id, err = _student_only()
    if err:
        return err

    try:
        auth = StudentAuth.query.filter_by(
            student_id=student_id,
            school_id=school_id,
            status="active",
        ).first()

        if not auth:
            return jsonify({"message": "No active report card access"}), 403

        report = (
            ReportCard.query
            .filter_by(
                school_id=school_id,
                student_id=student_id,
                term_id=auth.term_id,
                status="generated",
            )
            .order_by(ReportCard.generated_at.desc())
            .first()
        )

        if not report:
            report = (
                ReportCard.query
                .filter_by(
                    school_id=school_id,
                    student_id=student_id,
                    status="generated",
                )
                .order_by(ReportCard.generated_at.desc())
                .first()
            )

        if not report:
            return jsonify({"message": "Report card not found"}), 404

        # ── Resolve file path ────────────────────────────────────
        file_path = report.local_path or report.firebase_path

        if not file_path:
            rel = (report.firebase_url or "").lstrip("/")
            if rel.startswith("static/"):
                rel = rel[len("static/"):]
            static_folder = current_app.static_folder
            file_path = os.path.join(static_folder, rel) if rel else None

        if not file_path or not os.path.exists(file_path):
            logger.error(
                "serve_report_file: file missing for report=%s path=%s",
                report.id, file_path,
            )
            return jsonify({"message": "Report file not found on server"}), 404

        # ── Path traversal guard ─────────────────────────────────
        static_folder = current_app.static_folder
        report_dir    = os.path.realpath(os.path.join(static_folder, "report_cards"))
        safe_path     = os.path.realpath(file_path)

        if not safe_path.startswith(report_dir):
            logger.warning(
                "serve_report_file: path traversal attempt student=%s path=%s",
                student_id, file_path,
            )
            return jsonify({"message": "Forbidden"}), 403

        # ── MIME type & filename ─────────────────────────────────
        ext      = os.path.splitext(safe_path)[1].lower()
        mimetype = "application/pdf" if ext == ".pdf" else "text/html"

        student   = Student.query.get(student_id)
        name_slug = (
            f"{student.first_name}_{student.last_name}".replace(" ", "_")
            if student else f"student_{student_id}"
        )
        exam_type = (report.exam_type or "report").upper()
        term_obj  = Term.query.get(report.term_id) if report.term_id else None
        term_slug = (term_obj.name or "term").replace(" ", "_") if term_obj else "term"
        filename  = f"{name_slug}_{term_slug}_{exam_type}{ext}"

        # ── Build response with no-cache headers ─────────────────
        # This prevents the browser from serving a stale cached PDF
        # in the iframe when a different report card is now active.
        response = make_response(
            send_file(
                safe_path,
                mimetype=mimetype,
                as_attachment=False,
                download_name=filename,
            )
        )
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"]        = "no-cache"
        response.headers["Expires"]       = "0"
        return response

    except Exception:
        logger.exception("serve_report_file failed | student_id=%s", student_id)
        return jsonify({"message": "Could not serve report file"}), 500