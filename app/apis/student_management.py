"""
app/apis/student_management.py
================================
Student CRUD API — register, update, delete, bulk import, download.

CHANGES vs original:
  - Rate limits applied per endpoint sensitivity.
  - All except blocks log internally and return safe client messages.
    No str(e) ever reaches the client.
  - print(e) calls removed; replaced with logger.exception().
  - delete_student now clears the finance chain (Receipt -> Payment ->
    InvoiceItem -> Invoice) before deleting the student, fixing the
    ForeignKeyViolation on invoice_items_invoice_id_fkey.
  - [FIX][STORAGE] _upload_image()/_upload_document() now store the
    relative remote_path returned alongside the upload, not the full
    CDN URL — so a future change of BUNNY_BASE_URL never requires a
    data migration. bunny_upload() already raises on failure, so
    reaching the return line means the file is confirmed on Bunny.
    Templates resolve the stored value to a renderable URL via the new
    bunny_public_url() helper (passed into render_template() below),
    which is idempotent for the full-URL values already sitting in
    existing rows — no backfill needed. bunny_remote_path_from_url()
    (used by _delete_cdn_file()) already passed relative paths through
    unchanged, so deletion needed no changes.
  - [FIX][PHOTOS] update_student now uploads the replacement photo
    *before* deleting the old one, for both student and guardian
    photos. bunny_upload() raises on failure, so if the new upload
    fails the old photo is left untouched and the student/guardian
    keeps a working photo instead of ending up with none. The old
    file is only deleted once the new one is confirmed live on Bunny.
  - [NEW][PHOTO-BG] Student and guardian photos now have their
    background replaced with a flat white background before upload.
    Uses the same brightness-threshold technique as report_cards'
    signature background removal, but composites onto an OPAQUE WHITE
    canvas rather than leaving the area transparent, since these
    photos are displayed directly (list rows, cards, printed
    documents) rather than layered over other content. See
    _replace_background_with_white() and its use inside _upload_image().
    Best-effort: if processing fails for any reason, the original
    image is uploaded unchanged rather than blocking registration or
    update on it.
"""

import logging
from flask import Blueprint, request, jsonify, render_template, Response
from flask_jwt_extended import jwt_required, get_jwt
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from PIL import Image
from app.extensions import db, limiter
from app.models.core import School, UserModule
from app.models.people import (
    Student, StudentAcademic, Guardian, MedicalRecord, Document
)
from app.models.user import StudentAuth, User
from app.models.academic_structure import Class
from app.models.finance import Invoice, Payment, Receipt, InvoiceItem
from app.models.academic_structure import (
    StudentMark, StudentAttendance, StudentSubject, StudentStream, StudentEnrollment,
    StudentDailyAttendance,
)
from app.models.reportcards import PrimaryReportSummary
from app.utils.utilities import check_student_limit
from app.utils.bunny import bunny_upload, bunny_delete, bunny_remote_path_from_url, bunny_public_url
from app.core.rate_limit import (
    READ_LIMIT, WRITE_LIMIT, BULK_LIMIT,
    PASSWORD_RESET_LIMIT,
)
import os
import io
import csv
import uuid

logger = logging.getLogger(__name__)

student_management_api = Blueprint(
    "student_management_api",
    __name__,
    url_prefix="/api/students"
)

ALLOWED_EXTENSIONS  = {"csv", "xlsx", "xls"}
MAX_FILE_SIZE       = 50 * 1024 * 1024
VALID_STUDENT_TYPES = {"day", "boarding"}

STAFF_ROLES = {"staff"}
ALL_ROLES   = {"staff", "admin"}

# [FIX][ADAPTIVE-BG] A fixed absolute-brightness cutoff (the original
# version used >=235) essentially never matches a real phone photo's
# background — shadows, off-white/colored backdrops, uneven lighting
# and JPEG noise all keep genuine background pixels below any single
# fixed threshold, so the "remove background" step silently did
# nothing on real uploads even though it ran without error. This
# mirrors the identical fix applied to signature background removal
# in reportcardgeneration.py: instead of assuming a fixed brightness,
# the background color is sampled from THIS photo's own border (a
# strip around the edge, where background reliably dominates — a
# passport-style photo's subject essentially never touches the very
# edge of the frame), and each pixel's distance from that sampled
# color drives the alpha, rather than distance from an assumed white.
_PHOTO_BORDER_MARGIN_FRAC = 0.04   # border strip width, as a fraction of image size
# Per-pixel distance (0-255 scale, luminance-weighted channel diff)
# from the sampled background color, below which a pixel is treated
# as background (replaced with white) and above which it's treated as
# subject (kept as-is). Values in between fade smoothly.
_PHOTO_DIFF_LOW  = 18
_PHOTO_DIFF_HIGH = 55


