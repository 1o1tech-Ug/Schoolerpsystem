from flask import Blueprint, render_template, jsonify, request, Response
from flask_jwt_extended import jwt_required, get_jwt
from app.models.core import School, UserModule
from app.models.people import Student, Guardian, MedicalRecord, Document, StudentAcademic
from app.models.academic_structure import Class, StudentStream, Stream
from app.extensions import db, limiter
from app.core.rate_limit import READ_LIMIT, WRITE_LIMIT
from app.utils.bunny import bunny_public_url
import io
import csv
import logging

logger = logging.getLogger(__name__)

student_Management = Blueprint(
    "student_Management",
    __name__,
    url_prefix="/students"
)


# ─────────────────────────────────────────────────────────────
# SHARED FILTER HELPER
# ─────────────────────────────────────────────────────────────
def _apply_filters(query, school_id):
    search = request.args.get("search", "").strip()
    class_ = request.args.get("class",  "").strip()
    stream = request.args.get("stream", "").strip()
    gender = request.args.get("gender", "").strip()
    level  = request.args.get("level",  "").strip()

    if search:
        like = f"%{search}%"
        query = query.filter(
            db.or_(
                Student.first_name.ilike(like),
                Student.last_name.ilike(like)
            )
        )

    if class_:
        class_obj = Class.query.filter_by(name=class_, school_id=school_id).first()
        if class_obj:
            query = query.filter(Student.class_id == class_obj.id)
        else:
            query = query.filter(db.false())

    if stream:
        query = query.filter(Student.stream == stream)

    if gender:
        query = query.filter(Student.gender == gender)

    if level:
        query = query.filter(Student.level == level)

    return query


def _current_filters():
    return {
        "search": request.args.get("search", ""),
        "class":  request.args.get("class",  ""),
        "stream": request.args.get("stream", ""),
        "gender": request.args.get("gender", ""),
        "level":  request.args.get("level",  ""),
    }


def _guardian_query(school_id):
    """Filters students by school, then optionally by name search and class."""
    search = request.args.get("search", "").strip()
    class_ = request.args.get("class",  "").strip()

    q = Student.query.filter_by(school_id=school_id)

    if search:
        like = f"%{search}%"
        q = q.filter(
            db.or_(
                Student.first_name.ilike(like),
                Student.last_name.ilike(like)
            )
        )

    if class_:
        class_obj = Class.query.filter_by(name=class_, school_id=school_id).first()
        if class_obj:
            q = q.filter(Student.class_id == class_obj.id)
        else:
            q = q.filter(db.false())

    return q


# ─────────────────────────────────────────────────────────────
# REGISTRATION PAGE
# ─────────────────────────────────────────────────────────────
@student_Management.route("/registration", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def registration_page():

    claims = get_jwt()
    role   = claims.get("role")

    if role not in {"staff"}:
        return jsonify({"message": "Unauthorized"}), 403

    school_id = claims.get("school_id")
    school    = School.query.get(school_id)

    if not school:
        return jsonify({"message": "School not found"}), 404

    classes = Class.query.filter_by(school_id=school.id).all()
    user_id = int(claims.get("sub"))
    modules = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]

    return render_template(
        "modules/students/registration.html",
        school=school,
        modules=modules,
        role=role,
        classes=classes,
        bunny_public_url=bunny_public_url,
    )


