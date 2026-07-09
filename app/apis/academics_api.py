from flask import Blueprint, request, jsonify, render_template
from flask_jwt_extended import jwt_required, get_jwt
from datetime import datetime
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
import logging

from app.extensions import db, limiter
from app.models.core import School, UserModule
from app.models.people import Staff, Student
from app.models.academic_structure import (
    Class, Stream, Subject, Papers,
    TeacherSubject, TeacherStream,
    StudentStream, StudentSubject,
    TeachAssignment, StudentAttendance, StaffAttendance, LessonSession, AcademicConfig,
)
from app.core.rate_limit import (
    READ_LIMIT, WRITE_LIMIT, SEARCH_LIMIT, MARKS_SAVE_LIMIT,
)

logger = logging.getLogger(__name__)

academics_api = Blueprint(
    "academics_api",
    __name__,
    url_prefix="/api/academics",
)

STAFF_ROLES = {"staff"}
ALL_ROLES   = {"staff", "admin"}

# ═══════════════════════════════════════════════════════════════
#  PAGINATION CONSTANTS
# ═══════════════════════════════════════════════════════════════

DEFAULT_PAGE_SIZE  = 20
MAX_PAGE_SIZE      = 100
CLASSES_PAGE_SIZE  = 10
SUBJECTS_PAGE_SIZE = 10
STUDENTS_PAGE_SIZE = 20


def _paginate(query, page: int, per_page: int):
    """
    Apply SQLAlchemy OFFSET/LIMIT pagination to a query.

    Returns a dict:
        {
            "items":       list of model objects,
            "total":       total matching rows (int),
            "page":        current page (int, 1-based),
            "per_page":    page size (int),
            "total_pages": total number of pages (int),
            "has_prev":    bool,
            "has_next":    bool,
        }
    """
    per_page = max(1, min(per_page, MAX_PAGE_SIZE))
    page     = max(1, page)

    total      = query.count()
    items      = query.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = max(1, -(-total // per_page))  # ceiling division

    return {
        "items":       items,
        "total":       total,
        "page":        page,
        "per_page":    per_page,
        "total_pages": total_pages,
        "has_prev":    page > 1,
        "has_next":    page < total_pages,
    }


def _page_range(page: int, total_pages: int, edge: int = 1, window: int = 2) -> list:
    """
    Build a compact page-number list for pagination controls, using None
    as an ellipsis marker.

    e.g. _page_range(6, 12) -> [1, None, 4, 5, 6, 7, 8, None, 12]
    """
    if total_pages <= (edge * 2) + (window * 2) + 1:
        return list(range(1, total_pages + 1))

    pages  = set(range(1, edge + 1))
    pages |= set(range(total_pages - edge + 1, total_pages + 1))
    pages |= set(range(max(1, page - window), min(total_pages, page + window) + 1))

    ordered = sorted(pages)
    result  = []
    prev    = None
    for p in ordered:
        if prev is not None and p - prev > 1:
            result.append(None)
        result.append(p)
        prev = p
    return result


def _pagination_meta(page_data: dict) -> dict:
    """Return only the metadata portion (no items) for JSON/template responses."""
    meta = {k: v for k, v in page_data.items() if k != "items"}
    meta["page_range"] = _page_range(meta["page"], meta["total_pages"])
    return meta


# ═══════════════════════════════════════════════════════════════
#  ROLE GUARDS
# ═══════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ═══════════════════════════════════════════════════════════════

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


def _subjects_by_level(school_id: int, level: str):
    return (
        Subject.query
        .filter_by(school_id=school_id, level=level)
        .order_by(Subject.name)
        .all()
    )


def _get_levels_with_subjects(school_id: int):
    all_levels = [
        row[0] for row in
        db.session.query(Subject.level)
        .filter(Subject.school_id == school_id, Subject.level.isnot(None))
        .distinct().order_by(Subject.level).all()
    ]
    result = [
        (level, Subject.query.filter_by(school_id=school_id, level=level).order_by(Subject.name).all())
        for level in all_levels
    ]
    no_level = Subject.query.filter(
        Subject.school_id == school_id, Subject.level.is_(None)
    ).order_by(Subject.name).all()
    if no_level:
        result.append(("Other", no_level))
    return result


def _serialize_student(student: Student) -> dict:
    ss_row    = student.stream[0] if student.stream else None
    stream_id = ss_row.stream_id  if ss_row         else None

    stream_name = None
    if stream_id:
        s = Stream.query.get(stream_id)
        stream_name = s.name if s else None

    subject_ids = [ss.subject_id for ss in student.subjects]
    subj_rows   = Subject.query.filter(Subject.id.in_(subject_ids)).all() if subject_ids else []
    subj_lookup = {s.id: s for s in subj_rows}

    subjects = [
        {"id": sid, "name": subj_lookup[sid].name, "level": subj_lookup[sid].level or "other"}
        for sid in subject_ids if sid in subj_lookup
    ]

    return {
        "id":           student.id,
        "student_code": student.student_code,
        "full_name":    f"{student.first_name} {student.last_name}",
        "class_id":     student.class_id,
        "class_name":   student.class_.name if student.class_ else None,
        "stream_id":    stream_id,
        "stream_name":  stream_name,
        "subjects":     subjects,
    }


def _serialize_assignment(a: TeachAssignment) -> dict:
    teacher = Staff.query.get(a.staff_id)
    subject = Subject.query.get(a.subject_id)
    stream  = Stream.query.get(a.stream_id) if a.stream_id else None
    cls     = Class.query.get(stream.class_id) if stream else None

    stream_label = None
    if stream:
        stream_label = f"{cls.name} {stream.name}" if cls else stream.name

    return {
        "id":            a.id,
        "staff_id":      a.staff_id,
        "subject_id":    a.subject_id,
        "stream_id":     a.stream_id,
        "class_id":      cls.id              if cls     else None,
        "teacher_first": teacher.first_name  if teacher else "",
        "teacher_last":  teacher.last_name   if teacher else "",
        "subject_name":  subject.name        if subject else "",
        "stream_name":   stream.name         if stream  else None,
        "stream_label":  stream_label,
    }


# ═══════════════════════════════════════════════════════════════
#  SCHOOL INFO
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/school/info", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def school_info():
    guard = any_role_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    school, err = _school_or_404(school_id)
    if err:
        return err

    return jsonify({
        "id":          school.id,
        "name":        school.name,
        "school_type": school.school_type,
        "address":     getattr(school, "address", None),
        "phone":       getattr(school, "phone",   None),
        "email":       getattr(school, "email",   None),
    }), 200


# ═══════════════════════════════════════════════════════════════
#  CLASSES
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/classes", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def classes_page():
    """
    Renders the classes/streams page with server-side pagination
    (CLASSES_PAGE_SIZE classes per page).
    """
    guard = any_role_required()
    if guard:
        return guard

    claims                      = get_jwt()
    school_id, user_id, modules = _get_context(claims)
    school, err                 = _school_or_404(school_id)
    if err:
        return err

    page = request.args.get("page", default=1, type=int)

    classes_query = Class.query.filter_by(school_id=school_id).order_by(Class.name)
    paged         = _paginate(classes_query, page, CLASSES_PAGE_SIZE)
    classes       = paged["items"]
    class_ids     = [cls.id for cls in classes]

    streams = (
        Stream.query.join(Class)
        .filter(
            Class.school_id == school_id,
            Class.id.in_(class_ids),
            or_(Stream.status != "deleted", Stream.status.is_(None)),
        )
        .all()
    ) if class_ids else []

    staff = Staff.query.filter_by(school_id=school_id, staff_type="teaching").all()

    rows = []
    for cls in classes:
        cls_streams = [s for s in streams if s.class_id == cls.id]
        if cls_streams:
            for stream in cls_streams:
                student_count = StudentStream.query.filter_by(
                    stream_id=stream.id, school_id=school_id
                ).count()
                ts = TeacherStream.query.filter_by(
                    stream_id=stream.id, school_id=school_id
                ).first()
                teacher_name = "—"
                if ts:
                    t = Staff.query.get(ts.teacher_id)
                    if t:
                        teacher_name = f"{t.first_name} {t.last_name}"
                rows.append({
                    "class_id":      cls.id,
                    "class_name":    cls.name,
                    "stream_id":     stream.id,
                    "stream_name":   stream.name,
                    "capacity":      stream.capacity or "—",
                    "teacher":       teacher_name,
                    "student_count": student_count,
                })
        else:
            rows.append({
                "class_id":      cls.id,
                "class_name":    cls.name,
                "stream_id":     None,
                "stream_name":   "No streams",
                "capacity":      "—",
                "teacher":       "—",
                "student_count": 0,
            })

    return render_template(
        "modules/academics/classes.html",
        rows=rows, classes=classes, staff=staff, school=school, modules=modules,
        pagination=_pagination_meta(paged),
    )


@academics_api.route("/classes", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def create_class():
    guard = any_role_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    data        = request.get_json(force=True) or {}
    class_name  = str(data.get("class_name",  "")).strip()
    stream_name = str(data.get("stream_name", "")).strip()
    capacity    = data.get("capacity")
    teacher_id  = data.get("teacher_id")

    if not class_name:
        return jsonify({"message": "class_name is required"}), 400

    if capacity is not None and capacity != "":
        try:
            capacity = int(capacity)
            if capacity <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"message": "Capacity must be a positive whole number"}), 400
    else:
        capacity = None

    try:
        cls = Class.query.filter_by(name=class_name, school_id=school_id).first()
        if not cls:
            cls = Class(name=class_name, school_id=school_id)
            db.session.add(cls)
            db.session.flush()

        stream = None
        if stream_name:
            existing = Stream.query.filter_by(name=stream_name, class_id=cls.id).first()
            if existing:
                return jsonify({"message": f"Stream '{stream_name}' already exists in {class_name}"}), 400
            stream = Stream(name=stream_name, class_id=cls.id, capacity=capacity)
            db.session.add(stream)
            db.session.flush()

            if teacher_id:
                teacher = Staff.query.filter_by(
                    id=int(teacher_id), school_id=school_id, staff_type="teaching"
                ).first()
                if not teacher:
                    return jsonify({"message": "Teacher not found"}), 404
                if not TeacherStream.query.filter_by(
                    teacher_id=teacher.id, stream_id=stream.id, school_id=school_id
                ).first():
                    db.session.add(TeacherStream(
                        teacher_id=teacher.id, stream_id=stream.id, school_id=school_id,
                    ))

        db.session.commit()
        return jsonify({
            "message":   "Created successfully",
            "class_id":  cls.id,
            "stream_id": stream.id if stream else None,
        }), 201

    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": "Duplicate entry — check class/stream names"}), 400
    except Exception:
        db.session.rollback()
        logger.exception("create_class failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to create class. Please try again."}), 500