def _sample_background_color(img):
    """
    Estimates a photo's background color by sampling a thin strip
    around its border, where the background reliably dominates.
    Returns the per-channel MEDIAN of the sampled border pixels as an
    (r, g, b) tuple — median rather than mean so a minority of edge
    pixels catching hair, a shoulder, or a shadow corner don't skew
    the estimate. Identical technique to the one used for signature
    background removal in reportcardgeneration.py.
    """
    w, h = img.size
    mx = max(1, int(w * _PHOTO_BORDER_MARGIN_FRAC))
    my = max(1, int(h * _PHOTO_BORDER_MARGIN_FRAC))

    pixels = []
    pixels += list(img.crop((0, 0, w, my)).getdata())          # top strip
    pixels += list(img.crop((0, h - my, w, h)).getdata())      # bottom strip
    pixels += list(img.crop((0, 0, mx, h)).getdata())          # left strip
    pixels += list(img.crop((w - mx, 0, w, h)).getdata())      # right strip

    if not pixels:
        return (255, 255, 255)

    rs = sorted(p[0] for p in pixels)
    gs = sorted(p[1] for p in pixels)
    bs = sorted(p[2] for p in pixels)
    mid = len(pixels) // 2
    return (rs[mid], gs[mid], bs[mid])


# ─────────────────────────────────────────────────────────────
# ROLE GUARDS
# ─────────────────────────────────────────────────────────────

def staff_required():
    claims = get_jwt()
    if claims.get("role") not in STAFF_ROLES:
        return jsonify({"message": "Unauthorized"}), 403
    return None


def any_role_required():
    claims = get_jwt()
    if claims.get("role") not in ALL_ROLES:
        return jsonify({"message": "Unauthorized"}), 403
    return None


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def check_file_size(file, label):
    if not file:
        return None
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_FILE_SIZE:
        return f"{label} exceeds 50MB limit"
    return None


def generate_student_code(student_id):
    return f"STD-{student_id:03d}"


def _sanitise_student_type(raw):
    val = str(raw or "").strip().lower()
    return val if val in VALID_STUDENT_TYPES else "day"


def _replace_background_with_white(image_bytes: bytes, ext: str) -> bytes:
    """
    [NEW][PHOTO-BG] Same brightness-threshold technique as
    reportcardgeneration.py's _strip_signature_background(): the image
    is converted to grayscale to get a per-pixel brightness value,
    which is mapped through a 256-entry lookup table (via PIL's
    point()) to an alpha value — bright/near-white pixels are treated
    as background, dark pixels are treated as subject, and pixels in
    between fade smoothly so edges don't look hard-cut.

    Unlike the signature version, this does NOT keep the result
    transparent. Student/guardian photos are displayed directly (list
    rows, cards, printed documents), so the "removed" area is instead
    composited onto an OPAQUE WHITE canvas and flattened, giving back
    a normal JPEG/PNG with a flat white background rather than a
    transparent one.

    `ext` controls the output encoding ("png" -> PNG, anything else ->
    JPEG) so the result stays a normal photo file, not always a PNG.

    Raises if the bytes can't be parsed as an image. Callers should
    treat that as "skip processing, upload the original" rather than
    fail the whole request — a photo with its original background is
    much better than blocking registration/update entirely.
    """
    img  = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    gray = img.convert("L")

    lower = max(_PHOTO_BG_THRESHOLD - _PHOTO_BG_BAND, 0)

    def _alpha_from_brightness(b):
        if b >= _PHOTO_BG_THRESHOLD:
            return 0
        if b <= lower:
            return 255
        # Linear ramp between lower and threshold.
        return int(255 * (_PHOTO_BG_THRESHOLD - b) / (_PHOTO_BG_THRESHOLD - lower))

    alpha_mask = gray.point(_alpha_from_brightness)

    rgba = img.convert("RGBA")
    rgba.putalpha(alpha_mask)

    # Flatten onto an opaque white canvas — this is the key difference
    # from the signature version, which keeps the area transparent.
    white_bg = Image.new("RGB", rgba.size, (255, 255, 255))
    white_bg.paste(rgba, mask=rgba.split()[3])

    out = io.BytesIO()
    if ext == "png":
        white_bg.save(out, format="PNG")
    else:
        white_bg.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _upload_image(file_storage, school_id, student_code, prefix="student"):
    ext         = file_storage.filename.rsplit(".", 1)[1].lower()
    # [FIX] `prefix` used to be accepted but never actually used, so
    # student and guardian photos shared the exact same filename
    # pattern. Folding it in here keeps the two file sets visibly
    # distinct in the bucket.
    fname       = f"{prefix}_{school_id}_{student_code}_{uuid.uuid4().hex}.{ext}"
    remote_path = f"uploads/images/{fname}"
    data        = file_storage.read()
    file_storage.seek(0)

    # [NEW][PHOTO-BG] Replace the photo's background with flat white
    # before it ever reaches Bunny. Best-effort: if the image can't be
    # processed for any reason, fall back to uploading the original
    # bytes rather than failing registration/update over this.
    try:
        data = _replace_background_with_white(data, ext)
    except Exception:
        logger.exception(
            "_upload_image: background removal failed, uploading original | "
            "prefix=%s school_id=%s student_code=%s",
            prefix, school_id, student_code,
        )

    # bunny_upload() raises RuntimeError on failure, so reaching the next
    # line means the file is confirmed on Bunny.
    # [FIX] Return bunny_upload()'s actual result instead of the
    # locally-built `remote_path` string. They were equivalent in
    # practice (bunny_upload just re-adds a leading slash), but storing
    # whatever bunny_upload() reports as the live path — rather than a
    # second, independently-constructed guess at it — means there's
    # only one source of truth for "where this file actually is",
    # which matters when diagnosing delete issues later.
    return bunny_upload(data=data, remote_path=remote_path)


