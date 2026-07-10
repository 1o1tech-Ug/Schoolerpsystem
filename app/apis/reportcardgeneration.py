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
        return jsonify({
            "success": False,
            "message": (
                "A report card is already being generated for your school. "
                "Please wait for it to finish before starting another."
            ),
        }), 409

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

                service = ReportCardService(_school, _detail)
                result  = service.generate(
                    student=_student,
                    stream=_stream,
                    term=_term,
                    academic_year=_ay,
                    exam_type=exam_type,
                    static_folder=static_folder,
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

    return jsonify({
        "success": True,
        "job_id":  job_id,
        "message": "Generation started",
    }), 202


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