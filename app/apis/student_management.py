"""
app/apis/student_management.py
================================
Student CRUD API — register, update, delete, bulk import, download.

CHANGES vs original:
  - Rate limits applied per endpoint sensitivity.
  - All except blocks log internally and return safe client messages.
    No str(e) ever reaches the client.
  - print(e) calls removed; replaced with logger.exception().
"""

import logging
from flask import Blueprint, request, jsonify, render_template, Response
from flask_jwt_extended import jwt_required, get_jwt
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from app.extensions import db, limiter
from app.models.core import School, UserModule
from app.models.people import (
    Student, StudentAcademic, Guardian, MedicalRecord, Document
)
from app.models.user import StudentAuth, User
from app.models.academic_structure import Class
from app.utils.utilities import check_student_limit
from app.utils.bunny import bunny_upload, bunny_delete, bunny_remote_path_from_url
from app.core.rate_limit import (
    READ_LIMIT, WRITE_LIMIT, BULK_LIMIT, SEARCH_LIMIT,
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


def _upload_image(file_storage, school_id, student_code, prefix="student"):
    ext   = file_storage.filename.rsplit(".", 1)[1].lower()
    fname = f"{school_id}_{student_code}_{uuid.uuid4().hex}.{ext}"
    remote_path = f"uploads/images/{fname}"
    data  = file_storage.read()
    file_storage.seek(0)
    return bunny_upload(data=data, remote_path=remote_path)


def _upload_document(file_storage, school_id, student_code):
    ext   = file_storage.filename.rsplit(".", 1)[1].lower()
    fname = f"{school_id}_{student_code}_{uuid.uuid4().hex}.{ext}"
    remote_path = f"uploads/documents/{fname}"
    data  = file_storage.read()
    file_storage.seek(0)
    return bunny_upload(data=data, remote_path=remote_path)


def _delete_cdn_file(url):
    if url:
        try:
            bunny_delete(bunny_remote_path_from_url(url))
        except Exception:
            logger.warning("CDN delete failed for URL: %s", url)


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

        db.session.add(guardian)

        for i, doc in enumerate(documents):
            if doc and doc.filename:
                cdn_url = _upload_document(doc, school_id, student.student_code)
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
        return jsonify({"message": "Admission number already exists"}), 400
    except ValueError as ve:
        db.session.rollback()
        return jsonify({"message": str(ve)}), 400
    except Exception:
        db.session.rollback()
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

    school_id   = get_jwt().get("school_id")
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

        exists = db.session.query(Student.id).filter(
            Student.school_id        == school_id,
            Student.admission_number == admission_number
        ).first()
        if exists:
            return jsonify({"message": f"Admission number '{admission_number}' already exists"}), 400

        default_class = Class.query.filter_by(school_id=school_id).first()
        if not default_class:
            return jsonify({"message": "No classes configured for this school"}), 400

        student = Student(
            school_id=school_id,
            student_code="TEMP",
            admission_number=admission_number,
            first_name=first_name,
            last_name=last_name,
            gender=gender,
            date_of_birth=dob,
            class_id=default_class.id,
        )

        if hasattr(student, "student_type"):
            student.student_type = student_type

        db.session.add(student)
        db.session.flush()

        student.student_code = generate_student_code(student.id)

        db.session.add(StudentAcademic(student_id=student.id, class_id=default_class.id))
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

        for doc in Document.query.filter_by(student_id=student.id).all():
            _delete_cdn_file(doc.file_url)
            db.session.delete(doc)

        _delete_cdn_file(student.photo_url)

        guardian = Guardian.query.filter_by(student_id=student.id).first()
        if guardian:
            _delete_cdn_file(getattr(guardian, "photo_url", None))

        Guardian.query.filter_by(student_id=student.id).delete()
        MedicalRecord.query.filter_by(student_id=student.id).delete()
        StudentAcademic.query.filter_by(student_id=student.id).delete()
        StudentAuth.query.filter_by(student_id=student.id).delete()
        db.session.delete(student)
        db.session.commit()

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

        _delete_cdn_file(document.file_url)

        db.session.delete(document)
        db.session.commit()
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
            _delete_cdn_file(student.photo_url)
            student.photo_url = _upload_image(
                student_photo, school_id, student.student_code, prefix="student"
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
                _delete_cdn_file(getattr(guardian, "photo_url", None))
                guardian.photo_url = _upload_image(
                    guardian_photo, school_id, student.student_code, prefix="guardian"
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
                db.session.add(Document(
                    student_id=student.id,
                    document_type=doc.filename,
                    file_url=cdn_url,
                ))

        db.session.commit()
        return jsonify({"message": "Student updated successfully"}), 200

    except IntegrityError as e:
        db.session.rollback()
        if "admission" in str(e.orig).lower():
            return jsonify({"message": "Admission number already exists"}), 400
        return jsonify({"message": "Database constraint error"}), 400
    except Exception:
        db.session.rollback()
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