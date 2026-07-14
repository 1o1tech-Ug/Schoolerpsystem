"""
app/apis/reportcardgeneration.py
=================================
Report Card Generation API.

CHANGES vs original:
  - Rate limits applied per endpoint sensitivity.
    Single generation: REPORT_GEN_LIMIT (10/min, 60/hr)
    Bulk generation:   REPORT_ALL_LIMIT (3/min, 20/hr)
    All reads:         READ_LIMIT
  - All except blocks log internally and return safe client messages.
    No str(e) / str(exc) ever reaches the client.
  - [NEW] Threading job store for non-blocking PDF generation.
    generate_report_card  → starts a background thread, returns job_id
    GET /api/report-cards/job/<job_id> → poll for status / result
  - [FIX] App context captured before thread starts to avoid
    RuntimeError: Working outside of application context.
  - [REMOVED] generate_all_report_cards endpoint.
  - [FIX] Report PDFs are now uploaded to BunnyCDN with
    Cache-Control: no-cache, no-store, must-revalidate so the CDN edge
    never serves a stale copy after a report is regenerated at the same
    remote path. Combined with the existing _bust() query-string
    versioning, this fixes the "browser/CDN shows old report" issue.
  - [FIX][STORAGE] Report card PDFs are now stored STRICTLY on BunnyCDN.
    There is no local-disk fallback or persistence of any kind:
      * _upload_report_pdf() no longer swallows upload failures and
        falls back to a local URL — it raises, and the generation job
        is marked "error". A report card is never recorded as
        "generated" unless it actually made it to Bunny.
      * The local temp file produced by the PDF renderer is deleted
        immediately after the Bunny upload attempt (success or
        failure) inside the generation thread and on delete.
      * ReportCard.local_path is no longer populated/used. Only
        firebase_url (CDN URL) and firebase_path (CDN remote path)
        are stored.
      * _resolve_report_source(), view_report_card() and
        download_report_card() only ever read from Bunny — the local
        source branch has been removed.
      * serve_report_file() (local static file serving) has been
        removed since report cards are never persisted on local disk.
  - [NEW][CONCURRENCY] Per-school generation lock. Report card
    generation is CPU/IO heavy (HTML→PDF render + CDN upload) and the
    server is resource-constrained, so only ONE report card may be
    generated at a time PER SCHOOL — regardless of which staff member,
    student, stream, or term is involved. A second attempt from the
    same school while one is in flight gets HTTP 409 immediately
    instead of piling onto the server. The lock is released in the
    generation thread's `finally` block, and includes a stale-lock
    timeout in case a thread dies without reaching `finally` (process
    kill, OOM, etc.), so a school is never permanently locked out.
  - [FIX][CDN-CACHE] Report card PDFs are now uploaded to a UNIQUE
    remote path on every single generation (see _upload_report_pdf).
    Previously every regeneration for the same student/term/exam_type
    reused the exact same remote path, so a CDN edge PoP that had
    already cached the OLD file at that path would keep serving those
    stale bytes for its own TTL — regardless of the Cache-Control
    header set on the NEW upload, and regardless of the old object
    being deleted from storage (a storage delete is not an edge-cache
    purge). Versioning the path removes the dependency on purge APIs
    or query-string cache-key behavior entirely: there is nothing
    stale for the edge to have cached at a path it has never seen.
  - [NEW][SIGNATURES] Schools can upload a headteacher signature (once,
    school-wide) and a class teacher signature per stream. Both are
    stored on BunnyCDN exactly like the school logo — uploaded once,
    reused on every report card rendered for that school/stream until
    replaced. See upload_headteacher_signature(), list_class_signatures(),
    upload_class_teacher_signature(), delete_class_teacher_signature().
  - [NEW][OVERRIDES] Staff can review and edit a report card BEFORE the
    PDF is generated: attendance counts, class-teacher/headteacher
    comments, and initials. Marks/grades/positions are intentionally
    NOT editable this way — those stay strictly computed so staff can't
    silently hand-edit academic data. Overrides are stored per
    (school, student, term, exam_type) in ReportCardOverride and are
    durable — editing and regenerating later reuses the same row.
    See get_report_card_preview() and save_report_card_override().
    generate_report_card()'s background thread looks up any existing
    override and applies it on top of the computed values before
    rendering.
  - [NEW][AUTO-COMMENT] _auto_comment() now varies its wording by
    exam_type (BOT / MID / EOT get separate, contextually sensible
    templates instead of one-size-fits-all "next term" phrasing) and
    derives pronouns from the student's gender via _pronoun(), falling
    back to gender-neutral "they/their/them" when gender is missing or
    unrecognized. See auto_generate_report_card() for the call site.
"""

import requests as http_requests
from flask import Response, stream_with_context
import os
import uuid
import threading
import logging
from datetime import datetime
from flask import (
    Blueprint, request, jsonify,
    render_template, current_app, send_from_directory,
)
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from app.extensions import db, limiter
from app.models.user import User
from app.models.core import School, UserModule
from app.models.reportcards import SchoolDetail, ReportCard, PrimaryReportSummary
from app.models.report_card_extras import (
    HeadteacherSignature, ClassTeacherSignature, ReportCardOverride,
    ReportCommentBank,
)
from app.models.people import Student
from app.models.academic_structure import (
    AcademicYear, Term, Stream, Class,
    StudentStream, Subject, Papers,
    TeachAssignment, Assessment, AssessmentType,
    StudentMark, GradeScale,
)
from app.services.report_card_service import (
    ReportCardService,
    classify_class,
    fetch_grade_scales,
    fetch_student_marks,
    build_subject_rows,
    build_secondary_subject_rows,
    compute_primary_aggregates,
    compute_olevel_aggregates,
    compute_alevel_points,
    calculate_stream_positions,
    compute_attendance,
    html_to_pdf_bytes,
    ensure_report_dir,
)
from app.utils.bunny import bunny_upload, bunny_delete, bunny_remote_path_from_url
from app.core.rate_limit import READ_LIMIT, WRITE_LIMIT, REPORT_GEN_LIMIT, REPORT_ALL_LIMIT

logger = logging.getLogger(__name__)

report_cards_api = Blueprint("report_cards_api", __name__)

ALLOWED_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_LOGO_SIZE_BYTES     = 5 * 1024 * 1024
VALID_EXAM_TYPES        = {"BOT", "MID", "EOT"}

# Signatures reuse the logo's constraints — same kind of small image
# upload, same reasoning (rarely changes → long cache lifetime is fine).
ALLOWED_SIGNATURE_EXTENSIONS = ALLOWED_LOGO_EXTENSIONS
MAX_SIGNATURE_SIZE_BYTES     = MAX_LOGO_SIZE_BYTES
_SIGNATURE_CACHE_CONTROL     = "public, max-age=2592000"  # 30 days — same as logo

# Cache-Control sent to BunnyCDN when uploading report card PDFs.
# Report files are regenerated and re-uploaded at a NEW versioned
# remote path each time (see _upload_report_pdf below), but we still
# set this so any given file is never cached indefinitely by the edge
# or the browser.
_REPORT_CACHE_CONTROL = "no-cache, no-store, must-revalidate"

_SECTION_LABELS = {
    "nursery": "Nursery",
    "primary": "Primary",
    "olevel":  "O-Level",
    "alevel":  "A-Level",
}

# ═══════════════════════════════════════════════════════════════
#  THREADING JOB STORE
#  Simple in-memory dict — no Celery required.
#  Each job has: status, result, error, progress, total
#  Jobs are cleaned up after 10 minutes via a daemon thread.
# ═══════════════════════════════════════════════════════════════

_jobs: dict = {}
_jobs_lock  = threading.Lock()

def _new_job() -> str:
    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "status":   "pending",   # pending | running | done | error
            "result":   None,
            "error":    None,
            "progress": 0,
            "total":    0,
            "created":  datetime.utcnow(),
        }
    return job_id