# ─────────────────────────────────────────────────────────────
# STUDENT LIST / PROFILES PAGE
# ─────────────────────────────────────────────────────────────
@student_Management.route("/profiles", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def student_profiles_page():

    claims = get_jwt()
    role   = claims.get("role")

    if role not in {"staff"}:
        return jsonify({"message": "Unauthorized"}), 403

    school_id = claims.get("school_id")
    school    = School.query.get(school_id)

    if not school:
        return jsonify({"message": "School not found"}), 404

    base_query = Student.query.filter_by(school_id=school.id)
    filtered   = _apply_filters(base_query, school_id)

    page       = request.args.get("page", 1, type=int)
    per_page   = 10

    pagination = filtered.order_by(
        Student.last_name,
        Student.first_name
    ).paginate(page=page, per_page=per_page, error_out=False)

    students = pagination.items

    user_id = int(claims.get("sub"))

    modules = [
        m.module_name
        for m in UserModule.query.filter_by(user_id=user_id).all()
    ]

    classes = Class.query.filter_by(school_id=school.id).all()

    class_map = {
        c.id: c.name
        for c in classes
    }

    # ---------------------------------------------------
    # GET STUDENT STREAMS
    # ---------------------------------------------------

    # Get all student ids on current page
    student_ids = [student.id for student in students]

    # Get all stream assignments for these students
    student_streams = StudentStream.query.filter(
        StudentStream.student_id.in_(student_ids)
    ).all()

    # Get all unique stream ids
    stream_ids = list({
        ss.stream_id
        for ss in student_streams
        if ss.stream_id
    })

    # Get stream names
    streams = Stream.query.filter(
        Stream.id.in_(stream_ids)
    ).all()

    # Map stream id -> stream name
    stream_map = {
        stream.id: stream.name
        for stream in streams
    }

    # Map student id -> stream name
    student_stream_map = {}

    for ss in student_streams:
        student_stream_map[ss.student_id] = stream_map.get(
            ss.stream_id,
            "No Stream"
        )

    return render_template(
        "modules/students/student_profiles.html",
        school=school,
        classes=classes,
        modules=modules,
        students=students,
        pagination=pagination,
        class_map=class_map,
        student_stream_map=student_stream_map,
        current_filters=_current_filters(),
        bunny_public_url=bunny_public_url,
    )


# ─────────────────────────────────────────────────────────────
# INDIVIDUAL PROFILE PAGE
# ─────────────────────────────────────────────────────────────
@student_Management.route("/profile/<int:student_id>", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def student_profile_page(student_id):

    claims = get_jwt()

    if claims.get("role") not in {"staff"}:
        return jsonify({"message": "Unauthorized"}), 403

    school_id = claims.get("school_id")

    student = Student.query.filter_by(id=student_id, school_id=school_id).first()
    if not student:
        return jsonify({"message": "Student not found"}), 404

    try:
        guardian  = Guardian.query.filter_by(student_id=student.id).first()
        school    = School.query.filter_by(id=school_id).first()
        medical   = MedicalRecord.query.filter_by(student_id=student.id).first()
        documents = Document.query.filter_by(student_id=student.id).all()
        academic  = StudentAcademic.query.filter_by(student_id=student.id).first()
        class_obj = Class.query.filter_by(id=student.class_id).first()

        user_id = claims.get("sub")
        modules = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]

        return render_template(
            "modules/students/profile.html",
            student=student,
            guardian=guardian,
            medical=medical,
            documents=documents,
            academic=academic,
            class_obj=class_obj,
            school_id=school_id,
            school=school,
            modules=modules,
            bunny_public_url=bunny_public_url,
        )

    except Exception:
        logger.exception("student_profile_page failed | student_id=%s", student_id)
        return jsonify({"message": "Failed to load student profile. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
# GUARDIAN LIST PAGE  –  GET /students/guardians
# ─────────────────────────────────────────────────────────────
@student_Management.route("/guardians", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def guardian_list_page():

    claims = get_jwt()
    role   = claims.get("role")

    if role not in {"staff"}:
        return jsonify({"message": "Unauthorized"}), 403

    school_id = claims.get("school_id")
    school    = School.query.get(school_id)

    if not school:
        return jsonify({"message": "School not found"}), 404

    try:
        user_id = int(claims.get("sub"))
        modules = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]
        classes = Class.query.filter_by(school_id=school_id).all()

        page       = request.args.get("page", 1, type=int)
        per_page   = 15
        pagination = _guardian_query(school_id).order_by(
            Student.last_name, Student.first_name
        ).paginate(page=page, per_page=per_page, error_out=False)

        students = pagination.items

        guardians = []
        for s in students:
            g = Guardian.query.filter_by(student_id=s.id).first()
            guardians.append({
                "id":            g.id                                    if g else None,
                "student_name":  f"{s.first_name} {s.last_name}",
                "guardian_name": g.name                                  if g else "—",
                "relationship":  getattr(g, "relationship", "") or "—"  if g else "—",
                "contact":       g.contact                               if g else "—",
                "address":       getattr(g, "address", "")      or "—"  if g else "—",
                "occupation":    getattr(g, "occupation", "")   or "—"  if g else "—",
            })

        return render_template(
            "modules/students/guardian_info.html",
            guardians=guardians,
            pagination=pagination,
            classes=classes,
            school=school,
            modules=modules,
        )

    except Exception:
        logger.exception("guardian_list_page failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to load guardian list. Please try again."}), 500


# ─────────────────────────────────────────────────────────────
# GUARDIAN DOWNLOAD  –  GET /students/guardians/download
# ─────────────────────────────────────────────────────────────
@student_Management.route("/guardians/download", methods=["GET"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def download_guardians():

    claims = get_jwt()
    if claims.get("role") not in {"staff"}:
        return jsonify({"message": "Unauthorized"}), 403

    school_id = claims.get("school_id")

    try:
        students = (
            _guardian_query(school_id)
            .order_by(Student.last_name, Student.first_name)
            .all()
        )

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "Child's Names",
            "Guardian Name",
            "Relationship",
            "Contact",
            "Address",
            "Occupation",
        ])

        for s in students:
            g = Guardian.query.filter_by(student_id=s.id).first()
            writer.writerow([
                f"{s.first_name} {s.last_name}",
                g.name                              if g else "",
                getattr(g, "relationship", "") or "" if g else "",
                g.contact                           if g else "",
                getattr(g, "address", "")    or ""  if g else "",
                getattr(g, "occupation", "") or ""  if g else "",
            ])

        csv_bytes = output.getvalue().encode("utf-8-sig")

        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=guardians.csv",
                "Content-Length": str(len(csv_bytes)),
            }
        )

    except Exception:
        logger.exception("download_guardians failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to generate guardian export. Please try again."}), 500