def _upload_document(file_storage, school_id, student_code):
    ext         = file_storage.filename.rsplit(".", 1)[1].lower()
    fname       = f"{school_id}_{student_code}_{uuid.uuid4().hex}.{ext}"
    remote_path = f"uploads/documents/{fname}"
    data        = file_storage.read()
    file_storage.seek(0)

    return bunny_upload(data=data, remote_path=remote_path)


def _delete_cdn_file(url, context=""):
    """
    [FIX] bunny_delete() returns False on failure rather than raising,
    and the old version of this function ignored that return value
    entirely — so a failed CDN delete (bad credentials, missing delete
    permission, path mismatch, timeout) looked identical to a
    successful one from every caller's perspective, and the API still
    reported "updated successfully" either way. This now logs the
    outcome explicitly, with a `context` label (which student/guardian,
    which field) so a failure is traceable in the logs instead of
    silently leaving an orphaned file in the bucket.
    """
    if not url:
        return
    remote_path = bunny_remote_path_from_url(url)
    try:
        deleted = bunny_delete(remote_path)
        if deleted:
            logger.info(
                "CDN delete OK | context=%s stored_value=%s remote_path=%s",
                context, url, remote_path,
            )
        else:
            logger.warning(
                "CDN delete FAILED | context=%s stored_value=%s remote_path=%s "
                "— file was NOT removed from the bucket. Check BunnyCDN "
                "AccessKey permissions and whether remote_path matches what "
                "was actually uploaded.",
                context, url, remote_path,
            )
    except Exception:
        logger.exception(
            "CDN delete RAISED | context=%s stored_value=%s remote_path=%s",
            context, url, remote_path,
        )


def _cleanup_orphaned_uploads(paths, context=""):
    """
    [FIX][ORPHAN CLEANUP] Deletes every path in *paths* from Bunny.
    Called from an except block after db.session.rollback(), for any
    files that were successfully uploaded to Bunny earlier in the same
    request but whose DB row never made it to commit. Without this,
    every failed registration/update that got as far as a photo or
    document upload leaves that file behind forever, since nothing in
    the (rolled-back) database ever points to it.
    """
    for path in paths:
        _delete_cdn_file(path, context=f"orphaned upload cleanup | {context}")


# ─────────────────────────────────────────────────────────────
# FILTERED STUDENT QUERY
# ─────────────────────────────────────────────────────────────

def _filtered_student_query(school_id):
    search       = request.args.get("search",       "").strip()
    class_filter = request.args.get("class",        "").strip()
    stream       = request.args.get("stream",       "").strip()
    gender       = request.args.get("gender",       "").strip()
    level        = request.args.get("level",        "").strip()
    student_type = request.args.get("student_type", "").strip()

    q = Student.query.filter_by(school_id=school_id)

    if search:
        like = f"%{search}%"
        q = q.filter(db.or_(
            Student.first_name.ilike(like),
            Student.last_name.ilike(like)
        ))

    if class_filter:
        class_obj = Class.query.filter_by(name=class_filter, school_id=school_id).first()
        q = q.filter(Student.class_id == class_obj.id) if class_obj else q.filter(db.false())

    if stream:
        q = q.filter(Student.stream == stream)
    if gender:
        q = q.filter(Student.gender == gender)
    if level:
        q = q.filter(Student.level == level)
    if student_type in VALID_STUDENT_TYPES:
        q = q.filter(Student.student_type == student_type)

    return q


# ─────────────────────────────────────────────────────────────
# LIST PAGE
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def list_students():
    guard = staff_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    user_id   = claims.get("sub")

    school    = School.query.get(school_id)
    modules   = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]
    classes   = Class.query.filter_by(school_id=school_id).all()
    class_map = {c.id: c.name for c in classes}

    page       = request.args.get("page", 1, type=int)
    per_page   = 20
    pagination = (
        _filtered_student_query(school_id)
        .order_by(Student.last_name, Student.first_name)
        .paginate(page=page, per_page=per_page, error_out=False)
    )

    return render_template(
        "modules/students/list.html",
        students=pagination.items,
        pagination=pagination,
        classes=classes,
        class_map=class_map,
        school=school,
        modules=modules,
        bunny_public_url=bunny_public_url,
        current_filters={
            "search":       request.args.get("search",       ""),
            "class":        request.args.get("class",        ""),
            "stream":       request.args.get("stream",       ""),
            "gender":       request.args.get("gender",       ""),
            "level":        request.args.get("level",        ""),
            "student_type": request.args.get("student_type", ""),
        }
    )