def _update_job(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


def _start_cleanup_thread():
    """Remove jobs older than 10 minutes — runs once every 5 minutes."""
    def _cleanup():
        import time
        while True:
            time.sleep(300)
            cutoff = datetime.utcnow()
            with _jobs_lock:
                stale = [
                    jid for jid, j in _jobs.items()
                    if (cutoff - j["created"]).total_seconds() > 600
                ]
                for jid in stale:
                    del _jobs[jid]
    t = threading.Thread(target=_cleanup, daemon=True, name="job-cleanup")
    t.start()

_start_cleanup_thread()


# ═══════════════════════════════════════════════════════════════
#  PER-SCHOOL GENERATION LOCK
#  Only one report-card generation job may run at a time PER SCHOOL,
#  regardless of which staff member, student, stream, or term is
#  involved. This is a resource-protection measure (weak server, PDF
#  rendering is CPU/IO heavy) — not a data-integrity lock.
#  Includes a stale-lock timeout in case a thread dies without
#  reaching its `finally` block (process kill, OOM, etc.), so a
#  school is never permanently locked out by a crashed worker.
# ═══════════════════════════════════════════════════════════════

_school_generation_lock      = threading.Lock()
_active_school_jobs: dict    = {}   # school_id -> {"job_id": str, "started": datetime}
_SCHOOL_LOCK_TIMEOUT_SECONDS = 180  # safety valve — treat as stale after 3 min


def _try_acquire_school_lock(school_id, job_id: str) -> bool:
    """Atomically reserve the generation slot for a school.
    Returns False if another job is already running (and not stale)
    for this school."""
    now = datetime.utcnow()
    with _school_generation_lock:
        active = _active_school_jobs.get(school_id)
        if active:
            age = (now - active["started"]).total_seconds()
            if age < _SCHOOL_LOCK_TIMEOUT_SECONDS:
                return False
            logger.warning(
                "Stale school generation lock for school_id=%s overridden after %.0fs",
                school_id, age,
            )
        _active_school_jobs[school_id] = {"job_id": job_id, "started": now}
        return True


def _release_school_lock(school_id, job_id: str):
    """Release the slot — only if it still belongs to this job_id, so a
    late release from a stale/overridden job can't clobber a newer
    legitimate lock."""
    with _school_generation_lock:
        active = _active_school_jobs.get(school_id)
        if active and active["job_id"] == job_id:
            del _active_school_jobs[school_id]


# ═══════════════════════════════════════════════════════════════
#  GUARDS & SHARED HELPERS
# ═══════════════════════════════════════════════════════════════

def _teacher_required(claims):
    if claims.get("role") not in {"staff", "teacher"}:
        logger.warning(
            "_teacher_required: role '%s' not allowed", claims.get("role")
        )
        return None, (jsonify({"message": "Unauthorised — staff access required"}), 403)

    school_id = claims.get("school_id")
    user_id   = claims.get("sub")

    user = User.query.filter_by(
        id=user_id, school_id=school_id, role="staff"
    ).first()

    if not user:
        return None, (jsonify({"message": "User not found"}), 403)

    if not user.staff_id:
        return None, (jsonify({"message": "staff_id missing from user profile"}), 403)

    return user.staff_id, None


def _get_context(claims):
    school_id = claims.get("school_id")
    user_id   = claims.get("sub")
    modules   = [
        m.module_name
        for m in UserModule.query.filter_by(user_id=user_id).all()
    ]
    return school_id, user_id, modules


def _school_or_404(school_id):
    school = School.query.get(school_id)
    if not school:
        return None, (jsonify({"message": "School not found"}), 404)
    return school, None


def _validate_logo_extension(filename: str) -> bool:
    if "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in ALLOWED_LOGO_EXTENSIONS


def _validate_image_extension(filename: str, allowed: set) -> bool:
    if "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in allowed


def _bust(url: str, generated_at: datetime) -> str:
    if not url:
        return url
    ts = int(generated_at.timestamp()) if generated_at else int(datetime.utcnow().timestamp())
    return f"{url}?v={ts}"


def _delete_cdn_file(url):
    if url:
        try:
            bunny_delete(bunny_remote_path_from_url(url))
        except Exception:
            logger.warning("CDN delete failed for URL: %s", url)


def _delete_local_file(path):
    """Best-effort removal of a local temp file. Local disk is never a
    storage location for report cards — this only ever cleans up the
    ephemeral file produced by the PDF renderer before/after upload."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as rm_err:
            logger.warning("_delete_local_file: could not remove %s — %s", path, rm_err)


def _stream_report_type(stream) -> str:
    class_name = stream.class_.name if (stream and stream.class_) else ""
    return classify_class(class_name)


def _upload_report_pdf(local_path: str, academic_year, term, stream, unique_token: str) -> tuple[str, str]:
    """
    Uploads the generated PDF to BunnyCDN. This is the ONLY persistence
    layer for report card files — there is intentionally no local-disk
    fallback. If the upload fails, this raises RuntimeError and the
    caller MUST treat the whole generation as failed (never record a
    report card whose file only exists locally).

    [FIX][CDN-CACHE] `unique_token` is folded into the remote filename
    so every generation lands on a brand-new CDN path instead of
    reusing the same path across regenerations. Relying solely on
    Cache-Control headers / query-string busting was not sufficient:
    a CDN edge PoP that already cached the OLD file at a given path
    keeps serving those bytes for its own TTL regardless of what
    Cache-Control the NEW upload sets, and regardless of whether the
    old object was deleted from storage — that's a storage-level
    delete, not an edge-cache purge. Giving every generation a unique
    path sidesteps the problem entirely: there is nothing stale for
    the edge to have cached at a URL it has never seen before.

    Returns (cdn_url, remote_path).
    """
    with open(local_path, "rb") as fh:
        pdf_bytes = fh.read()

    filename    = os.path.basename(local_path)
    name_part, ext_part = os.path.splitext(filename)
    ay_slug     = (academic_year.name if academic_year else "unknown").replace(" ", "_")
    term_slug   = (term.name          if term          else "unknown").replace(" ", "_")
    stream_slug = (stream.name        if stream        else "unknown").replace(" ", "_")

    # unique_token makes this path different from any previous
    # generation for the same student/term/exam_type, so a CDN edge
    # that cached an older version has nothing stale to serve.
    versioned_filename = f"{name_part}_{unique_token}{ext_part}"

    remote_path = (
        f"uploads/report_cards/{ay_slug}/{term_slug}/{stream_slug}/{versioned_filename}"
    )

    try:
        # Explicit no-cache Cache-Control on upload — belt-and-suspenders
        # on top of the path versioning above, so this specific file is
        # also never held onto indefinitely by an intermediary.
        cdn_url = bunny_upload(
            data=pdf_bytes,
            remote_path=remote_path,
            cache_control=_REPORT_CACHE_CONTROL,
        )
    except Exception as exc:
        logger.warning("_upload_report_pdf: upload failed for %s", local_path)
        raise RuntimeError("Failed to upload report card to storage") from exc

    if not cdn_url:
        logger.warning("_upload_report_pdf: bunny_upload returned no URL for %s", local_path)
        raise RuntimeError("Failed to upload report card to storage")

    logger.info("_upload_report_pdf: uploaded %s → %s", versioned_filename, cdn_url)
    return cdn_url, remote_path


def _get_academic_year_for_term(term: Term):
    if term and term.academic_year_id:
        return AcademicYear.query.get(term.academic_year_id)
    return None


# ═══════════════════════════════════════════════════════════════
#  REPORT CARD GENERATION PAGE  —  GET /report-cards
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report-cards", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def report_cards_page():
    try:
        claims   = get_jwt()
        staff_id, err = _teacher_required(claims)
        if err:
            return err

        school_id, user_id, modules = _get_context(claims)
        school, err = _school_or_404(school_id)
        if err:
            return err

        school_detail = SchoolDetail.query.filter_by(school_id=school_id).first()

        classes   = Class.query.filter_by(school_id=school_id).all()
        class_ids = [c.id for c in classes]

        streams = (
            Stream.query.filter(Stream.class_id.in_(class_ids)).all()
            if class_ids else []
        )

        terms = Term.query.filter_by(school_id=school_id).order_by(Term.name).all()

        ay_ids = list({t.academic_year_id for t in terms if t.academic_year_id})
        academic_years = (
            AcademicYear.query.filter(AcademicYear.id.in_(ay_ids)).all()
            if ay_ids else []
        )

        return render_template(
            "modules/academics/report_card_generation.html",
            school=school,
            school_detail=school_detail,
            streams=streams,
            classes=classes,
            terms=terms,
            academic_years=academic_years,
            modules=modules,
        )

    except Exception:
        logger.exception("report_cards_page failed")
        return jsonify({"success": False, "message": "Failed to load page. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  GET STUDENTS FOR GENERATION  —  GET /api/report-cards/students
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report-cards/students", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_students_for_generation():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
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
            .order_by(Student.first_name.asc(), Student.last_name.asc())
            .all()
        )

        existing = ReportCard.query.filter(
            ReportCard.school_id  == school_id,
            ReportCard.term_id    == term_id,
            ReportCard.exam_type  == exam_type,
            ReportCard.student_id.in_(student_ids),
        ).all()
        report_map = {rc.student_id: rc for rc in existing}

        class_name  = stream.class_.name if stream.class_ else ""
        stream_name = stream.name or ""
        report_type = classify_class(class_name)

        results = []
        for student in students:
            report = report_map.get(student.id)
            results.append({
                "id":               student.id,
                "student_code":     student.student_code     or "",
                "admission_number": student.admission_number or "",
                "name":             f"{student.first_name} {student.last_name}".strip(),
                "class_name":       class_name,
                "stream_name":      stream_name,
                "report_type":      report_type,
                "section_label":    _SECTION_LABELS.get(report_type, report_type.title()),
                "report_generated": report is not None,
                "report_id":        report.id if report else None,
                "report_url": (
                    _bust(report.firebase_url, report.generated_at)
                    if report and report.firebase_url else None
                ),
            })

        return jsonify({"success": True, "students": results}), 200

    except Exception:
        logger.exception("get_students_for_generation failed | school_id=%s stream_id=%s", school_id, stream_id)
        return jsonify({"success": False, "message": "Failed to load students."}), 500


# ═══════════════════════════════════════════════════════════════
#  JOB STATUS POLL  —  GET /api/report-cards/job/<job_id>
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report-cards/job/<job_id>", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_job_status(job_id: str):
    job = _get_job(job_id)
    if not job:
        return jsonify({"message": "Job not found"}), 404
    return jsonify({
        "success":  True,
        "job_id":   job_id,
        "status":   job["status"],    # pending | running | done | error
        "progress": job["progress"],  # not used for single generation
        "total":    job["total"],     # not used for single generation
        "result":   job["result"],    # set when status == done
        "error":    job["error"],     # set when status == error
    }), 200


# ═══════════════════════════════════════════════════════════════
#  PREVIEW A REPORT (READ-ONLY DATA)  —  GET /api/report-cards/preview
#
#  [NEW][OVERRIDES] Returns the computed academic data for one student
#  (marks, grades, positions, default attendance) WITHOUT rendering a
#  PDF, merged with any existing ReportCardOverride for the same
#  (student, term, exam_type). The frontend uses this to populate the
#  edit modal before generation: academic fields render read-only,
#  attendance/comments/initials render as editable inputs pre-filled
#  with either the saved override or the computed default.
#
#  NOTE: this calls ReportCardService.compute_preview(), a new method
#  that needs to exist alongside the current .generate() — it should
#  run the same aggregation steps .generate() already runs (fetch
#  marks, build subject rows, compute aggregates/positions, compute
#  attendance) and return them as a plain dict WITHOUT calling
#  html_to_pdf_bytes() or touching Bunny. If your current .generate()
#  doesn't already separate "compute the data" from "render the PDF"
#  internally, that logic will need to be factored out into a shared
#  helper both methods call — see the docstring on compute_preview()
#  for the exact expected return shape.
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report-cards/preview", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_report_card_preview():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    student_id = request.args.get("student_id", type=int)
    term_id    = request.args.get("term_id",    type=int)
    exam_type  = request.args.get("exam_type", "").strip().upper()
    stream_id  = request.args.get("stream_id",  type=int)

    if not student_id:
        return jsonify({"message": "student_id is required"}), 400
    if not term_id:
        return jsonify({"message": "term_id is required"}), 400
    if not exam_type:
        return jsonify({"message": "exam_type is required"}), 400
    if exam_type not in VALID_EXAM_TYPES:
        return jsonify({"message": f"exam_type must be one of {sorted(VALID_EXAM_TYPES)}"}), 400

    student = Student.query.filter_by(id=student_id, school_id=school_id).first()
    if not student:
        return jsonify({"message": "Student not found"}), 404

    term = Term.query.filter_by(id=term_id, school_id=school_id).first()
    if not term:
        return jsonify({"message": "Term not found"}), 404

    if not stream_id:
        ss_row    = StudentStream.query.filter_by(student_id=student_id, school_id=school_id).first()
        stream_id = ss_row.stream_id if ss_row else None
    stream = Stream.query.get(stream_id) if stream_id else None
    if not stream:
        return jsonify({"message": "Stream not found for this student"}), 404

    try:
        detail = SchoolDetail.query.filter_by(school_id=school_id).first()
        ay     = _get_academic_year_for_term(term)

        service = ReportCardService(school, detail)
        # See note above the route: compute_preview() must exist on
        # ReportCardService and return the same academic data .generate()
        # would compute, minus the PDF render / Bunny upload step.
        computed = service.compute_preview(
            student=student,
            stream=stream,
            term=term,
            academic_year=ay,
            exam_type=exam_type,
        )

        override = ReportCardOverride.query.filter_by(
            school_id=school_id, student_id=student_id, term_id=term_id, exam_type=exam_type,
        ).first()
        saved_subject_initials = (override.subject_initials or {}) if override else {}

        # [NEW] Merge saved per-subject initials into each subject row so
        # the edit UI can pre-fill the "INITIAL" column per subject (the
        # Primary report's marking-teacher initials, distinct from the
        # class teacher's own sign-off initials). compute_preview() must
        # include a stable "subject_id" on each row in "subjects" for
        # this lookup to work.
        subjects_with_initials = []
        for row in computed.get("subjects", []):
            row = dict(row)
            sid = row.get("subject_id")
            row["initials"] = saved_subject_initials.get(str(sid), "") if sid is not None else ""
            subjects_with_initials.append(row)

        # Merge: override values win when present, otherwise fall back
        # to whatever compute_preview() computed as the default.
        attendance = computed.get("attendance", {}) or {}
        merged = {
            "student": {
                "id":   student.id,
                "name": f"{student.first_name} {student.last_name}",
            },
            "report_type":   computed.get("report_type"),
            "section_label": _SECTION_LABELS.get(
                computed.get("report_type"), (computed.get("report_type") or "").title()
            ),
            # Read-only academic data (scores/grades) — "initials" per
            # row IS editable though, see subject_initials handling below.
            "subjects":  subjects_with_initials,
            "aggregates": computed.get("aggregates", {}),
            "positions":  computed.get("positions", {}),
            # Editable fields — override wins over computed default.
            "attendance_present": (
                override.attendance_present if override and override.attendance_present is not None
                else attendance.get("present")
            ),
            "attendance_total": (
                override.attendance_total if override and override.attendance_total is not None
                else attendance.get("total")
            ),
            "class_teacher_comment": (
                override.class_teacher_comment if override and override.class_teacher_comment
                else computed.get("default_class_teacher_comment", "")
            ),
            "headteacher_comment": (
                override.headteacher_comment if override and override.headteacher_comment
                else computed.get("default_headteacher_comment", "")
            ),
            "class_teacher_initials": (
                override.class_teacher_initials if override and override.class_teacher_initials
                else computed.get("default_class_teacher_initials", "")
            ),
            "headteacher_initials": (
                override.headteacher_initials if override and override.headteacher_initials
                else computed.get("default_headteacher_initials", "")
            ),
            "has_saved_override": override is not None,
        }

        return jsonify({"success": True, "preview": merged}), 200

    except Exception:
        logger.exception(
            "get_report_card_preview failed | student_id=%s term_id=%s exam_type=%s",
            student_id, term_id, exam_type,
        )
        return jsonify({"success": False, "message": "Failed to load report preview."}), 500


# ═══════════════════════════════════════════════════════════════
#  SAVE REPORT CARD OVERRIDE  —  POST /api/report-cards/overrides
#
#  [NEW][OVERRIDES] Upserts the editable fields (attendance, comments,
#  initials) for one (student, term, exam_type). Does NOT generate a
#  PDF — the frontend calls this first, then calls the existing
#  /report-cards/generate endpoint, whose background thread looks up
#  this row and applies it on top of the computed defaults.
#  Saved independently of generation so staff can revise a comment and
#  regenerate later without retyping everything.
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report-cards/overrides", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def save_report_card_override():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, user_id, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    data       = request.get_json(force=True) or {}
    student_id = data.get("student_id")
    term_id    = data.get("term_id")
    exam_type  = str(data.get("exam_type", "")).strip().upper()

    if not student_id:
        return jsonify({"message": "student_id is required"}), 400
    if not term_id:
        return jsonify({"message": "term_id is required"}), 400
    if exam_type not in VALID_EXAM_TYPES:
        return jsonify({"message": f"exam_type must be one of {sorted(VALID_EXAM_TYPES)}"}), 400

    student = Student.query.filter_by(id=student_id, school_id=school_id).first()
    if not student:
        return jsonify({"message": "Student not found"}), 404
    term = Term.query.filter_by(id=term_id, school_id=school_id).first()
    if not term:
        return jsonify({"message": "Term not found"}), 404

    def _clean_int(val):
        if val in (None, ""):
            return None
        try:
            n = int(val)
            return n if n >= 0 else None
        except (TypeError, ValueError):
            return None

    def _clean_str(val, max_len):
        if val is None:
            return None
        s = str(val).strip()
        return s[:max_len] if s else None

    def _clean_subject_initials(val):
        """
        [NEW] Expects {"<subject_id>": "XY", ...}. Silently drops any
        entry that isn't a small int-like key with a short string value
        rather than rejecting the whole request over one bad entry —
        this field is edited via per-row inputs in the UI, not hand-typed
        JSON, so a partial/malformed payload is more likely to be a
        client bug than malicious input, and failing soft here keeps a
        typo in one row from blocking every other row's initials from
        saving.
        """
        if not isinstance(val, dict):
            return {}
        cleaned = {}
        for k, v in val.items():
            try:
                subject_id = str(int(k))
            except (TypeError, ValueError):
                continue
            if v is None:
                continue
            s = str(v).strip()[:20]
            if s:
                cleaned[subject_id] = s
        return cleaned

    try:
        override = ReportCardOverride.query.filter_by(
            school_id=school_id, student_id=student_id, term_id=term_id, exam_type=exam_type,
        ).first()
        if not override:
            override = ReportCardOverride(
                school_id=school_id, student_id=student_id, term_id=term_id, exam_type=exam_type,
            )
            db.session.add(override)

        override.attendance_present     = _clean_int(data.get("attendance_present"))
        override.attendance_total       = _clean_int(data.get("attendance_total"))
        override.class_teacher_comment  = _clean_str(data.get("class_teacher_comment"), 2000)
        override.headteacher_comment    = _clean_str(data.get("headteacher_comment"), 2000)
        override.class_teacher_initials = _clean_str(data.get("class_teacher_initials"), 20)
        override.headteacher_initials   = _clean_str(data.get("headteacher_initials"), 20)
        # [NEW] Per-subject marking-teacher initials — Primary report's
        # "INITIAL" column, e.g. {"14": "JN", "15": "RK"}.
        override.subject_initials       = _clean_subject_initials(data.get("subject_initials"))
        override.updated_by             = int(user_id) if user_id else None

        db.session.commit()

        return jsonify({"success": True, "override": override.to_dict()}), 200

    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("save_report_card_override DB error | student_id=%s", student_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500
    except Exception:
        db.session.rollback()
        logger.exception("save_report_card_override failed | student_id=%s", student_id)
        return jsonify({"success": False, "message": "Failed to save changes. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  COMMENT BANK  —  reusable canned comments per school
#  [NEW] Powers the "choose an existing comment" dropdown in the report
#  editor so staff aren't forced to type a fresh comment every time.
#    GET    /api/report-comments?comment_type=class_teacher|headteacher
#    POST   /api/report-comments        { comment_type, text }
#    DELETE /api/report-comments/<id>
# ═══════════════════════════════════════════════════════════════

_VALID_COMMENT_TYPES = {"class_teacher", "headteacher"}


@report_cards_api.route("/report-comments", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def list_comment_bank():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    comment_type = (request.args.get("comment_type") or "").strip()
    q = ReportCommentBank.query.filter_by(school_id=school_id)
    if comment_type:
        if comment_type not in _VALID_COMMENT_TYPES:
            return jsonify({"message": f"comment_type must be one of {sorted(_VALID_COMMENT_TYPES)}"}), 400
        q = q.filter_by(comment_type=comment_type)

    try:
        rows = q.order_by(ReportCommentBank.text).all()
        return jsonify({"success": True, "comments": [r.to_dict() for r in rows]}), 200
    except Exception:
        logger.exception("list_comment_bank failed | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Failed to load comments."}), 500


@report_cards_api.route("/report-comments", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def add_comment_bank_entry():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, user_id, _ = _get_context(claims)

    data         = request.get_json(force=True) or {}
    comment_type = (data.get("comment_type") or "").strip()
    text         = (data.get("text") or "").strip()

    if comment_type not in _VALID_COMMENT_TYPES:
        return jsonify({"message": f"comment_type must be one of {sorted(_VALID_COMMENT_TYPES)}"}), 400
    if not text:
        return jsonify({"message": "text is required"}), 400
    if len(text) > 2000:
        return jsonify({"message": "text must not exceed 2000 characters"}), 400

    try:
        entry = ReportCommentBank(
            school_id=school_id, comment_type=comment_type, text=text,
            created_by=int(user_id) if user_id else None,
        )
        db.session.add(entry)
        db.session.commit()
        return jsonify({"success": True, "comment": entry.to_dict()}), 201
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("add_comment_bank_entry DB error | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500


@report_cards_api.route("/report-comments/<int:comment_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_comment_bank_entry(comment_id: int):
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    try:
        entry = ReportCommentBank.query.filter_by(id=comment_id, school_id=school_id).first()
        if not entry:
            return jsonify({"message": "Comment not found"}), 404
        db.session.delete(entry)
        db.session.commit()
        return jsonify({"success": True, "message": "Comment removed"}), 200
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("delete_comment_bank_entry DB error | comment_id=%s", comment_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  AUTO-GENERATE  —  POST /api/report-cards/auto-generate
#
#  [NEW] Single-click flow: computes the same preview data used by the
#  edit modal, derives class-teacher and headteacher comments from a
#  simple performance-based rule set (see _auto_comment() below), saves
#  them as the report's override (so they show up pre-filled if staff
#  later open the Edit modal), and immediately starts generation —
#  same as clicking "Generate" after manually filling comments, just
#  without the manual step.
#
#  Deliberately does NOT touch attendance or subject initials — those
#  have no sensible auto-derivation from marks alone and are left for
#  staff to fill in via the Edit modal if the school cares about them
#  on a given report.
# ═══════════════════════════════════════════════════════════════

def _pronoun(gender) -> dict:
    """Maps a Student.gender value to a pronoun set. Falls back to
    gender-neutral 'they/their/them' if gender is missing or unrecognized,
    so auto-comments never assume a gender we don't actually have."""
    g = (gender or "").strip().lower()
    if g in ("m", "male", "boy"):
        return {"Subj": "He",   "poss": "his",   "obj": "him"}
    if g in ("f", "female", "girl"):
        return {"Subj": "She",  "poss": "her",   "obj": "her"}
    return {"Subj": "They", "poss": "their", "obj": "them"}


def _auto_comment(*, comment_type: str, average, division, aggregate,
                   exam_type: str, gender=None) -> str:
    """
    Rule-based comment generator. `comment_type` is "class_teacher" or
    "headteacher" — headteacher phrasing is deliberately a notch more
    formal/summary in tone than the class teacher's. Falls back to a
    generic encouraging comment if no performance figure is available
    at all (e.g. no marks entered yet).

    [FIX] Two things previously made auto-comments read as wrong:
      1. Every band of comment referenced "next term" / "this term's
         work" regardless of exam_type — nonsensical for a BOT
         (beginning-of-term) report, where there's no "this term's
         work" to summarize yet. Wording is now templated separately
         per exam_type (BOT / MID / EOT).
      2. Comments used a fixed pronoun ("he") regardless of the
         student's actual gender. Pronouns are now derived from
         Student.gender via _pronoun(), with a neutral "they" fallback
         if gender is unknown.

    This is intentionally simple and school-agnostic — schools that want
    different banded phrasing can just add their own comments to the
    Comment Bank and pick them from the dropdown instead of relying on
    auto-generation.
    """
    # Prefer average (0-100) when present; fall back to inferring a
    # rough band from division/aggregate (lower aggregate = better,
    # Ugandan O-Level convention) if that's all we have.
    band = None
    if average is not None:
        if average >= 80:
            band = "excellent"
        elif average >= 65:
            band = "good"
        elif average >= 50:
            band = "fair"
        else:
            band = "needs_improvement"
    elif division in ("I", "1", 1):
        band = "excellent"
    elif division in ("II", "2", 2):
        band = "good"
    elif division in ("III", "3", 3, "IV", "4", 4):
        band = "fair"
    elif division is not None:
        band = "needs_improvement"

    p = _pronoun(gender)

    templates = {
        "BOT": {
            "class_teacher": {
                "excellent":         f"{p['Subj']} has made an excellent start to the term — keep encouraging {p['obj']} to maintain this standard.",
                "good":              f"A good start to the term. {p['Subj']} should keep building on this early momentum.",
                "fair":              f"A fair start to the term. {p['Subj']} would benefit from more consistent revision as the term goes on.",
                "needs_improvement": f"{p['Subj']} needs closer attention early this term — extra support now will help {p['obj']} going forward.",
                None:                f"Encourage {p['obj']} to settle in and work steadily as the term begins.",
            },
            "headteacher": {
                "excellent":         "A strong beginning to the term. Well done.",
                "good":              "A promising start to the term.",
                "fair":              "An okay start — there is room to build on this as the term progresses.",
                "needs_improvement": f"{p['Subj']} will need additional support as the term progresses.",
                None:                "Best wishes for a productive term ahead.",
            },
        },
        "MID": {
            "class_teacher": {
                "excellent":         f"{p['Subj']} is performing excellently so far this term — keep up the great effort.",
                "good":              f"A good performance so far this term. {p['Subj']} should continue working hard.",
                "fair":              f"A fair performance so far this term. More consistent revision would help {p['obj']} improve before the end of term.",
                "needs_improvement": f"{p['poss'].capitalize()} performance so far this term needs attention — extra effort and support are encouraged for the rest of the term.",
                None:                f"Keep encouraging {p['obj']} to stay consistent for the rest of the term.",
            },
            "headteacher": {
                "excellent":         "An excellent showing so far this term. Keep up this standard.",
                "good":              "Commendable progress so far this term.",
                "fair":              "Satisfactory progress so far — there is room to improve before the term ends.",
                "needs_improvement": "More effort is required for the remainder of the term to improve this performance.",
                None:                "Keep up the effort for the rest of the term.",
            },
        },
        "EOT": {
            "class_teacher": {
                "excellent":         "An excellent term's work — keep up the outstanding effort and consistency next term.",
                "good":              f"A good, solid performance this term. {p['Subj']} should continue working hard to improve further next term.",
                "fair":              "A fair effort this term. With more consistent revision, real improvement is possible next term.",
                "needs_improvement": "This term's performance needs attention — extra effort and support are encouraged next term.",
                None:                "Keep working hard and stay consistent with studies next term.",
            },
            "headteacher": {
                "excellent":         "Congratulations on an excellent term. Keep up this standard next term.",
                "good":              "A commendable term overall. Well done.",
                "fair":              "Satisfactory progress this term — there is room to do even better next term.",
                "needs_improvement": "More effort is required next term to improve this performance.",
                None:                "Keep up the effort next term.",
            },
        },
    }

    exam_templates = templates.get(exam_type, templates["EOT"])
    return exam_templates.get(comment_type, exam_templates["class_teacher"]).get(band)


@report_cards_api.route("/report-cards/auto-generate", methods=["POST"])
@jwt_required()
@limiter.limit(REPORT_GEN_LIMIT)
def auto_generate_report_card():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, user_id, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    data       = request.get_json(force=True) or {}
    student_id = data.get("student_id")
    term_id    = data.get("term_id")
    stream_id  = data.get("stream_id")
    exam_type  = str(data.get("exam_type", "")).strip().upper()

    if not student_id:
        return jsonify({"message": "student_id is required"}), 400
    if not term_id:
        return jsonify({"message": "term_id is required"}), 400
    if not exam_type:
        return jsonify({"message": "exam_type is required"}), 400
    if exam_type not in VALID_EXAM_TYPES:
        return jsonify({"message": f"exam_type must be one of {sorted(VALID_EXAM_TYPES)}"}), 400

    student = Student.query.filter_by(id=student_id, school_id=school_id).first()
    if not student:
        return jsonify({"message": "Student not found"}), 404

    term = Term.query.filter_by(id=term_id, school_id=school_id).first()
    if not term:
        return jsonify({"message": "Term not found"}), 404

    if not stream_id:
        ss_row    = StudentStream.query.filter_by(student_id=student_id, school_id=school_id).first()
        stream_id = ss_row.stream_id if ss_row else None
    stream = Stream.query.get(stream_id) if stream_id else None
    if not stream:
        return jsonify({"message": "Stream not found for this student"}), 404

    try:
        detail = SchoolDetail.query.filter_by(school_id=school_id).first()
        ay     = _get_academic_year_for_term(term)

        service  = ReportCardService(school, detail)
        computed = service.compute_preview(
            student=student, stream=stream, term=term,
            academic_year=ay, exam_type=exam_type,
        )
        aggregates = computed.get("aggregates", {}) or {}

        ct_comment = _auto_comment(
            comment_type="class_teacher",
            average=aggregates.get("average"),
            division=aggregates.get("division"),
            aggregate=aggregates.get("aggregate"),
            exam_type=exam_type,
            gender=getattr(student, "gender", None),
        )
        ht_comment = _auto_comment(
            comment_type="headteacher",
            average=aggregates.get("average"),
            division=aggregates.get("division"),
            aggregate=aggregates.get("aggregate"),
            exam_type=exam_type,
            gender=getattr(student, "gender", None),
        )

        override = ReportCardOverride.query.filter_by(
            school_id=school_id, student_id=student_id, term_id=term_id, exam_type=exam_type,
        ).first()
        if not override:
            override = ReportCardOverride(
                school_id=school_id, student_id=student_id, term_id=term_id, exam_type=exam_type,
            )
            db.session.add(override)

        # Auto-generation only fills comments that are still empty — if
        # staff already wrote/edited a comment for this report, a later
        # click of "Auto Generate" (e.g. to regenerate after new marks)
        # won't silently overwrite their wording.
        if not override.class_teacher_comment:
            override.class_teacher_comment = ct_comment
        if not override.headteacher_comment:
            override.headteacher_comment = ht_comment
        override.updated_by = int(user_id) if user_id else None

        db.session.commit()

    except Exception:
        db.session.rollback()
        logger.exception("auto_generate_report_card: comment generation failed | student_id=%s", student_id)
        return jsonify({"success": False, "message": "Failed to auto-generate comments. Please try again."}), 500

    resp, status = _start_generation_job(
        school_id=school_id, user_id=user_id,
        student_id=student_id, term_id=term_id,
        stream_id=stream_id, exam_type=exam_type,
    )
    return jsonify(resp), status


# ═══════════════════════════════════════════════════════════════
#  GENERATE ONE REPORT  —  POST /api/report-cards/generate
#  Returns immediately with a job_id.
#  Poll GET /api/report-cards/job/<job_id> until status == done | error
#
#  [STORAGE] BunnyCDN is the sole storage location. If the upload to
#  Bunny fails for any reason, the job is marked "error" and NOTHING
#  is written to the database — a report card row only ever exists
#  once its file is confirmed to live on Bunny. The local temp file
#  produced by the renderer is always deleted before the thread ends.
#
#  [CONCURRENCY] Only one generation job may be in flight per school
#  at a time. If another staff member at the same school already has
#  a job running, this returns 409 immediately rather than starting a
#  second heavy render/upload on a resource-constrained server.
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report-cards/generate", methods=["POST"])
@jwt_required()
@limiter.limit(REPORT_GEN_LIMIT)
def generate_report_card():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, user_id, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    data       = request.get_json(force=True) or {}
    student_id = data.get("student_id")
    term_id    = data.get("term_id")
    stream_id  = data.get("stream_id")
    exam_type  = str(data.get("exam_type", "")).strip().upper()

    if not student_id:
        return jsonify({"message": "student_id is required"}), 400
    if not term_id:
        return jsonify({"message": "term_id is required"}), 400
    if not exam_type:
        return jsonify({"message": "exam_type is required"}), 400
    if exam_type not in VALID_EXAM_TYPES:
        return jsonify({"message": f"exam_type must be one of {sorted(VALID_EXAM_TYPES)}"}), 400

    student = Student.query.filter_by(id=student_id, school_id=school_id).first()
    if not student:
        return jsonify({"message": "Student not found"}), 404

    term = Term.query.filter_by(id=term_id, school_id=school_id).first()
    if not term:
        return jsonify({"message": "Term not found"}), 404

    if not stream_id:
        ss_row    = StudentStream.query.filter_by(student_id=student_id, school_id=school_id).first()
        stream_id = ss_row.stream_id if ss_row else None

    stream = Stream.query.get(stream_id) if stream_id else None
    if not stream:
        return jsonify({"message": "Stream not found for this student"}), 404

    resp, status = _start_generation_job(
        school_id=school_id, user_id=user_id,
        student_id=student_id, term_id=term_id,
        stream_id=stream_id, exam_type=exam_type,
    )
    return jsonify(resp), status


def _start_generation_job(*, school_id, user_id, student_id, term_id, stream_id, exam_type) -> tuple[dict, int]:
    """
    Shared job-starting logic used by both generate_report_card() (manual
    "Edit → Save & Generate" flow) and auto_generate_report_card() (the
    single-click "Auto Generate" flow). Callers are responsible for all
    validation (student/term/stream existence, exam_type) before calling
    this — it assumes its inputs are already valid.

    Returns (response_body_dict, http_status_code) — caller wraps with
    jsonify().
    """
    # ── FIX: capture the real app object NOW, while still inside the
    #         request context where current_app proxy is valid.
    #         The thread cannot use current_app — it has no request context.
    from flask import current_app as _cur_app
    _flask_app    = _cur_app._get_current_object()
    static_folder = _flask_app.static_folder

    job_id = _new_job()

    # ── CONCURRENCY GUARD: only one generation job per school at a time.
    #    Any staff member at this school with a job already running
    #    blocks any other staff member at the same school from starting
    #    another one until it finishes (or the stale-lock timeout hits).
    if not _try_acquire_school_lock(school_id, job_id):
        _update_job(job_id, status="error", error="cancelled — another generation is already in progress for this school")
        return {
            "success": False,
            "message": (
                "A report card is already being generated for your school. "
                "Please wait for it to finish before starting another."
            ),
        }, 409

    def _do_generate():
        _update_job(job_id, status="running")
        with _flask_app.app_context():
            local_path = None
            try:
                _school  = School.query.get(school_id)
                _detail  = SchoolDetail.query.filter_by(school_id=school_id).first()
                _student = Student.query.filter_by(id=student_id, school_id=school_id).first()
                _term    = Term.query.filter_by(id=term_id, school_id=school_id).first()
                _stream  = Stream.query.get(stream_id)
                _ay      = _get_academic_year_for_term(_term)

                # [NEW][OVERRIDES] Pull any staff-saved edits for this
                # exact (student, term, exam_type) — attendance counts,
                # comments, initials, per-subject initials. None of these
                # fields being unset is fine: service.generate() should
                # fall back to its own computed defaults for anything the
                # override left null, same as get_report_card_preview()
                # does.
                _override = ReportCardOverride.query.filter_by(
                    school_id=school_id, student_id=student_id,
                    term_id=term_id, exam_type=exam_type,
                ).first()

                # [NEW][SIGNATURES] Headteacher signature is school-wide;
                # class teacher signature is per-stream. Both are just
                # CDN URLs to drop into the report's sign-off block —
                # neither is regenerated per report, they're fetched
                # fresh here so a signature update takes effect on the
                # very next generation without any other code changing.
                _headteacher_sig = HeadteacherSignature.query.filter_by(
                    school_id=school_id
                ).first()
                _class_teacher_sig = ClassTeacherSignature.query.filter_by(
                    stream_id=stream_id
                ).first()

                service = ReportCardService(_school, _detail)
                result  = service.generate(
                    student=_student,
                    stream=_stream,
                    term=_term,
                    academic_year=_ay,
                    exam_type=exam_type,
                    static_folder=static_folder,
                    # NOTE: service.generate() needs to accept these two
                    # new kwargs and apply them when building the Jinja
                    # context for the report HTML — override fields
                    # replace the computed default only when not None;
                    # signature URLs render as <img> tags in the sign-off
                    # section (omit gracefully when a signature is unset).
                    overrides=_override.to_dict() if _override else {},
                    signatures={
                        "headteacher_signature_url": _headteacher_sig.signature_url if _headteacher_sig else None,
                        "headteacher_name":          _headteacher_sig.teacher_name  if _headteacher_sig else None,
                        "class_teacher_signature_url": _class_teacher_sig.signature_url if _class_teacher_sig else None,
                        "class_teacher_name":          _class_teacher_sig.teacher_name  if _class_teacher_sig else None,
                    },
                )

                local_path  = result["local_path"]
                report_type = result["report_type"]
                now         = datetime.utcnow()

                existing_report = ReportCard.query.filter_by(
                    school_id=school_id,
                    student_id=student_id,
                    term_id=term_id,
                    exam_type=exam_type,
                ).first()

                # [FIX][CDN-CACHE] Unique per-generation token folded into
                # the CDN remote path — see _upload_report_pdf docstring.
                # Combines a timestamp (readable in Bunny's file browser)
                # with a short uuid suffix (guarantees uniqueness even if
                # two jobs somehow land in the same second).
                unique_token = f"{int(now.timestamp())}_{uuid.uuid4().hex[:8]}"

                # Upload to Bunny FIRST. If this raises, we fall straight
                # into the except block below — no DB row is touched and
                # the previous report card (if any) stays untouched.
                cdn_url, remote_path = _upload_report_pdf(local_path, _ay, _term, _stream, unique_token)

                # Only now that the new file is safely on Bunny — at a
                # path the CDN edge has never served before, so there is
                # nothing stale to worry about — do we remove the
                # previous CDN file. This ordering means a failed
                # re-generation never leaves a report card with no file
                # at all.
                if existing_report and existing_report.firebase_url:
                    _delete_cdn_file(existing_report.firebase_url)

                if existing_report:
                    existing_report.firebase_url  = cdn_url
                    existing_report.firebase_path = remote_path
                    existing_report.local_path    = None
                    existing_report.generated_at  = now
                    existing_report.generated_by  = int(user_id) if user_id else None
                    existing_report.status        = "generated"
                    existing_report.academic_year = _ay.name if _ay else None
                    report = existing_report
                else:
                    report = ReportCard(
                        school_id=school_id,
                        student_id=student_id,
                        term_id=term_id,
                        exam_type=exam_type,
                        academic_year=_ay.name if _ay else None,
                        generated_at=now,
                        generated_by=int(user_id) if user_id else None,
                        firebase_url=cdn_url,
                        firebase_path=remote_path,
                        local_path=None,
                        status="generated",
                    )
                    db.session.add(report)

                db.session.commit()

                _update_job(job_id, status="done", result={
                    "id":            report.id,
                    "student_name":  f"{_student.first_name} {_student.last_name}",
                    "report_type":   report_type,
                    "section_label": _SECTION_LABELS.get(report_type, report_type.title()),
                    "file_url":      _bust(cdn_url, now),
                    "report_id":     report.id,
                })

            except Exception:
                db.session.rollback()
                logger.exception("generate thread failed | student_id=%s", student_id)
                _update_job(job_id, status="error", error="Failed to generate report card. Please try again.")

            finally:
                # Report cards are stored strictly on Bunny — the local
                # temp file produced by the renderer is never kept
                # around, whether the upload above succeeded or failed.
                _delete_local_file(local_path)
                # Always free the school's generation slot, success or
                # failure, so the next request for this school can run.
                _release_school_lock(school_id, job_id)

    thread = threading.Thread(target=_do_generate, daemon=True, name=f"gen-{job_id[:8]}")
    thread.start()

    return {
        "success": True,
        "job_id":  job_id,
        "message": "Generation started",
    }, 202


# ═══════════════════════════════════════════════════════════════
#  GET SAVED REPORT CARDS  —  GET /api/report_cards
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report_cards", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_report_cards():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    academic_year_id = request.args.get("academic_year_id", type=int)
    term_id          = request.args.get("term_id",          type=int)
    stream_id        = request.args.get("stream_id",        type=int)
    exam_type        = request.args.get("exam_type", "").strip().upper()

    if not all([academic_year_id, term_id, stream_id, exam_type]):
        return jsonify({
            "message": "academic_year_id, term_id, stream_id and exam_type are required"
        }), 400
    if exam_type not in VALID_EXAM_TYPES:
        return jsonify({"message": f"exam_type must be one of {sorted(VALID_EXAM_TYPES)}"}), 400

    try:
        ss_rows     = StudentStream.query.filter_by(school_id=school_id, stream_id=stream_id).all()
        student_ids = [ss.student_id for ss in ss_rows]

        if not student_ids:
            return jsonify({"success": True, "report_cards": []}), 200

        students    = Student.query.filter(Student.id.in_(student_ids)).all()
        student_map = {s.id: s for s in students}

        stream      = Stream.query.get(stream_id)
        class_name  = stream.class_.name if stream and stream.class_ else ""
        stream_name = stream.name        if stream else ""
        report_type = classify_class(class_name)

        ay      = AcademicYear.query.get(academic_year_id)
        ay_name = ay.name if ay else ""

        reports = ReportCard.query.filter(
            ReportCard.school_id  == school_id,
            ReportCard.term_id    == term_id,
            ReportCard.exam_type  == exam_type,
            ReportCard.student_id.in_(student_ids),
        ).all()

        results = []
        for rc in reports:
            student = student_map.get(rc.student_id)
            if not student:
                continue
            results.append({
                "id":            rc.id,
                "student_code":  student.student_code or "",
                "student_name":  f"{student.first_name} {student.last_name}",
                "class_name":    class_name,
                "stream_name":   stream_name,
                "exam_type":     rc.exam_type or exam_type,
                "academic_year": rc.academic_year or ay_name,
                "report_type":   report_type,
                "section_label": _SECTION_LABELS.get(report_type, report_type.title()),
                "generated_at":  rc.generated_at.isoformat() if rc.generated_at else None,
                "file_url": (
                    _bust(rc.firebase_url, rc.generated_at) if rc.firebase_url else ""
                ),
            })

        return jsonify({"success": True, "report_cards": results}), 200

    except Exception:
        logger.exception("get_report_cards failed | school_id=%s stream_id=%s", school_id, stream_id)
        return jsonify({"success": False, "message": "Failed to load report cards."}), 500


# ═══════════════════════════════════════════════════════════════
#  DELETE REPORT CARD  —  DELETE /api/report-cards/<id>
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report-cards/<int:report_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_report_card(report_id: int):
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    try:
        report = ReportCard.query.filter_by(id=report_id, school_id=school_id).first()
        if not report:
            return jsonify({"message": "Report card not found"}), 404

        # Bunny is the sole storage location — nothing else to clean up.
        _delete_cdn_file(report.firebase_url)

        db.session.delete(report)
        db.session.commit()

        return jsonify({"success": True, "message": "Report card deleted successfully"}), 200

    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("delete_report_card DB error | report_id=%s", report_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500
    except Exception:
        db.session.rollback()
        logger.exception("delete_report_card failed | report_id=%s", report_id)
        return jsonify({"success": False, "message": "Failed to delete report card. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  SAVE SCHOOL CONFIGURATION  —  POST /api/school/details
#  (Logos are a separate concern from report cards, but they too are
#  stored solely on BunnyCDN — no local disk copy is kept here either.)
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/school/details", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def save_school_details():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    try:
        logo_url = None

        logo_file = request.files.get("school_logo")
        if logo_file and logo_file.filename:
            filename = secure_filename(logo_file.filename)

            if not _validate_logo_extension(filename):
                return jsonify({
                    "success": False,
                    "message": (
                        f"Invalid file type. "
                        f"Allowed: {', '.join(sorted(ALLOWED_LOGO_EXTENSIONS))}"
                    ),
                }), 400

            logo_bytes = logo_file.read()
            if len(logo_bytes) > MAX_LOGO_SIZE_BYTES:
                return jsonify({
                    "success": False,
                    "message": "Logo file must not exceed 5MB",
                }), 400

            ext = filename.rsplit(".", 1)[1].lower()
            remote_path = f"uploads/logos/school_{school_id}_logo.{ext}"

            detail_check = SchoolDetail.query.filter_by(school_id=school_id).first()

            if detail_check and detail_check.school_logo_url:
                _delete_cdn_file(detail_check.school_logo_url)

            # School logos rarely change once uploaded and each new logo
            # gets its own filename via school_id, so a long-lived cache
            # is safe (and desirable) here — unlike report card PDFs.
            try:
                logo_url = bunny_upload(
                    data=logo_bytes,
                    remote_path=remote_path,
                    cache_control="public, max-age=2592000",
                )
            except Exception as exc:
                logger.exception("save_school_details: logo upload failed | school_id=%s", school_id)
                return jsonify({
                    "success": False,
                    "message": "Failed to upload logo. Please try again.",
                }), 502

            if not logo_url:
                logger.warning("save_school_details: logo upload returned no URL | school_id=%s", school_id)
                return jsonify({
                    "success": False,
                    "message": "Failed to upload logo. Please try again.",
                }), 502

            logger.info("save_school_details: logo uploaded to CDN → %s", logo_url)

        po_box_number  = (request.form.get("po_box_number",  "") or "").strip() or None
        district       = (request.form.get("district",       "") or "").strip() or None
        contact_1      = (request.form.get("contact_1",      "") or "").strip()
        contact_2      = (request.form.get("contact_2",      "") or "").strip() or None
        website_domain = (request.form.get("website_domain", "") or "").strip() or None
        email          = (request.form.get("email",          "") or "").strip() or None

        if not contact_1:
            return jsonify({"success": False, "message": "Contact 1 is required"}), 400

        gp_min_mark = sub_math_min_mark = ict_min_mark = None

        for field_name, var_name in [
            ("gp_min_mark", "gp_min_mark"),
            ("ict_min_mark", "ict_min_mark"),
            ("sub_math_min_mark", "sub_math_min_mark"),
        ]:
            raw = (request.form.get(field_name, "") or "").strip()
            if raw:
                try:
                    val = float(raw)
                    if not (0 <= val <= 100):
                        raise ValueError
                    if field_name == "gp_min_mark":
                        gp_min_mark = val
                    elif field_name == "ict_min_mark":
                        ict_min_mark = val
                    else:
                        sub_math_min_mark = val
                except ValueError:
                    label = field_name.replace("_", " ").title()
                    return jsonify({"success": False, "message": f"{label} must be a number between 0 and 100"}), 400

        detail = SchoolDetail.query.filter_by(school_id=school_id).first()
        if not detail:
            detail = SchoolDetail(school_id=school_id, contact_1=contact_1)
            db.session.add(detail)

        detail.contact_1      = contact_1
        detail.po_box_number  = po_box_number
        detail.district       = district
        detail.contact_2      = contact_2
        detail.website_domain = website_domain
        detail.email          = email

        if logo_url:
            detail.school_logo_url = logo_url
        if gp_min_mark is not None:
            detail.gp_min_mark = gp_min_mark
        if ict_min_mark is not None:
            detail.ict_min_mark = ict_min_mark
        if sub_math_min_mark is not None:
            detail.sub_math_min_mark = sub_math_min_mark

        db.session.commit()

        return jsonify({
            "success": True,
            "message": "School configuration saved successfully",
            "logo_url": logo_url or detail.school_logo_url,
        }), 200

    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("save_school_details DB error | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500
    except Exception:
        db.session.rollback()
        logger.exception("save_school_details failed | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Failed to save school configuration. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  HEADTEACHER SIGNATURE  —  POST /api/school/headteacher-signature
#  [NEW][SIGNATURES] One signature per school, uploaded once and reused
#  on every report card. Same storage pattern as the school logo:
#  BunnyCDN only, long cache lifetime (rarely changes), replace-in-place
#  (old file deleted after the new one uploads successfully).
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/school/headteacher-signature", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def upload_headteacher_signature():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, user_id, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    sig_file = request.files.get("signature")
    if not sig_file or not sig_file.filename:
        return jsonify({"success": False, "message": "signature file is required"}), 400

    filename = secure_filename(sig_file.filename)
    if not _validate_image_extension(filename, ALLOWED_SIGNATURE_EXTENSIONS):
        return jsonify({
            "success": False,
            "message": f"Invalid file type. Allowed: {', '.join(sorted(ALLOWED_SIGNATURE_EXTENSIONS))}",
        }), 400

    sig_bytes = sig_file.read()
    if len(sig_bytes) > MAX_SIGNATURE_SIZE_BYTES:
        return jsonify({"success": False, "message": "Signature file must not exceed 5MB"}), 400

    teacher_name = (request.form.get("teacher_name", "") or "").strip() or None
    ext = filename.rsplit(".", 1)[1].lower()
    remote_path = f"uploads/signatures/school_{school_id}_headteacher.{ext}"

    try:
        record = HeadteacherSignature.query.filter_by(school_id=school_id).first()
        old_url = record.signature_url if record else None

        sig_url = bunny_upload(
            data=sig_bytes, remote_path=remote_path, cache_control=_SIGNATURE_CACHE_CONTROL,
        )
    except Exception:
        logger.exception("upload_headteacher_signature: upload failed | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Failed to upload signature. Please try again."}), 502

    if not sig_url:
        logger.warning("upload_headteacher_signature: bunny_upload returned no URL | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Failed to upload signature. Please try again."}), 502

    try:
        if record:
            if old_url and old_url != sig_url:
                _delete_cdn_file(old_url)
            record.signature_url = sig_url
            if teacher_name:
                record.teacher_name = teacher_name
            record.updated_by = int(user_id) if user_id else None
        else:
            record = HeadteacherSignature(
                school_id=school_id, teacher_name=teacher_name,
                signature_url=sig_url, updated_by=int(user_id) if user_id else None,
            )
            db.session.add(record)
        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Headteacher signature saved",
            "signature_url": sig_url,
            "teacher_name": record.teacher_name,
        }), 200

    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("upload_headteacher_signature DB error | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  CLASS TEACHER SIGNATURES  —  one per stream
#  [NEW][SIGNATURES]
#    GET    /api/streams/signatures            — list all streams + status
#    POST   /api/streams/<id>/signature         — upload/replace
#    DELETE /api/streams/<id>/signature         — remove
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/streams/signatures", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def list_class_signatures():
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    try:
        classes   = Class.query.filter_by(school_id=school_id).all()
        class_ids = [c.id for c in classes]
        streams = (
            Stream.query.filter(Stream.class_id.in_(class_ids)).all()
            if class_ids else []
        )

        sigs = ClassTeacherSignature.query.filter_by(school_id=school_id).all()
        sig_map = {s.stream_id: s for s in sigs}

        results = []
        for stream in streams:
            sig = sig_map.get(stream.id)
            results.append({
                "stream_id":     stream.id,
                "class_name":    stream.class_.name if stream.class_ else "",
                "stream_name":   stream.name or "",
                "teacher_name":  sig.teacher_name if sig else None,
                "signature_url": sig.signature_url if sig else None,
                "has_signature": sig is not None and bool(sig.signature_url),
            })

        return jsonify({"success": True, "streams": results}), 200

    except Exception:
        logger.exception("list_class_signatures failed | school_id=%s", school_id)
        return jsonify({"success": False, "message": "Failed to load signatures."}), 500


@report_cards_api.route("/streams/<int:stream_id>/signature", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def upload_class_teacher_signature(stream_id: int):
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, user_id, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    stream = Stream.query.get(stream_id)
    if not stream or not stream.class_ or stream.class_.school_id != school_id:
        return jsonify({"message": "Stream not found"}), 404

    sig_file = request.files.get("signature")
    if not sig_file or not sig_file.filename:
        return jsonify({"success": False, "message": "signature file is required"}), 400

    filename = secure_filename(sig_file.filename)
    if not _validate_image_extension(filename, ALLOWED_SIGNATURE_EXTENSIONS):
        return jsonify({
            "success": False,
            "message": f"Invalid file type. Allowed: {', '.join(sorted(ALLOWED_SIGNATURE_EXTENSIONS))}",
        }), 400

    sig_bytes = sig_file.read()
    if len(sig_bytes) > MAX_SIGNATURE_SIZE_BYTES:
        return jsonify({"success": False, "message": "Signature file must not exceed 5MB"}), 400

    teacher_name = (request.form.get("teacher_name", "") or "").strip() or None
    ext = filename.rsplit(".", 1)[1].lower()
    remote_path = f"uploads/signatures/stream_{stream_id}_teacher.{ext}"

    try:
        record  = ClassTeacherSignature.query.filter_by(stream_id=stream_id).first()
        old_url = record.signature_url if record else None

        sig_url = bunny_upload(
            data=sig_bytes, remote_path=remote_path, cache_control=_SIGNATURE_CACHE_CONTROL,
        )
    except Exception:
        logger.exception("upload_class_teacher_signature: upload failed | stream_id=%s", stream_id)
        return jsonify({"success": False, "message": "Failed to upload signature. Please try again."}), 502

    if not sig_url:
        return jsonify({"success": False, "message": "Failed to upload signature. Please try again."}), 502

    try:
        if record:
            if old_url and old_url != sig_url:
                _delete_cdn_file(old_url)
            record.signature_url = sig_url
            if teacher_name:
                record.teacher_name = teacher_name
            record.updated_by = int(user_id) if user_id else None
        else:
            record = ClassTeacherSignature(
                school_id=school_id, stream_id=stream_id, teacher_name=teacher_name,
                signature_url=sig_url, updated_by=int(user_id) if user_id else None,
            )
            db.session.add(record)
        db.session.commit()

        return jsonify({
            "success": True,
            "message": "Class teacher signature saved",
            "signature_url": sig_url,
            "teacher_name": record.teacher_name,
        }), 200

    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("upload_class_teacher_signature DB error | stream_id=%s", stream_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500


@report_cards_api.route("/streams/<int:stream_id>/signature", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_class_teacher_signature(stream_id: int):
    claims = get_jwt()
    staff_id, err = _teacher_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    try:
        record = ClassTeacherSignature.query.filter_by(stream_id=stream_id, school_id=school_id).first()
        if not record:
            return jsonify({"message": "No signature on file for this stream"}), 404

        _delete_cdn_file(record.signature_url)
        db.session.delete(record)
        db.session.commit()

        return jsonify({"success": True, "message": "Signature removed"}), 200

    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("delete_class_teacher_signature DB error | stream_id=%s", stream_id)
        return jsonify({"success": False, "message": "Database error. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  SHARED: RESOLVE REPORT FILE (used by both /view and /download)
#  [STORAGE] Bunny is the ONLY source of truth. There is no local-disk
#  branch — if firebase_url is missing, the report simply has no file.
# ═══════════════════════════════════════════════════════════════

def _resolve_report_source(report) -> str:
    """
    Returns a fresh, cache-busted CDN URL for the report's PDF.
    Raises FileNotFoundError if no file is on record.
    """
    cdn_url = report.firebase_url
    if not cdn_url:
        raise FileNotFoundError("No file available for this report")

    # Strip any existing querystring and add a fresh cache-busting
    # timestamp on every request. This is a server-side outbound request
    # (not the user's browser hitting the CDN edge directly), so it
    # naturally avoids the stale-edge-cache problem that direct CDN links
    # in the browser can hit — but we still bust the query string in case
    # this server process's own outbound requests get routed through a
    # caching proxy at some point.
    cdn_url = cdn_url.split("?")[0]
    cdn_url = f"{cdn_url}?v={int(datetime.utcnow().timestamp())}"
    return cdn_url


# ═══════════════════════════════════════════════════════════════
#  VIEW REPORT CARD (INLINE)  —  GET /api/report-cards/<id>/view
#  Always resolves through the Flask server rather than letting the
#  browser hit the BunnyCDN URL directly. This exists because direct
#  CDN links can get served from a stale local edge PoP cache even
#  after regeneration + our Cache-Control fix at upload time —
#  routing through the server sidesteps that entirely, the same way
#  download_report_card already does for downloads.
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report-cards/<int:report_id>/view", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def view_report_card(report_id: int):
    claims    = get_jwt()
    school_id = claims.get("school_id")

    report = ReportCard.query.filter_by(id=report_id, school_id=school_id).first()
    if not report:
        return jsonify({"message": "Report card not found"}), 404

    try:
        cdn_url  = _resolve_report_source(report)
        upstream = http_requests.get(cdn_url, stream=True, timeout=30)
        upstream.raise_for_status()

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=8192)),
            status=200,
            content_type="application/pdf",
            headers={
                "Content-Disposition": "inline",
                "Cache-Control": "no-store",
            },
        )

    except FileNotFoundError:
        return jsonify({"message": "No file available for this report"}), 404
    except Exception:
        logger.exception("view_report_card failed | report_id=%s", report_id)
        return jsonify({"message": "Could not retrieve the report file. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  DOWNLOAD REPORT CARD  —  GET /api/report-cards/<id>/download
# ═══════════════════════════════════════════════════════════════

@report_cards_api.route("/report-cards/<int:report_id>/download", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def download_report_card(report_id: int):
    claims    = get_jwt()
    school_id = claims.get("school_id")

    report = ReportCard.query.filter_by(id=report_id, school_id=school_id).first()
    if not report:
        return jsonify({"message": "Report card not found"}), 404

    try:
        cdn_url  = _resolve_report_source(report)
        upstream = http_requests.get(cdn_url, stream=True, timeout=30)
        upstream.raise_for_status()

        student = Student.query.get(report.student_id)
        name    = f"{student.first_name}_{student.last_name}" if student else "report"
        fname   = f"report_{name}_{report.exam_type or ''}.pdf"

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=8192)),
            status=200,
            content_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{fname}"',
                "Cache-Control": "no-store",
            },
        )

    except FileNotFoundError:
        return jsonify({"message": "No file available for this report"}), 404
    except Exception:
        logger.exception("download_report_card failed | report_id=%s", report_id)
        return jsonify({"message": "Could not retrieve the report file. Please try again."}), 500