@academics_api.route("/classes/<int:class_id>", methods=["PUT"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def update_class(class_id):
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    cls       = Class.query.filter_by(id=class_id, school_id=school_id).first()
    if not cls:
        return jsonify({"message": "Class not found"}), 404

    data       = request.get_json(force=True) or {}
    class_name = str(data.get("class_name", "")).strip()
    if not class_name:
        return jsonify({"message": "class_name is required"}), 400

    try:
        cls.name = class_name
        db.session.commit()
        return jsonify({"message": "Class updated"}), 200
    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": "A class with that name already exists"}), 400
    except Exception:
        db.session.rollback()
        logger.exception("update_class failed | class_id=%s", class_id)
        return jsonify({"message": "Failed to update class. Please try again."}), 500


@academics_api.route("/classes/<int:class_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_class(class_id):
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    cls       = Class.query.filter_by(id=class_id, school_id=school_id).first()
    if not cls:
        return jsonify({"message": "Class not found"}), 404

    try:
        try:
            from app.models.finance import FeeStructure
            fee_count = FeeStructure.query.filter_by(class_id=cls.id).count()
            if fee_count:
                return jsonify({"message": f"Cannot delete '{cls.name}' — {fee_count} fee structure(s) are linked. Remove them first."}), 400
        except ImportError:
            pass

        streams = Stream.query.filter_by(class_id=cls.id).all()
        for stream in streams:
            enrolled = StudentStream.query.filter_by(stream_id=stream.id).count()
            if enrolled:
                return jsonify({"message": f"Cannot delete — {enrolled} student(s) are in stream '{stream.name}'"}), 400

        for stream in streams:
            TeacherStream.query.filter_by(stream_id=stream.id).delete()
            TeachAssignment.query.filter_by(stream_id=stream.id).delete()
            StudentStream.query.filter_by(stream_id=stream.id).delete()
            db.session.delete(stream)

        db.session.delete(cls)
        db.session.commit()
        return jsonify({"message": "Class deleted"}), 200

    except Exception:
        db.session.rollback()
        logger.exception("delete_class failed | class_id=%s", class_id)
        return jsonify({"message": "Failed to delete class. Please try again."}), 500


@academics_api.route("/classes/list", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def list_classes_json():
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    classes   = Class.query.filter_by(school_id=school_id).all()
    result    = []
    for cls in classes:
        streams = (
            Stream.query.join(Class)
            .filter(
                Stream.class_id == cls.id,
                Class.school_id == school_id,
                or_(Stream.status != "deleted", Stream.status.is_(None)),
            )
            .all()
        )
        result.append({
            "id":      cls.id,
            "name":    cls.name,
            "streams": [{"id": s.id, "name": s.name, "capacity": s.capacity} for s in streams],
        })
    return jsonify(result), 200


# ═══════════════════════════════════════════════════════════════
#  STREAMS
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/streams/<int:stream_id>", methods=["PUT"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def update_stream(stream_id):
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    stream    = (
        Stream.query.join(Class)
        .filter(Stream.id == stream_id, Class.school_id == school_id)
        .first()
    )
    if not stream:
        return jsonify({"message": "Stream not found"}), 404

    data        = request.get_json(force=True) or {}
    stream_name = str(data.get("stream_name", "")).strip()
    capacity    = data.get("capacity")
    teacher_id  = data.get("teacher_id")

    if capacity is not None and capacity not in ("", 0):
        try:
            capacity = int(capacity)
            if capacity <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"message": "Capacity must be a positive whole number"}), 400
    elif capacity in ("", 0):
        capacity = None

    try:
        if stream_name:
            stream.name = stream_name
        if capacity is not None or data.get("capacity") in ("", 0, None):
            stream.capacity = capacity

        if teacher_id is not None:
            TeacherStream.query.filter_by(stream_id=stream.id, school_id=school_id).delete()
            if teacher_id:
                teacher = Staff.query.filter_by(
                    id=int(teacher_id), school_id=school_id, staff_type="teaching"
                ).first()
                if not teacher:
                    return jsonify({"message": "Teacher not found"}), 404
                db.session.add(TeacherStream(
                    teacher_id=teacher.id, stream_id=stream.id, school_id=school_id,
                ))

        db.session.commit()
        return jsonify({"message": "Stream updated"}), 200

    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": "Duplicate stream name in this class"}), 400
    except Exception:
        db.session.rollback()
        logger.exception("update_stream failed | stream_id=%s", stream_id)
        return jsonify({"message": "Failed to update stream. Please try again."}), 500


@academics_api.route("/streams/<int:stream_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_stream(stream_id):
    guard = any_role_required()
    if guard:
        return guard

    class_id  = request.args.get("class_id", type=int)
    school_id = get_jwt().get("school_id")

    stream = (
        Stream.query.join(Class)
        .filter(Stream.id == stream_id, Class.school_id == school_id)
        .first()
    )
    if not stream:
        return jsonify({"message": "Stream not found"}), 404

    enrolled = StudentStream.query.filter_by(stream_id=stream.id).count()
    if enrolled:
        return jsonify({"message": f"Cannot delete — {enrolled} student(s) are enrolled in this stream"}), 400

    try:
        from app.models.finance import FeeStructure
        cls     = Class.query.filter_by(id=class_id, school_id=school_id).first()
        fee_ref = FeeStructure.query.filter_by(class_id=cls.id).count() if cls else 0

        if fee_ref:
            stream.status = "deleted"

        TeacherStream.query.filter_by(stream_id=stream.id).delete()
        TeachAssignment.query.filter_by(stream_id=stream.id).delete()
        if not fee_ref:
            db.session.delete(stream)

        db.session.commit()
        return jsonify({"message": "Stream deleted"}), 200

    except Exception:
        db.session.rollback()
        logger.exception("delete_stream failed | stream_id=%s", stream_id)
        return jsonify({"message": "Failed to delete stream. Please try again."}), 500


@academics_api.route("/streams/<int:stream_id>/detail", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def stream_detail(stream_id):
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    stream    = (
        Stream.query.join(Class)
        .filter(Stream.id == stream_id, Class.school_id == school_id)
        .first()
    )
    if not stream:
        return jsonify({"message": "Not found"}), 404

    ts           = TeacherStream.query.filter_by(stream_id=stream.id, school_id=school_id).first()
    teacher_id   = ts.teacher_id if ts else None
    teacher_name = None
    if teacher_id:
        t = Staff.query.get(teacher_id)
        if t:
            teacher_name = f"{t.first_name} {t.last_name}"

    return jsonify({
        "id":           stream.id,
        "name":         stream.name,
        "capacity":     stream.capacity,
        "class_id":     stream.class_id,
        "teacher_id":   teacher_id,
        "teacher_name": teacher_name,
    }), 200


# ═══════════════════════════════════════════════════════════════
#  SUBJECTS
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/subjects", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def subjects_page():
    """
    Renders the subjects page with server-side pagination
    (SUBJECTS_PAGE_SIZE subjects per page).
    """
    guard = any_role_required()
    if guard:
        return guard

    claims                      = get_jwt()
    school_id, user_id, modules = _get_context(claims)
    school, err                 = _school_or_404(school_id)
    if err:
        return err

    page = request.args.get("page", default=1, type=int)

    subjects_query = Subject.query.filter_by(school_id=school_id).order_by(Subject.name)
    paged          = _paginate(subjects_query, page, SUBJECTS_PAGE_SIZE)
    subjects       = paged["items"]
    staff          = Staff.query.filter_by(school_id=school_id, staff_type="teaching").all()

    subject_data = []
    for subj in subjects:
        papers = Papers.query.filter_by(subject_id=subj.id, school_id=school_id).all()
        teacher_links = TeacherSubject.query.filter_by(
            subject_id=subj.id, school_id=school_id
        ).all()
        teachers = []
        for tl in teacher_links:
            t = Staff.query.get(tl.teacher_id)
            if t:
                teachers.append({"id": t.id, "name": f"{t.first_name} {t.last_name}"})
        subject_data.append({"subject": subj, "papers": papers, "teachers": teachers})

    is_secondary = school.school_type.lower() in ("secondary", "college", "high school")

    return render_template(
        "modules/academics/subjects.html",
        subject_data=subject_data, staff=staff, school=school,
        modules=modules, is_secondary=is_secondary,
        pagination=_pagination_meta(paged),
    )


@academics_api.route("/subjects", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def create_subject():
    guard = any_role_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    data        = request.get_json(force=True) or {}
    name        = str(data.get("name",        "")).strip()
    description = str(data.get("description", "")).strip()
    level       = str(data.get("level",       "")).strip()
    teacher_ids = data.get("teacher_ids", [])
    papers_data = data.get("papers",      [])

    if not name:
        return jsonify({"message": "Subject name is required"}), 400
    if not level:
        return jsonify({"message": "Level is required"}), 400

    for p in papers_data:
        p_name  = str(p.get("paper_name", "")).strip()
        p_marks = p.get("max_marks")
        if not p_name:
            return jsonify({"message": "Each paper must have a name"}), 400
        if p_marks is None or p_marks == "":
            return jsonify({"message": f"Max score is required for {p_name}"}), 400
        try:
            if int(p_marks) <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return jsonify({"message": f"Max score for {p_name} must be a positive number"}), 400

    # [FIX] Uniqueness must be scoped to (school_id, name, level) — not
    # just (school_id, name). The same subject name (e.g. "English") is
    # legitimately a different Subject row per level (O-Level, A-Level,
    # Lower Primary, etc.), so the old name-only check incorrectly
    # blocked creating "English" at a second level.
    if Subject.query.filter_by(name=name, school_id=school_id, level=level).first():
        return jsonify({"message": f"Subject '{name}' already exists for level '{level}'"}), 400

    try:
        subject = Subject(school_id=school_id, name=name, description=description, level=level)
        db.session.add(subject)
        db.session.flush()

        for tid in teacher_ids:
            teacher = Staff.query.filter_by(
                id=int(tid), school_id=school_id, staff_type="teaching"
            ).first()
            if teacher and not TeacherSubject.query.filter_by(
                teacher_id=teacher.id, subject_id=subject.id, school_id=school_id
            ).first():
                db.session.add(TeacherSubject(
                    teacher_id=teacher.id, subject_id=subject.id, school_id=school_id,
                ))

        for p in papers_data:
            p_name  = str(p.get("paper_name", "")).strip()
            p_marks = int(p.get("max_marks"))
            if p_name:
                db.session.add(Papers(
                    school_id=school_id, subject_id=subject.id,
                    paper_name=p_name, max_marks=p_marks,
                ))

        db.session.commit()
        return jsonify({"message": "Subject created", "subject_id": subject.id}), 201

    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": "Duplicate subject"}), 400
    except Exception:
        db.session.rollback()
        logger.exception("create_subject failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to create subject. Please try again."}), 500


@academics_api.route("/subjects/<int:subject_id>", methods=["PUT"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def update_subject(subject_id):
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    subject   = Subject.query.filter_by(id=subject_id, school_id=school_id).first()
    if not subject:
        return jsonify({"message": "Subject not found"}), 404

    data        = request.get_json(force=True) or {}
    name        = str(data.get("name",        subject.name)).strip()
    description = str(data.get("description", subject.description or "")).strip()
    level       = str(data.get("level",       subject.level or "")).strip()
    teacher_ids = data.get("teacher_ids", None)
    papers_data = data.get("papers",      None)

    if not name:
        return jsonify({"message": "Subject name is required"}), 400
    if not level:
        return jsonify({"message": "Level is required"}), 400

    # [FIX] Same (school_id, name, level) scoping as create_subject,
    # excluding this subject's own row, so renaming/relevel-ing a
    # subject doesn't collide with itself but still catches a genuine
    # duplicate at the same level.
    dupe = Subject.query.filter(
        Subject.school_id == school_id,
        Subject.name       == name,
        Subject.level      == level,
        Subject.id         != subject_id,
    ).first()
    if dupe:
        return jsonify({"message": f"Subject '{name}' already exists for level '{level}'"}), 400

    if papers_data is not None:
        for p in papers_data:
            p_name  = str(p.get("paper_name", "")).strip()
            p_marks = p.get("max_marks")
            if not p_name:
                return jsonify({"message": "Each paper must have a name"}), 400
            if p_marks is None or p_marks == "":
                return jsonify({"message": f"Max score required for {p_name}"}), 400
            try:
                if int(p_marks) <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return jsonify({"message": f"Max score for {p_name} must be a positive number"}), 400

    try:
        subject.name        = name
        subject.description = description
        subject.level       = level

        if teacher_ids is not None:
            TeacherSubject.query.filter_by(subject_id=subject.id, school_id=school_id).delete()
            for tid in teacher_ids:
                teacher = Staff.query.filter_by(
                    id=int(tid), school_id=school_id, staff_type="teaching"
                ).first()
                if teacher:
                    db.session.add(TeacherSubject(
                        teacher_id=teacher.id, subject_id=subject.id, school_id=school_id,
                    ))

        if papers_data is not None:
            Papers.query.filter_by(subject_id=subject.id, school_id=school_id).delete()
            for p in papers_data:
                p_name  = str(p.get("paper_name", "")).strip()
                p_marks = int(p.get("max_marks"))
                if p_name:
                    db.session.add(Papers(
                        school_id=school_id, subject_id=subject.id,
                        paper_name=p_name, max_marks=p_marks,
                    ))

        db.session.commit()
        return jsonify({"message": "Subject updated"}), 200

    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": "A subject with that name already exists for this level"}), 400
    except Exception:
        db.session.rollback()
        logger.exception("update_subject failed | subject_id=%s", subject_id)
        return jsonify({"message": "Failed to update subject. Please try again."}), 500


@academics_api.route("/subjects/<int:subject_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_subject(subject_id):
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    subject   = Subject.query.filter_by(id=subject_id, school_id=school_id).first()
    if not subject:
        return jsonify({"message": "Subject not found"}), 404

    assignments = TeachAssignment.query.filter_by(
        subject_id=subject_id, school_id=school_id
    ).count()
    if assignments:
        return jsonify({"message": f"Cannot delete — {assignments} teaching assignment(s) reference this subject"}), 400

    try:
        StudentSubject.query.filter_by(subject_id=subject.id, school_id=school_id).delete()
        TeacherSubject.query.filter_by(subject_id=subject.id, school_id=school_id).delete()
        Papers.query.filter_by(subject_id=subject.id, school_id=school_id).delete()
        db.session.delete(subject)
        db.session.commit()
        return jsonify({"message": "Subject deleted"}), 200

    except Exception:
        db.session.rollback()
        logger.exception("delete_subject failed | subject_id=%s", subject_id)
        return jsonify({"message": "Failed to delete subject. Please try again."}), 500


@academics_api.route("/subjects/list", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def list_subjects_json():
    guard = any_role_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    school, err = _school_or_404(school_id)
    if err:
        return err

    subjects = Subject.query.filter_by(school_id=school_id).all()
    result   = []
    for subj in subjects:
        papers = Papers.query.filter_by(subject_id=subj.id, school_id=school_id).all()
        tlinks = TeacherSubject.query.filter_by(subject_id=subj.id, school_id=school_id).all()
        teachers = []
        for tl in tlinks:
            t = Staff.query.get(tl.teacher_id)
            if t:
                teachers.append({"id": t.id, "name": f"{t.first_name} {t.last_name}"})
        result.append({
            "id":          subj.id,
            "name":        subj.name,
            "description": subj.description,
            "level":       subj.level,
            "teachers":    teachers,
            "papers":      [{"paper_name": p.paper_name, "max_marks": p.max_marks} for p in papers],
        })
    return jsonify(result), 200


@academics_api.route("/subjects/<int:subject_id>/detail", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def subject_detail(subject_id):
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    subject   = Subject.query.filter_by(id=subject_id, school_id=school_id).first()
    if not subject:
        return jsonify({"message": "Not found"}), 404

    papers = Papers.query.filter_by(subject_id=subject.id, school_id=school_id).all()
    tlinks = TeacherSubject.query.filter_by(subject_id=subject.id, school_id=school_id).all()

    return jsonify({
        "id":          subject.id,
        "name":        subject.name,
        "description": subject.description,
        "level":       subject.level,
        "teacher_ids": [tl.teacher_id for tl in tlinks],
        "papers":      [{"paper_name": p.paper_name, "max_marks": p.max_marks} for p in papers],
    }), 200


# ═══════════════════════════════════════════════════════════════
#  STUDENTS
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/students", methods=["GET"])
@jwt_required()
@limiter.limit(SEARCH_LIMIT)
def student_lists_page():
    """
    Renders the student list page with server-side pagination
    (STUDENTS_PAGE_SIZE students per page).

    Query params:
        search     – name / code filter (optional)
        class_id   – filter by class (optional)
        subject_id – filter by enrolled subject (optional)
        page       – 1-based page number (default 1)
    """
    guard = any_role_required()
    if guard:
        return guard

    claims                      = get_jwt()
    school_id, user_id, modules = _get_context(claims)
    school, err                 = _school_or_404(school_id)
    if err:
        return err

    search     = request.args.get("search",     "").strip()
    class_id   = request.args.get("class_id",   type=int)
    subject_id = request.args.get("subject_id", type=int)
    page       = request.args.get("page", default=1, type=int)

    q = Student.query.filter_by(school_id=school_id)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            Student.first_name.ilike(like),
            Student.last_name.ilike(like),
            Student.admission_number.ilike(like),
            Student.student_code.ilike(like),
        ))
    if class_id:
        q = q.filter(Student.class_id == class_id)
    if subject_id:
        q = q.filter(Student.subjects.any(StudentSubject.subject_id == subject_id))

    q = q.order_by(Student.first_name, Student.last_name)

    paged    = _paginate(q, page, STUDENTS_PAGE_SIZE)
    students = paged["items"]

    classes  = Class.query.filter_by(school_id=school_id).order_by(Class.name).all()
    subjects = Subject.query.filter_by(school_id=school_id).order_by(Subject.name).all()

    streams  = (
        Stream.query.join(Class)
        .filter(
            Class.school_id == school_id,
            or_(Stream.status != "deleted", Stream.status.is_(None)),
        )
        .order_by(Class.name, Stream.name)
        .all()
    )

    is_secondary         = school.school_type.lower() in ("secondary", "college", "high school")
    stream_map           = {s.id: s.name     for s in streams}
    stream_class_map     = {s.id: s.class_id for s in streams}
    all_subjects         = Subject.query.filter_by(school_id=school_id).all()
    subject_map          = {s.id: {"name": s.name, "level": s.level or "other"} for s in all_subjects}
    levels_with_subjects = _get_levels_with_subjects(school_id)
    total_students        = Student.query.filter_by(school_id=school_id).count()

    # Only pass filters that are actually set, so pagination links stay clean
    filter_args = {}
    if search:     filter_args["search"]     = search
    if class_id:   filter_args["class_id"]   = class_id
    if subject_id: filter_args["subject_id"] = subject_id

    return render_template(
        "modules/academics/student_lists.html",
        students             = students,
        classes              = classes,
        subjects             = subjects,
        streams              = streams,
        school               = school,
        modules              = modules,
        is_secondary         = is_secondary,
        stream_map           = stream_map,
        stream_class_map     = stream_class_map,
        subject_map          = subject_map,
        levels_with_subjects = levels_with_subjects,
        pagination           = _pagination_meta(paged),
        total_students       = total_students,
        filter_args          = filter_args,
    )


@academics_api.route("/students/list", methods=["GET"])
@jwt_required()
@limiter.limit(SEARCH_LIMIT)
def list_students_json():
    """
    JSON endpoint for student data with server-side pagination.

    Query params:
        search     – name / code filter (optional)
        class_id   – filter by class (optional)
        subject_id – filter by enrolled subject (optional)
        page       – 1-based page number (default 1)
        per_page   – rows per page (default 20, max 100)

    Response:
        {
            "students":    [...],
            "total":       int,
            "page":        int,
            "per_page":    int,
            "total_pages": int,
            "has_prev":    bool,
            "has_next":    bool
        }
    """
    guard = any_role_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")

    search     = request.args.get("search",     "").strip()
    class_id   = request.args.get("class_id",   type=int)
    subject_id = request.args.get("subject_id", type=int)
    page       = request.args.get("page",       default=1,              type=int)
    per_page   = request.args.get("per_page",   default=DEFAULT_PAGE_SIZE, type=int)

    q = Student.query.filter_by(school_id=school_id)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            Student.first_name.ilike(like),
            Student.last_name.ilike(like),
            Student.admission_number.ilike(like),
            Student.student_code.ilike(like),
        ))
    if class_id:
        q = q.filter(Student.class_id == class_id)
    if subject_id:
        q = q.filter(Student.subjects.any(StudentSubject.subject_id == subject_id))

    q = q.order_by(Student.first_name, Student.last_name)

    paged    = _paginate(q, page, per_page)
    students = [_serialize_student(s) for s in paged["items"]]

    return jsonify({
        "students":    students,
        **_pagination_meta(paged),
    }), 200


@academics_api.route("/students/<int:student_id>", methods=["PUT"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def update_student(student_id: int):
    guard = any_role_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")

    student = Student.query.filter_by(id=student_id, school_id=school_id).first()
    if not student:
        return jsonify({"message": "Student not found"}), 404

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"message": "Invalid or missing JSON body"}), 400

    stream_id   = payload.get("stream_id")
    subject_ids = payload.get("subject_ids", [])

    if not isinstance(subject_ids, list):
        return jsonify({"message": "subject_ids must be a list of integers"}), 400

    if stream_id is not None:
        stream = (
            Stream.query.join(Class)
            .filter(
                Stream.id == stream_id,
                Class.school_id == school_id,
                or_(Stream.status != "deleted", Stream.status.is_(None)),
            )
            .first()
        )
        if not stream:
            return jsonify({"message": "Stream not found"}), 404

        if stream.class_id != student.class_id:
            correct_class = Class.query.get(student.class_id)
            stream_class  = Class.query.get(stream.class_id)
            return jsonify({
                "message": (
                    f"Stream '{stream.name}' belongs to "
                    f"{stream_class.name if stream_class else 'another class'}, "
                    f"but this student is in "
                    f"{correct_class.name if correct_class else 'a different class'}. "
                    "Please select a stream from the correct class."
                )
            }), 400

    if subject_ids:
        valid_subjects = Subject.query.filter(
            Subject.id.in_(subject_ids),
            Subject.school_id == school_id,
        ).all()
        if len(valid_subjects) != len(set(subject_ids)):
            found_ids = {s.id for s in valid_subjects}
            bad_ids   = [sid for sid in subject_ids if sid not in found_ids]
            return jsonify({"message": f"Unknown subject ids: {bad_ids}"}), 404
    else:
        valid_subjects = []

    try:
        StudentStream.query.filter_by(
            student_id=student.id, school_id=school_id
        ).delete(synchronize_session="fetch")

        if stream_id is not None:
            db.session.add(StudentStream(
                school_id=school_id, student_id=student.id, stream_id=stream_id,
            ))

        StudentSubject.query.filter_by(
            student_id=student.id, school_id=school_id
        ).delete(synchronize_session="fetch")

        for subj in valid_subjects:
            db.session.add(StudentSubject(
                school_id=school_id, student_id=student.id, subject_id=subj.id,
            ))

        db.session.commit()
        student = Student.query.get(student_id)
        return jsonify({"student": _serialize_student(student)}), 200

    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": "Integrity error — please check your data and try again."}), 400
    except Exception:
        db.session.rollback()
        logger.exception("update_student (academics) failed | student_id=%s", student_id)
        return jsonify({"message": "Failed to update student. Please try again."}), 500


@academics_api.route("/students/<int:student_id>", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_student(student_id: int):
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    student   = Student.query.filter_by(id=student_id, school_id=school_id).first()
    if not student:
        return jsonify({"message": "Student not found"}), 404

    return jsonify({"student": _serialize_student(student)}), 200


# ═══════════════════════════════════════════════════════════════
#  TEACHING ASSIGNMENTS
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/assignments", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def assignments_page():
    """
    Renders the teaching assignments page.

    All assignments are passed to the template; client-side pagination
    (20 per page) is handled entirely in JavaScript.
    """
    guard = any_role_required()
    if guard:
        return guard

    claims                      = get_jwt()
    school_id, user_id, modules = _get_context(claims)
    school, err                 = _school_or_404(school_id)
    if err:
        return err

    raw_assignments = TeachAssignment.query.filter_by(school_id=school_id).all()
    assignments     = [_serialize_assignment(a) for a in raw_assignments]

    staff    = Staff.query.filter_by(school_id=school_id, staff_type="teaching").order_by(Staff.first_name).all()
    classes  = Class.query.filter_by(school_id=school_id).order_by(Class.name).all()
    subjects = Subject.query.filter_by(school_id=school_id).order_by(Subject.name).all()
    streams  = (
        Stream.query.join(Class)
        .filter(
            Class.school_id == school_id,
            or_(Stream.status != "deleted", Stream.status.is_(None)),
        )
        .order_by(Class.name, Stream.name)
        .all()
    )

    teacher_subject_links = TeacherSubject.query.filter_by(school_id=school_id).all()
    teacher_subject_map   = {}
    subject_teacher_map   = {}
    for link in teacher_subject_links:
        teacher_subject_map.setdefault(link.teacher_id, []).append(link.subject_id)
        subject_teacher_map.setdefault(link.subject_id, []).append(link.teacher_id)

    return render_template(
        "modules/academics/teaching_assignments.html",
        assignments          = assignments,
        staff                = staff,
        classes              = classes,
        subjects             = subjects,
        streams              = streams,
        school               = school,
        modules              = modules,
        levels_with_subjects = _get_levels_with_subjects(school_id),
        teacher_subject_map  = teacher_subject_map,
        subject_teacher_map  = subject_teacher_map,
    )


@academics_api.route("/assignments/list", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def list_assignments_json():
    """
    JSON endpoint for assignment data with optional server-side pagination.

    Query params:
        stream_id  – filter (optional)
        staff_id   – filter (optional)
        subject_id – filter (optional)
        page       – 1-based page number (default 1)
        per_page   – rows per page (default 20, max 100)

    If `page` is not supplied the response is the original flat list for
    backwards compatibility. If `page` is supplied the response is wrapped
    in a pagination envelope.
    """
    guard = any_role_required()
    if guard:
        return guard

    school_id  = get_jwt().get("school_id")
    q          = TeachAssignment.query.filter_by(school_id=school_id)
    stream_id  = request.args.get("stream_id",  type=int)
    staff_id   = request.args.get("staff_id",   type=int)
    subject_id = request.args.get("subject_id", type=int)
    page       = request.args.get("page",       type=int)          # None → no pagination
    per_page   = request.args.get("per_page",   default=DEFAULT_PAGE_SIZE, type=int)

    if stream_id:  q = q.filter_by(stream_id=stream_id)
    if staff_id:   q = q.filter_by(staff_id=staff_id)
    if subject_id: q = q.filter_by(subject_id=subject_id)

    if page is not None:
        paged       = _paginate(q, page, per_page)
        assignments = [_serialize_assignment(a) for a in paged["items"]]
        return jsonify({
            "assignments": assignments,
            **_pagination_meta(paged),
        }), 200

    # Legacy flat list (no page param)
    return jsonify([_serialize_assignment(a) for a in q.all()]), 200


@academics_api.route("/assignments", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def create_assignment():
    guard = any_role_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    payload   = request.get_json(silent=True) or {}
    staff_id   = payload.get("staff_id")
    subject_id = payload.get("subject_id")
    stream_id  = payload.get("stream_id")

    if not staff_id or not subject_id:
        return jsonify({"message": "staff_id and subject_id are required"}), 400

    teacher = Staff.query.filter_by(id=staff_id, school_id=school_id, staff_type="teaching").first()
    if not teacher:
        return jsonify({"message": "Teacher not found"}), 404

    subject = Subject.query.filter_by(id=subject_id, school_id=school_id).first()
    if not subject:
        return jsonify({"message": "Subject not found"}), 404

    if stream_id:
        stream = (
            Stream.query.join(Class)
            .filter(
                Stream.id == stream_id,
                Class.school_id == school_id,
                or_(Stream.status != "deleted", Stream.status.is_(None)),
            )
            .first()
        )
        if not stream:
            return jsonify({"message": "Stream not found"}), 404
    else:
        stream_id = None

    teacher_link = TeacherSubject.query.filter_by(
        school_id=school_id, teacher_id=staff_id, subject_id=subject_id,
    ).first()
    if not teacher_link:
        return jsonify({
            "message": (
                f"{teacher.first_name} {teacher.last_name} is not assigned to teach "
                f"'{subject.name}'. Go to Academics → Subjects to assign them first."
            )
        }), 400

    exists = TeachAssignment.query.filter_by(
        school_id=school_id, staff_id=staff_id,
        subject_id=subject_id, stream_id=stream_id,
    ).first()
    if exists:
        return jsonify({"message": "This assignment already exists"}), 400

    try:
        assignment = TeachAssignment(
            school_id=school_id, staff_id=staff_id,
            subject_id=subject_id, stream_id=stream_id,
        )
        db.session.add(assignment)
        db.session.commit()
        return jsonify({"message": "Assignment created", "assignment": _serialize_assignment(assignment)}), 201

    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": "Duplicate assignment"}), 400
    except Exception:
        db.session.rollback()
        logger.exception("create_assignment failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to create assignment. Please try again."}), 500


@academics_api.route("/assignments/<int:assignment_id>", methods=["PUT"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def update_assignment(assignment_id: int):
    guard = any_role_required()
    if guard:
        return guard

    school_id  = get_jwt().get("school_id")
    assignment = TeachAssignment.query.filter_by(id=assignment_id, school_id=school_id).first()
    if not assignment:
        return jsonify({"message": "Assignment not found"}), 404

    payload    = request.get_json(silent=True) or {}
    staff_id   = payload.get("staff_id",   assignment.staff_id)
    subject_id = payload.get("subject_id", assignment.subject_id)
    stream_id  = payload.get("stream_id",  assignment.stream_id)

    teacher = Staff.query.filter_by(id=staff_id, school_id=school_id, staff_type="teaching").first()
    if not teacher:
        return jsonify({"message": "Teacher not found"}), 404

    subject = Subject.query.filter_by(id=subject_id, school_id=school_id).first()
    if not subject:
        return jsonify({"message": "Subject not found"}), 404

    if stream_id:
        stream = (
            Stream.query.join(Class)
            .filter(
                Stream.id == stream_id,
                Class.school_id == school_id,
                or_(Stream.status != "deleted", Stream.status.is_(None)),
            )
            .first()
        )
        if not stream:
            return jsonify({"message": "Stream not found"}), 404
    else:
        stream_id = None

    teacher_link = TeacherSubject.query.filter_by(
        school_id=school_id, teacher_id=staff_id, subject_id=subject_id,
    ).first()
    if not teacher_link:
        return jsonify({
            "message": (
                f"{teacher.first_name} {teacher.last_name} is not assigned to teach "
                f"'{subject.name}'. Go to Academics → Subjects to assign them first."
            )
        }), 400

    dupe = TeachAssignment.query.filter(
        TeachAssignment.school_id  == school_id,
        TeachAssignment.staff_id   == staff_id,
        TeachAssignment.subject_id == subject_id,
        TeachAssignment.stream_id  == stream_id,
        TeachAssignment.id         != assignment_id,
    ).first()
    if dupe:
        return jsonify({"message": "An identical assignment already exists"}), 400

    try:
        assignment.staff_id   = staff_id
        assignment.subject_id = subject_id
        assignment.stream_id  = stream_id
        db.session.commit()
        return jsonify({"message": "Assignment updated", "assignment": _serialize_assignment(assignment)}), 200

    except IntegrityError:
        db.session.rollback()
        return jsonify({"message": "Duplicate assignment"}), 400
    except Exception:
        db.session.rollback()
        logger.exception("update_assignment failed | assignment_id=%s", assignment_id)
        return jsonify({"message": "Failed to update assignment. Please try again."}), 500


@academics_api.route("/assignments/<int:assignment_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_assignment(assignment_id: int):
    guard = any_role_required()
    if guard:
        return guard

    school_id  = get_jwt().get("school_id")
    assignment = TeachAssignment.query.filter_by(id=assignment_id, school_id=school_id).first()
    if not assignment:
        return jsonify({"message": "Assignment not found"}), 404

    from app.models.academic_structure import LessonSession
    lesson_count = LessonSession.query.filter_by(assignment_id=assignment_id).count()
    if lesson_count:
        return jsonify({"message": f"Cannot delete — {lesson_count} lesson session(s) are linked. Archive them first."}), 400

    try:
        from app.models.academic_structure import Assessment
        Assessment.query.filter_by(assignment_id=assignment_id).delete()
        db.session.delete(assignment)
        db.session.commit()
        return jsonify({"message": "Assignment deleted"}), 200

    except Exception:
        db.session.rollback()
        logger.exception("delete_assignment failed | assignment_id=%s", assignment_id)
        return jsonify({"message": "Failed to delete assignment. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  STUDENT ATTENDANCE PAGE
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/student", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def student_attendance_page():
    guard = any_role_required()
    if guard:
        return guard

    claims = get_jwt()
    school_id, user_id, modules = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    classes = (
        Class.query
        .filter_by(school_id=school_id)
        .order_by(Class.name.asc())
        .all()
    )

    subjects = (
        Subject.query
        .filter_by(school_id=school_id)
        .order_by(Subject.name.asc())
        .all()
    )

    return render_template(
        "modules/academics/student_attendance.html",
        school=school,
        modules=modules,
        classes=classes,
        subjects=subjects,
        levels_with_subjects=_get_levels_with_subjects(school_id),
    )


# ═══════════════════════════════════════════════════════════════
#  STUDENT ATTENDANCE FILTER
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/student-attendance/filter", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def filter_student_attendance():
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")

    class_id   = request.args.get("class_id", type=int)
    subject_id = request.args.get("subject_id", type=int)
    date_str   = request.args.get("date")

    try:
        query = (
            db.session.query(
                Student.id.label("student_id"),
                Student.student_code,
                Student.first_name,
                Student.last_name,
                StudentAttendance.status.label("status"),
                LessonSession.date.label("attendance_date"),
                Class.name.label("class_name"),
                Stream.name.label("stream_name"),
                Subject.name.label("subject_name"),
                TeachAssignment.id.label("assignment_id")
            )
            .join(StudentAttendance, StudentAttendance.student_id == Student.id)
            .join(LessonSession, LessonSession.id == StudentAttendance.lesson_id)
            .join(TeachAssignment, TeachAssignment.id == LessonSession.assignment_id)
            .join(Stream, Stream.id == TeachAssignment.stream_id)
            .join(Class, Class.id == Stream.class_id)
            .join(Subject, Subject.id == TeachAssignment.subject_id)
            .filter(Student.school_id == school_id)
        )

        if class_id:
            query = query.filter(Class.id == class_id)

        if subject_id:
            query = query.filter(Subject.id == subject_id)

        if date_str:
            try:
                attendance_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                query = query.filter(LessonSession.date == attendance_date)
            except ValueError:
                return jsonify({"message": "Invalid date format"}), 400

        records = query.order_by(
            LessonSession.date.desc(),
            Student.first_name.asc()
        ).all()

        result = []
        for r in records:
            result.append({
                "student_id":    r.student_id,
                "student_code":  r.student_code,
                "first_name":    r.first_name,
                "last_name":     r.last_name,
                "status":        r.status,
                "date":          r.attendance_date.strftime("%Y-%m-%d") if r.attendance_date else None,
                "class_name":    r.class_name,
                "stream_name":   r.stream_name,
                "subject_name":  r.subject_name,
                "assignment_id": r.assignment_id,
            })

        return jsonify(result), 200

    except Exception:
        logger.exception("filter_student_attendance failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to load attendance records."}), 500


# ═══════════════════════════════════════════════════════════════
#  STAFF ATTENDANCE PAGE
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/staff-attendance", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def staff_attendance_page():
    guard = any_role_required()
    if guard:
        return guard

    claims = get_jwt()
    school_id, user_id, modules = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    staff_members = (
        Staff.query
        .filter_by(school_id=school_id, staff_type="teaching")
        .order_by(Staff.first_name.asc())
        .all()
    )

    return render_template(
        "modules/academics/staffAttendance.html",
        staff_members=staff_members,
        school=school,
        modules=modules
    )


# ═══════════════════════════════════════════════════════════════
#  FILTER STAFF ATTENDANCE
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/staff-attendance/filter", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def filter_staff_attendance():
    guard = any_role_required()
    if guard:
        return guard

    school_id = get_jwt().get("school_id")
    date_str  = request.args.get("date")
    status    = request.args.get("status")

    try:
        query = (
            StaffAttendance.query
            .join(Staff, Staff.id == StaffAttendance.staff_id)
            .filter(StaffAttendance.school_id == school_id)
        )

        if date_str:
            try:
                attendance_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                query = query.filter(StaffAttendance.date == attendance_date)
            except ValueError:
                return jsonify({"message": "Invalid date format"}), 400

        if status:
            query = query.filter(StaffAttendance.status == status)

        records = query.order_by(StaffAttendance.date.desc()).all()

        results = []
        for record in records:
            results.append({
                "id":         record.id,
                "first_name": record.staff.first_name,
                "last_name":  record.staff.last_name,
                "status":     record.status,
                "time_in":    record.time_in.strftime("%H:%M") if record.time_in else None,
                "date":       record.date.strftime("%Y-%m-%d"),
                "notes":      record.notes or ""
            })

        return jsonify(results), 200

    except Exception:
        logger.exception("filter_staff_attendance failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to load staff attendance records."}), 500


# ═══════════════════════════════════════════════════════════════
#  SAVE STAFF ATTENDANCE
# ═══════════════════════════════════════════════════════════════

@academics_api.route("/staff-attendances", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def save_staff_attendance():
    guard = any_role_required()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    payload   = request.get_json(silent=True) or {}
    attendance_records = payload.get("attendance", [])

    if not attendance_records:
        return jsonify({"message": "No attendance records provided"}), 400

    config = AcademicConfig.query.filter_by(school_id=school_id).first()
    if not config:
        return jsonify({"message": "Academic configuration not found"}), 400

    if not config.current_term_id or not config.current_academic_year_id:
        return jsonify({"message": "Current academic year or term not configured"}), 400

    saved_count = 0

    try:
        for item in attendance_records:
            staff_id = item.get("staff_id")
            status   = item.get("status")
            time_in  = item.get("time_in")
            notes    = item.get("notes", "")
            date_str = item.get("date")

            if not staff_id or not status or not date_str:
                continue

            staff = Staff.query.filter_by(id=staff_id, school_id=school_id).first()
            if not staff:
                continue

            try:
                attendance_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            parsed_time = None
            if time_in:
                try:
                    parsed_time = datetime.strptime(time_in, "%H:%M").time()
                except ValueError:
                    parsed_time = None

            existing = StaffAttendance.query.filter_by(
                staff_id=staff_id,
                date=attendance_date
            ).first()

            if existing:
                existing.status  = status
                existing.time_in = parsed_time
                existing.notes   = notes
            else:
                attendance = StaffAttendance(
                    school_id=school_id,
                    staff_id=staff_id,
                    academic_year_id=config.current_academic_year_id,
                    term_id=config.current_term_id,
                    status=status,
                    time_in=parsed_time,
                    notes=notes,
                    date=attendance_date
                )
                db.session.add(attendance)

            saved_count += 1

        db.session.commit()
        return jsonify({"message": f"{saved_count} attendance records saved"}), 200

    except Exception:
        db.session.rollback()
        logger.exception("save_staff_attendance failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to save attendance. Please try again."}), 500