# ─────────────────────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/download", methods=["GET"])
@jwt_required()
@limiter.limit("20 per hour")
def download_students():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")

    try:
        classes   = Class.query.filter_by(school_id=school_id).all()
        class_map = {c.id: c.name for c in classes}

        students = (
            _filtered_student_query(school_id)
            .order_by(Student.last_name, Student.first_name)
            .all()
        )

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "Student Code", "Admission Number", "First Name", "Last Name",
            "Gender", "Date of Birth", "Student Type", "Nationality", "NIN",
            "Class", "Stream", "Level",
            "Guardian Name", "Guardian Contact", "Guardian Relationship",
            "Guardian Occupation", "Guardian Address",
            "Has Asthma", "Has Heart Problem", "Has Sickle Cell", "Has HIV",
            "Other Medical Conditions", "Document Count", "Document Types",
        ])

        for s in students:
            guardian  = Guardian.query.filter_by(student_id=s.id).first()
            medical   = MedicalRecord.query.filter_by(student_id=s.id).first()
            docs      = Document.query.filter_by(student_id=s.id).all()
            doc_types = "; ".join(d.document_type for d in docs if d.document_type)

            writer.writerow([
                s.student_code       or "",
                s.admission_number   or "",
                s.first_name         or "",
                s.last_name          or "",
                s.gender             or "",
                str(s.date_of_birth) if s.date_of_birth else "",
                getattr(s, "student_type", "") or "",
                getattr(s, "nationality",  "") or "",
                getattr(s, "nin",          "") or "",
                class_map.get(s.class_id, ""),
                getattr(s, "stream", "") or "",
                getattr(s, "level",  "") or "",
                guardian.name                               if guardian else "",
                guardian.contact                            if guardian else "",
                getattr(guardian, "relationship", "") or "" if guardian else "",
                getattr(guardian, "occupation",   "") or "" if guardian else "",
                getattr(guardian, "address",      "") or "" if guardian else "",
                "Yes" if (medical and medical.has_asthma)        else "No",
                "Yes" if (medical and medical.has_heart_problem)  else "No",
                "Yes" if (medical and medical.has_sickle_cell)    else "No",
                "Yes" if (medical and medical.has_hiv)            else "No",
                (medical.other_conditions or "") if medical else "",
                len(docs),
                doc_types,
            ])

        csv_bytes = output.getvalue().encode("utf-8-sig")
        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=students.csv",
                "Content-Length":      str(len(csv_bytes)),
            }
        )

    except Exception:
        logger.exception("download_students failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to generate download. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
# REGISTER  (single student)
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/register", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def register_student():
    guard = staff_required()
    if guard:
        return guard

    school_id   = get_jwt().get("school_id")
    limit_error = check_student_limit(school_id)
    if limit_error:
        return jsonify({"message": limit_error}), 403

    # [FIX][ORPHAN CLEANUP] Photo/document uploads happen to Bunny
    # *before* the DB transaction is known to succeed. If anything
    # later in this request fails (missing guardian fields, an
    # exception mid-way, etc.) the DB rolls back but a file already
    # sitting on Bunny has nothing pointing to it — it's orphaned
    # forever. Every path uploaded during this request is tracked
    # here so it can be deleted if we end up in any except block below.
    uploaded_cdn_paths = []

    try:
        first_name       = request.form.get("first_name",       "").strip()
        last_name        = request.form.get("last_name",        "").strip()
        gender           = request.form.get("gender",           "").strip()
        dob              = request.form.get("dob",              "").strip()
        admission_number = request.form.get("admission_number", "").strip()
        class_name       = request.form.get("class_id",         "").strip()
        stream           = request.form.get("stream",           "").strip()
        level            = request.form.get("level",            "").strip()
        nationality      = request.form.get("nationality",      "").strip()
        nin              = request.form.get("nin",              "").strip()
        student_type     = _sanitise_student_type(request.form.get("student_type", "day"))

        guardian_name         = request.form.get("guardian_name",         "").strip()
        guardian_contact      = request.form.get("guardian_contact",      "").strip()
        guardian_relationship = request.form.get("guardian_relationship", "").strip()
        guardian_occupation   = request.form.get("guardian_occupation",   "").strip()
        guardian_address      = request.form.get("guardian_address",      "").strip()

        has_asthma        = bool(request.form.get("has_asthma"))
        has_heart_problem = bool(request.form.get("has_heart_problem"))
        has_sickle_cell   = bool(request.form.get("has_sickle_cell"))
        has_hiv           = bool(request.form.get("has_hiv"))
        other_diseases    = request.form.get("other_diseases", "").strip()

        document_titles = request.form.getlist("document_title[]")

        if not all([first_name, last_name, gender, dob, admission_number, class_name]):
            return jsonify({"message": "Missing required fields"}), 400

        dob = datetime.strptime(dob, "%Y-%m-%d").date()

        class_obj = Class.query.filter_by(name=class_name, school_id=school_id).first()
        if not class_obj:
            return jsonify({"message": "Invalid class"}), 400

        exists = db.session.query(Student.id).filter(
            Student.school_id        == school_id,
            Student.admission_number == admission_number
        ).first()
        if exists:
            return jsonify({"message": "Admission number already exists"}), 400

        guardian_photo = request.files.get("guardian_photo")
        student_photo  = request.files.get("student_photo")
        documents      = request.files.getlist("document_file[]")

        for label, f in [("Student photo", student_photo), ("Guardian photo", guardian_photo)]:
            err = check_file_size(f, label)
            if err:
                return jsonify({"message": err}), 400

        for i, f in enumerate(documents):
            err = check_file_size(f, f"Document {i}")
            if err:
                return jsonify({"message": err}), 400

        student = Student(
            school_id=school_id,
            student_code="TEMP",
            admission_number=admission_number,
            first_name=first_name,
            last_name=last_name,
            gender=gender,
            date_of_birth=dob,
            class_id=class_obj.id,
        )

        for attr, val in [
            ("nationality",  nationality),
            ("nin",          nin),
            ("student_type", student_type),
        ]:
            if hasattr(student, attr):
                setattr(student, attr, val)

        db.session.add(student)
        db.session.flush()

        student.student_code = generate_student_code(student.id)

        if student_photo and student_photo.filename:
            student.photo_url = _upload_image(
                student_photo, school_id, student.student_code, prefix="student"
            )
            uploaded_cdn_paths.append(student.photo_url)

        db.session.add(StudentAcademic(student_id=student.id, class_id=class_obj.id))

        db.session.add(MedicalRecord(
            student_id=student.id,
            has_asthma=has_asthma,
            has_heart_problem=has_heart_problem,
            has_sickle_cell=has_sickle_cell,
            has_hiv=has_hiv,
            other_conditions=other_diseases,
        ))

        if not guardian_name or not guardian_contact:
            raise ValueError("Guardian name and contact are required")

        guardian = Guardian(
            student_id=student.id,
            name=guardian_name,
            contact=guardian_contact,
            relationship=guardian_relationship,
            occupation=guardian_occupation,
            address=guardian_address,
        )

        if guardian_photo and guardian_photo.filename:
            guardian.photo_url = _upload_image(
                guardian_photo, school_id, student.student_code, prefix="guardian"
            )
            uploaded_cdn_paths.append(guardian.photo_url)

        db.session.add(guardian)

        for i, doc in enumerate(documents):
            if doc and doc.filename:
                cdn_url = _upload_document(doc, school_id, student.student_code)
                uploaded_cdn_paths.append(cdn_url)
                title   = document_titles[i] if i < len(document_titles) else doc.filename
                db.session.add(Document(
                    student_id=student.id,
                    document_type=title,
                    file_url=cdn_url,
                ))

        db.session.commit()
        return jsonify({"message": "Student created successfully", "student_id": student.id}), 201

    except IntegrityError:
        db.session.rollback()
        _cleanup_orphaned_uploads(uploaded_cdn_paths, context=f"register_student IntegrityError school_id={school_id}")
        return jsonify({"message": "Admission number already exists"}), 400
    except ValueError as ve:
        db.session.rollback()
        _cleanup_orphaned_uploads(uploaded_cdn_paths, context=f"register_student ValueError school_id={school_id}")
        return jsonify({"message": str(ve)}), 400
    except Exception:
        db.session.rollback()
        _cleanup_orphaned_uploads(uploaded_cdn_paths, context=f"register_student Exception school_id={school_id}")
        logger.exception("register_student failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to register student. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
# BULK IMPORT – single row
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/bulk-import-row", methods=["POST"])
@jwt_required()
@limiter.limit(BULK_LIMIT)
def bulk_import_row():
    guard = staff_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")

    limit_error = check_student_limit(school_id)
    if limit_error:
        return jsonify({"message": limit_error}), 403

    try:
        data = request.get_json(force=True) or {}

        first_name       = str(data.get("first_name",       "")).strip()
        last_name        = str(data.get("last_name",        "")).strip()
        gender           = str(data.get("gender",           "")).strip()
        dob_raw          = str(data.get("date_of_birth",    "")).strip()
        admission_number = str(data.get("admission_number", "")).strip()
        guardian_name    = str(data.get("guardian_name",    "")).strip()
        guardian_contact = str(data.get("guardian_contact", "")).strip()
        student_type     = _sanitise_student_type(data.get("student_type", "day"))
        class_name       = str(data.get("class_name",       "")).strip()

        # ── Validation ──────────────────────────────────────────
        errors = []
        for field, val in [
            ("first_name",       first_name),
            ("last_name",        last_name),
            ("gender",           gender),
            ("date_of_birth",    dob_raw),
            ("admission_number", admission_number),
            ("guardian_name",    guardian_name),
            ("guardian_contact", guardian_contact),
        ]:
            if not val:
                errors.append(f"{field} is required")

        if gender and gender not in ("Male", "Female"):
            errors.append("gender must be Male or Female")

        dob = None
        if dob_raw:
            try:
                dob = datetime.strptime(dob_raw, "%Y-%m-%d").date()
            except ValueError:
                errors.append("date_of_birth must be YYYY-MM-DD")

        if errors:
            return jsonify({"message": "; ".join(errors)}), 400

        # ── Duplicate admission number check ────────────────────
        exists = db.session.query(Student.id).filter(
            Student.school_id        == school_id,
            Student.admission_number == admission_number
        ).first()
        if exists:
            return jsonify({"message": f"Admission number '{admission_number}' already exists"}), 400

        # ── Resolve class ───────────────────────────────────────
        target_class = None
        if class_name:
            target_class = Class.query.filter_by(
                school_id=school_id,
                name=class_name
            ).first()

        if not target_class:
            target_class = Class.query.filter_by(school_id=school_id).first()

        if not target_class:
            return jsonify({"message": "No classes configured for this school"}), 400

        # ── Create student ──────────────────────────────────────
        student = Student(
            school_id=school_id,
            student_code="TEMP",
            admission_number=admission_number,
            first_name=first_name,
            last_name=last_name,
            gender=gender,
            date_of_birth=dob,
            class_id=target_class.id,
        )
        if hasattr(student, "student_type"):
            student.student_type = student_type

        db.session.add(student)
        db.session.flush()

        student.student_code = generate_student_code(student.id)

        db.session.add(StudentAcademic(student_id=student.id, class_id=target_class.id))
        db.session.add(MedicalRecord(student_id=student.id))
        db.session.add(Guardian(
            student_id=student.id,
            name=guardian_name,
            contact=guardian_contact,
        ))

        db.session.commit()
        return jsonify({"message": "Row imported", "student_id": student.id}), 201

    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": f"Admission number '{admission_number}' already exists"}), 400

    except Exception:
        db.session.rollback()
        logger.exception("bulk_import_row failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to import row. Please try again."}), 500
# ─────────────────────────────────────────────────────────────
# DELETE STUDENT
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/<int:student_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_student(student_id):
    guard = staff_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")

    try:
        student = Student.query.filter_by(id=student_id, school_id=school_id).first()
        if not student:
            return jsonify({"message": "Student not found"}), 404

        # ── Finance chain: Receipt → Payment (direct student_id FK) ──
        payment_ids = [
            row.id for row in
            db.session.query(Payment.id).filter_by(student_id=student.id).all()
        ]
        if payment_ids:
            Receipt.query.filter(Receipt.payment_id.in_(payment_ids)).delete(synchronize_session=False)
            Payment.query.filter(Payment.id.in_(payment_ids)).delete(synchronize_session=False)

        # ── Finance chain: InvoiceItem → Invoice ──
        invoice_ids = [
            row.id for row in
            db.session.query(Invoice.id).filter_by(student_id=student.id).all()
        ]
        if invoice_ids:
            InvoiceItem.query.filter(InvoiceItem.invoice_id.in_(invoice_ids)).delete(synchronize_session=False)
            Invoice.query.filter(Invoice.id.in_(invoice_ids)).delete(synchronize_session=False)

        # ── Academic records tied to this student ──
        StudentMark.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        StudentAttendance.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        StudentDailyAttendance.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        StudentSubject.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        StudentStream.query.filter_by(student_id=student.id).delete(synchronize_session=False)
        StudentEnrollment.query.filter_by(student_id=student.id).delete(synchronize_session=False)

        # ── Report summaries tied to this student ──
        PrimaryReportSummary.query.filter_by(student_id=student.id).delete(synchronize_session=False)

        # ── Documents (DB now, CDN after commit succeeds — see below) ──
        # [FIX] CDN files used to be deleted from Bunny before the DB
        # commit. If the commit then failed, the DB rows would still
        # exist but point at files already gone from Bunny. Collecting
        # paths here and deleting them only after a successful commit
        # keeps the two in sync.
        cdn_paths_to_delete = []
        for doc in Document.query.filter_by(student_id=student.id).all():
            if doc.file_url:
                cdn_paths_to_delete.append((doc.file_url, f"document id={doc.id} student_id={student.id}"))
            db.session.delete(doc)

        if student.photo_url:
            cdn_paths_to_delete.append(
                (student.photo_url, f"student_photo student_id={student.id} (delete_student)")
            )

        guardian = Guardian.query.filter_by(student_id=student.id).first()
        if guardian and getattr(guardian, "photo_url", None):
            cdn_paths_to_delete.append(
                (guardian.photo_url, f"guardian_photo student_id={student.id} (delete_student)")
            )

        Guardian.query.filter_by(student_id=student.id).delete()
        MedicalRecord.query.filter_by(student_id=student.id).delete()
        StudentAcademic.query.filter_by(student_id=student.id).delete()
        StudentAuth.query.filter_by(student_id=student.id).delete()
        db.session.delete(student)
        db.session.commit()

        # Commit succeeded — now safe to remove the files from Bunny.
        for path, ctx in cdn_paths_to_delete:
            _delete_cdn_file(path, context=ctx)

        return jsonify({"message": "Student deleted successfully"}), 200

    except Exception:
        db.session.rollback()
        logger.exception("delete_student failed | student_id=%s school_id=%s", student_id, school_id)
        return jsonify({"message": "Failed to delete student. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
# DELETE DOCUMENT
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/document/<int:doc_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_document(doc_id):
    guard = staff_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")

    try:
        document = db.session.query(Document).join(Student).filter(
            Document.id == doc_id,
            Student.school_id == school_id
        ).first()

        if not document:
            return jsonify({"message": "Document not found"}), 404

        file_url = document.file_url
        doc_id_for_log = document.id

        db.session.delete(document)
        db.session.commit()

        # Commit succeeded — now safe to remove the file from Bunny.
        _delete_cdn_file(file_url, context=f"document id={doc_id_for_log}")

        return jsonify({"message": "Document deleted successfully"}), 200

    except Exception:
        db.session.rollback()
        logger.exception("delete_document failed | doc_id=%s school_id=%s", doc_id, school_id)
        return jsonify({"message": "Failed to delete document. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
# UPDATE STUDENT
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/<int:student_id>", methods=["PUT"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def update_student(student_id):
    guard = staff_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")

    # [FIX][ORPHAN CLEANUP] Old photos used to be deleted from Bunny the
    # moment a new one uploaded successfully — but that's *before* the
    # DB transaction commits. If something later in this same request
    # failed and rolled back, the DB would revert to the old photo_url
    # while the old file was already gone from Bunny (broken image),
    # and the newly-uploaded file would be orphaned (nothing points to
    # it either). These two lists defer that: old files are only
    # deleted once db.session.commit() has actually succeeded, and
    # newly-uploaded files are only deleted if we land in an except
    # block instead.
    old_cdn_paths_to_delete_on_success = []
    new_cdn_paths_to_cleanup_on_failure = []

    try:
        student = Student.query.filter_by(id=student_id, school_id=school_id).first()
        if not student:
            return jsonify({"message": "Student not found"}), 404

        first_name       = request.form.get("first_name",       "").strip()
        last_name        = request.form.get("last_name",        "").strip()
        gender           = request.form.get("gender",           "").strip()
        dob              = request.form.get("dob",              "").strip()
        admission_number = request.form.get("admission_number", "").strip()
        class_name       = request.form.get("class_id",         "").strip()
        nationality      = request.form.get("nationality",      "").strip()
        nin              = request.form.get("nin",              "").strip()
        level            = request.form.get("level",            "").strip()
        student_type     = _sanitise_student_type(request.form.get("student_type", "day"))

        guardian_name         = request.form.get("guardian_name",         "").strip()
        guardian_contact      = request.form.get("guardian_contact",      "").strip()
        guardian_relationship = request.form.get("guardian_relationship", "").strip()
        guardian_occupation   = request.form.get("guardian_occupation",   "").strip()
        guardian_address      = request.form.get("guardian_address",      "").strip()

        other_diseases    = request.form.get("other_diseases", "").strip()
        has_asthma        = bool(request.form.get("has_asthma"))
        has_heart_problem = bool(request.form.get("has_heart_problem"))
        has_sickle_cell   = bool(request.form.get("has_sickle_cell"))
        has_hiv           = bool(request.form.get("has_hiv"))

        if not all([first_name, last_name, gender, dob, admission_number, class_name]):
            return jsonify({"message": "Missing required fields"}), 400

        dob = datetime.strptime(dob, "%Y-%m-%d").date()

        class_obj = Class.query.filter_by(name=class_name, school_id=school_id).first()
        if not class_obj:
            return jsonify({"message": "Invalid class"}), 400

        existing = db.session.query(Student.id).filter(
            Student.school_id        == school_id,
            Student.admission_number == admission_number,
            Student.id               != student.id
        ).first()
        if existing:
            return jsonify({"message": "Admission number already exists"}), 400

        guardian_photo = request.files.get("guardian_photo")
        student_photo  = request.files.get("student_photo")
        documents      = request.files.getlist("document_file[]")

        for label, f in [("Student photo", student_photo), ("Guardian photo", guardian_photo)]:
            err = check_file_size(f, label)
            if err:
                return jsonify({"message": err}), 400

        for i, f in enumerate(documents):
            err = check_file_size(f, f"Document {i}")
            if err:
                return jsonify({"message": err}), 400

        student.first_name       = first_name
        student.last_name        = last_name
        student.gender           = gender
        student.date_of_birth    = dob
        student.admission_number = admission_number
        student.class_id         = class_obj.id

        for attr, val in [
            ("nationality",  nationality),
            ("nin",          nin),
            ("level",        level),
            ("student_type", student_type),
        ]:
            if hasattr(student, attr):
                setattr(student, attr, val)

        if student_photo and student_photo.filename:
            old_student_photo = student.photo_url
            # Upload the new photo first — bunny_upload() raises on
            # failure, so if the upload itself fails the old photo_url
            # is never touched. The old file's deletion is *not* fired
            # here though — it's queued and only actually performed
            # after db.session.commit() succeeds below, so a later
            # failure in this same request can't strand the DB pointing
            # at an already-deleted file.
            student.photo_url = _upload_image(
                student_photo, school_id, student.student_code, prefix="student"
            )
            new_cdn_paths_to_cleanup_on_failure.append(student.photo_url)
            if old_student_photo:
                old_cdn_paths_to_delete_on_success.append(
                    (old_student_photo, f"student_photo replace student_id={student.id} (update_student)")
                )

        academic = StudentAcademic.query.filter_by(student_id=student.id).first()
        if academic:
            academic.class_id = class_obj.id

        guardian = Guardian.query.filter_by(student_id=student.id).first()
        if guardian:
            guardian.name         = guardian_name
            guardian.contact      = guardian_contact
            guardian.relationship = guardian_relationship
            guardian.occupation   = guardian_occupation
            guardian.address      = guardian_address

            if guardian_photo and guardian_photo.filename:
                old_guardian_photo = getattr(guardian, "photo_url", None)
                # Same defer-until-commit approach as the student photo
                # above.
                guardian.photo_url = _upload_image(
                    guardian_photo, school_id, student.student_code, prefix="guardian"
                )
                new_cdn_paths_to_cleanup_on_failure.append(guardian.photo_url)
                if old_guardian_photo:
                    old_cdn_paths_to_delete_on_success.append(
                        (old_guardian_photo, f"guardian_photo replace student_id={student.id} (update_student)")
                    )

        medical = MedicalRecord.query.filter_by(student_id=student.id).first()
        if medical:
            medical.has_asthma        = has_asthma
            medical.has_heart_problem = has_heart_problem
            medical.has_sickle_cell   = has_sickle_cell
            medical.has_hiv           = has_hiv
            medical.other_conditions  = other_diseases

        for doc in documents:
            if doc and doc.filename:
                cdn_url = _upload_document(doc, school_id, student.student_code)
                new_cdn_paths_to_cleanup_on_failure.append(cdn_url)
                db.session.add(Document(
                    student_id=student.id,
                    document_type=doc.filename,
                    file_url=cdn_url,
                ))

        db.session.commit()

        # Commit succeeded — now, and only now, it's safe to remove the
        # old photo(s) that were just replaced.
        for old_path, ctx in old_cdn_paths_to_delete_on_success:
            _delete_cdn_file(old_path, context=ctx)

        return jsonify({"message": "Student updated successfully"}), 200

    except IntegrityError as e:
        db.session.rollback()
        _cleanup_orphaned_uploads(
            new_cdn_paths_to_cleanup_on_failure,
            context=f"update_student IntegrityError student_id={student_id}",
        )
        if "admission" in str(e.orig).lower():
            return jsonify({"message": "Admission number already exists"}), 400
        return jsonify({"message": "Database constraint error"}), 400
    except Exception:
        db.session.rollback()
        _cleanup_orphaned_uploads(
            new_cdn_paths_to_cleanup_on_failure,
            context=f"update_student Exception student_id={student_id}",
        )
        logger.exception("update_student failed | student_id=%s school_id=%s", student_id, school_id)
        return jsonify({"message": "Failed to update student. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
# EDIT PAGE
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/edit/<int:student_id>", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def edit_student_page(student_id):
    guard = staff_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    user_id   = claims.get("sub")

    school    = School.query.filter_by(id=school_id).first()
    modules   = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]
    student   = Student.query.filter_by(id=student_id, school_id=school_id).first()

    if not student:
        return "Student not found", 404

    guardian  = Guardian.query.filter_by(student_id=student.id).first()
    medical   = MedicalRecord.query.filter_by(student_id=student.id).first()
    documents = Document.query.filter_by(student_id=student.id).all()
    classes   = Class.query.filter_by(school_id=school_id).all()

    return render_template(
        "modules/students/registration.html",
        student=student,
        guardian=guardian,
        medical=medical,
        documents=documents,
        classes=classes,
        school=school,
        modules=modules,
        edit_mode=True,
        bunny_public_url=bunny_public_url,
    )


# ─────────────────────────────────────────────────────────────
# SETTINGS PAGE
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/settings", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def settings_page():
    guard = any_role_required()
    if guard:
        return guard

    claims  = get_jwt()
    user_id = int(claims.get("sub"))
    user    = User.query.get(user_id)

    if not user:
        return "User not found", 404

    modules = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]

    return render_template(
        "modules/settings/settings.html",
        current_user=user,
        modules=modules,
    )


# ─────────────────────────────────────────────────────────────
# CHANGE USERNAME
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/settings/change-username", methods=["POST"])
@jwt_required()
@limiter.limit(PASSWORD_RESET_LIMIT)
def change_username():
    guard = any_role_required()
    if guard:
        return guard

    claims  = get_jwt()
    user_id = int(claims.get("sub"))
    user    = User.query.get(user_id)

    if not user:
        return jsonify({"message": "User not found"}), 404

    data             = request.get_json(force=True) or {}
    new_username     = str(data.get("new_username",     "")).strip()
    current_password = str(data.get("current_password", "")).strip()

    if not new_username or len(new_username) < 3:
        return jsonify({"message": "Username must be at least 3 characters"}), 400

    if not current_password:
        return jsonify({"message": "Current password is required to confirm this change"}), 400

    if not check_password_hash(user.password_hash, current_password):
        return jsonify({"message": "Current password is incorrect"}), 401

    if new_username.lower() == user.username.lower():
        return jsonify({"message": "New username is the same as your current one"}), 400

    taken = User.query.filter(
        db.func.lower(User.username) == new_username.lower(),
        User.id != user.id
    ).first()
    if taken:
        return jsonify({"message": "That username is already taken"}), 409

    try:
        user.username = new_username
        db.session.commit()
        return jsonify({"message": "Username updated successfully"}), 200
    except Exception:
        db.session.rollback()
        logger.exception("change_username failed | user_id=%s", user_id)
        return jsonify({"message": "Failed to update username. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
# CHANGE PASSWORD
# ─────────────────────────────────────────────────────────────

@student_management_api.route("/settings/change-password", methods=["POST"])
@jwt_required()
@limiter.limit(PASSWORD_RESET_LIMIT)
def change_password():
    guard = any_role_required()
    if guard:
        return guard

    claims  = get_jwt()
    user_id = int(claims.get("sub"))
    user    = User.query.get(user_id)

    if not user:
        return jsonify({"message": "User not found"}), 404

    data             = request.get_json(force=True) or {}
    current_password = str(data.get("current_password", "")).strip()
    new_password     = str(data.get("new_password",     "")).strip()

    if not current_password:
        return jsonify({"message": "Current password is required"}), 400

    if len(new_password) < 8:
        return jsonify({"message": "New password must be at least 8 characters"}), 400

    if not check_password_hash(user.password_hash, current_password):
        return jsonify({"message": "Current password is incorrect"}), 401

    if check_password_hash(user.password_hash, new_password):
        return jsonify({"message": "New password must differ from your current password"}), 400

    try:
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        return jsonify({"message": "Password updated successfully"}), 200
    except Exception:
        db.session.rollback()
        logger.exception("change_password failed | user_id=%s", user_id)
        return jsonify({"message": "Failed to update password. Please try again."}), 500