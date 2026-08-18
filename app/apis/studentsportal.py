"""
app/apis/studentportal.py
"""

import logging

import requests as http_requests
from flask import (
    Blueprint, render_template, jsonify,
    redirect, url_for, current_app,
    Response, stream_with_context,
)
from flask_jwt_extended import jwt_required, get_jwt, get_jwt_identity

from app.extensions import db, limiter
from app.core.rate_limit import READ_LIMIT
from app.models.user import StudentAuth
from app.models.people import Student
from app.models.reportcards import ReportCard, SchoolDetail
from app.models.academic_structure import Term, AcademicYear
from app.models.core import School
from app.utils.bunny import public_file_url

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


def _get_active_report(student_id, school_id):
    """
    Shared lookup used by the portal page and both file routes.
    Returns (report, error_response). error_response is a (jsonify, status)
    tuple on failure, or None on success.
    """
    auth = StudentAuth.query.filter_by(
        student_id=student_id,
        school_id=school_id,
        status="active",
    ).first()

    if not auth:
        return None, (jsonify({"message": "No active report card access"}), 403)

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
        return None, (jsonify({"message": "Report card not found"}), 404)

    return report, None


def _resolve_report_cdn_url(report) -> str:
    """
    Converts the stored RELATIVE Bunny path (report.firebase_url) into a
    fresh, cache-busted, fully-qualified CDN URL for the server's own
    outbound fetch. Raises FileNotFoundError if no file is on record —
    callers should treat that as a 404.
    """
    stored_path = report.firebase_url
    if not stored_path:
        raise FileNotFoundError("No file available for this report")

    from datetime import datetime
    cdn_url = public_file_url(stored_path)
    return f"{cdn_url}?v={int(datetime.utcnow().timestamp())}"


def _report_filename(student_id: int, report) -> str:
    student   = Student.query.get(student_id)
    name_slug = (
        f"{student.first_name}_{student.last_name}".replace(" ", "_")
        if student else f"student_{student_id}"
    )
    exam_type = (report.exam_type or "report").upper()
    term_obj  = Term.query.get(report.term_id) if report.term_id else None
    term_slug = (term_obj.name or "term").replace(" ", "_") if term_obj else "term"
    return f"{name_slug}_{term_slug}_{exam_type}.pdf"


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

        # ── Active StudentAuth + ReportCard ─────────────────────
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

        report, _ = _get_active_report(student_id, school_id)

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
            "student_portal.view_report_file",
            _=cache_key,
        )
        download_url = url_for(
            "student_portal.download_report_file",
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
            download_url=download_url,
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
#  VIEW REPORT FILE (INLINE)  —  GET /student/report-file
#  Proxies the PDF from BunnyCDN through this server rather than
#  redirecting the student's browser straight to the CDN URL — this
#  keeps the file behind student JWT auth and avoids a stale-edge-
#  cache PoP serving an old copy directly to the browser.
# ═══════════════════════════════════════════════════════════════

@student_portal.route("/student/report-file", methods=["GET"])
@jwt_required()
@limiter.limit("10 per minute")
def view_report_file():
    student_id, school_id, err = _student_only()
    if err:
        return err

    try:
        report, err = _get_active_report(student_id, school_id)
        if err:
            return err

        cdn_url  = _resolve_report_cdn_url(report)
        upstream = http_requests.get(cdn_url, stream=True, timeout=30)
        upstream.raise_for_status()

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=8192)),
            status=200,
            content_type="application/pdf",
            headers={
                "Content-Disposition": "inline",
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    except FileNotFoundError:
        return jsonify({"message": "No file available for this report"}), 404
    except Exception:
        logger.exception("view_report_file failed | student_id=%s", student_id)
        return jsonify({"message": "Could not retrieve the report file. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  DOWNLOAD REPORT FILE (ATTACHMENT)  —  GET /student/report-file/download
# ═══════════════════════════════════════════════════════════════

@student_portal.route("/student/report-file/download", methods=["GET"])
@jwt_required()
@limiter.limit("10 per minute")
def download_report_file():
    student_id, school_id, err = _student_only()
    if err:
        return err

    try:
        report, err = _get_active_report(student_id, school_id)
        if err:
            return err

        cdn_url  = _resolve_report_cdn_url(report)
        upstream = http_requests.get(cdn_url, stream=True, timeout=30)
        upstream.raise_for_status()

        filename = _report_filename(student_id, report)

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=8192)),
            status=200,
            content_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    except FileNotFoundError:
        return jsonify({"message": "No file available for this report"}), 404
    except Exception:
        logger.exception("download_report_file failed | student_id=%s", student_id)
        return jsonify({"message": "Could not retrieve the report file. Please try again."}), 500