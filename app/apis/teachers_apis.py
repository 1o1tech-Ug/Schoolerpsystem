"""
app/apis/teachers_apis.py
==========================
Teacher-facing APIs: marks entry and attendance recording.

CHANGES vs original:
  - Rate limits applied per endpoint sensitivity.
  - All except blocks log internally and return safe client messages.
    No str(e) / str(exc) ever reaches the client.
  - Removed bare print() calls.
  - Pagination added to /attendance/students and /attendance/history.
"""

import logging
from flask import Blueprint, request, jsonify, render_template
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError

from app.extensions import db, limiter
from app.models.core import School, UserModule
from app.models.people import Staff, Student
from app.models.user import User
from app.models.academic_structure import (
    Class, Stream, Subject, Papers,
    TeachAssignment,
    StudentStream,
    Assessment, AssessmentType, StudentMark,
    AcademicConfig, Term, AcademicYear,
    LessonSession, StudentAttendance,
)
from app.core.rate_limit import (
    READ_LIMIT, WRITE_LIMIT, MARKS_SAVE_LIMIT,
)

logger = logging.getLogger(__name__)

teachers_api = Blueprint(
    "teachers_api",
    __name__,
    url_prefix="/api/teachers",
)

ALL_ROLES = {"staff", "admin"}

# Pagination defaults
_STUDENTS_PER_PAGE  = 30
_HISTORY_PER_PAGE   = 25


# ═══════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ═══════════════════════════════════════════════════════════════

def _any_role():
    if get_jwt().get("role") not in ALL_ROLES:
        return jsonify({"message": "Unauthorized"}), 403
    return None


def _school_or_404(school_id):
    s = School.query.get(school_id)
    if not s:
        return None, (jsonify({"message": "School not found"}), 404)
    return s, None


def _get_context(claims):
    school_id = claims.get("school_id")
    user_id   = claims.get("sub")
    modules   = [m.module_name for m in UserModule.query.filter_by(user_id=user_id).all()]
    return school_id, user_id, modules


def _resolve_staff(claims, school_id, staff_id):
    if staff_id:
        staff = Staff.query.filter_by(id=staff_id, school_id=school_id).first()
        if staff:
            return staff, None

    try:
        user = User.query.get(claims.get("sub"))
        if user:
            staff = Staff.query.filter_by(
                school_id=school_id,
                staff_code=getattr(user, "staff_code", None),
            ).first()
            if staff:
                return staff, None
    except Exception:
        pass

    return None, (jsonify({"message": "Staff record not found for this user"}), 404)


def _assignment_lookup(school_id, staff_id):
    return (
        TeachAssignment.query
        .options(
            joinedload(TeachAssignment.stream).joinedload(Stream.class_),
            joinedload(TeachAssignment.subject),
        )
        .filter_by(school_id=school_id, staff_id=staff_id)
        .all()
    )


def _build_streams_and_levels(assignments):
    stream_map = {}
    levels     = {}

    for a in assignments:
        if a.stream and a.stream_id not in stream_map:
            cls_name = a.stream.class_.name if a.stream.class_ else ""
            stream_map[a.stream_id] = {
                "id":         a.stream.id,
                "name":       a.stream.name,
                "class_name": cls_name,
                "label":      f"{cls_name} {a.stream.name}",
            }

        if a.subject:
            level = a.subject.level or "Other"
            levels.setdefault(level, [])
            if not any(s.id == a.subject.id for s in levels[level]):
                levels[level].append(a.subject)

    return list(stream_map.values()), levels


def _serialize_assignment(a):
    subject = a.subject or Subject.query.get(a.subject_id)
    stream  = a.stream  or (Stream.query.get(a.stream_id) if a.stream_id else None)
    cls     = (stream.class_ if stream and hasattr(stream, "class_") else None) or \
              (Class.query.get(stream.class_id) if stream else None)

    stream_label = f"{cls.name} {stream.name}" if cls and stream else (stream.name if stream else None)

    return {
        "id":            a.id,
        "subject_id":    a.subject_id,
        "subject_name":  subject.name  if subject else "",
        "subject_level": subject.level if subject else "",
        "stream_id":     a.stream_id,
        "stream_name":   stream.name   if stream  else None,
        "stream_label":  stream_label,
        "class_id":      cls.id        if cls     else None,
        "class_name":    cls.name      if cls     else None,
    }


# ═══════════════════════════════════════════════════════════════
#  MARKS ENTRY PAGE  —  GET /api/teachers/marks-entry
# ═══════════════════════════════════════════════════════════════

@teachers_api.route("/marks-entry", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def marks_entry_page():
    guard = _any_role()
    if guard:
        return guard

    claims                      = get_jwt()
    school_id, user_id, modules = _get_context(claims)
    school, err                 = _school_or_404(school_id)
    if err:
        return err

    user_ = User.query.filter_by(school_id=school_id, role="staff", id=claims.get("sub")).first()
    staff_id = user_.staff_id if user_ else None
    staff, err = _resolve_staff(claims, school_id, staff_id)
    if err:
        return err

    assignments         = _assignment_lookup(school_id, staff.id)
    streams, levels     = _build_streams_and_levels(assignments)

    config         = AcademicConfig.query.filter_by(school_id=school_id).first()
    academic_years = AcademicYear.query.order_by(AcademicYear.name.desc()).all()
    terms          = Term.query.filter_by(school_id=school_id).order_by(Term.name).all()

    return render_template(
        "modules/teachers/marks_entry.html",
        teacher_name    = f"{staff.first_name} {staff.last_name}",
        streams         = streams,
        levels          = levels,
        academic_years  = academic_years,
        terms           = terms,
        current_term_id = config.current_term_id              if config else None,
        current_year_id = config.current_academic_year_id     if config else None,
        school          = school,
        modules         = modules,
    )


# ═══════════════════════════════════════════════════════════════
#  LOAD STUDENTS + PAPERS  —  GET /api/teachers/marks-entry/load
# ═══════════════════════════════════════════════════════════════

@teachers_api.route("/marks-entry/load", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def load_marks_entry():
    guard = _any_role()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    user_     = User.query.filter_by(school_id=school_id, role="staff", id=claims.get("sub")).first()
    staff_id  = user_.staff_id if user_ else None
    staff, err = _resolve_staff(claims, school_id, staff_id)
    if err:
        return err

    stream_id  = request.args.get("stream_id",  type=int)
    subject_id = request.args.get("subject_id", type=int)
    exam_type  = request.args.get("exam_type")

    if not stream_id or not subject_id or not exam_type:
        return jsonify({"message": "stream_id, subject_id and exam_type are required"}), 400

    try:
        exam_enum = AssessmentType(exam_type)
    except ValueError:
        return jsonify({"message": f"Invalid exam_type '{exam_type}'. Use BOT, MID or EOT"}), 400

    assignment = TeachAssignment.query.filter_by(
        school_id=school_id,
        staff_id=staff.id,
        stream_id=stream_id,
        subject_id=subject_id,
    ).first()
    if not assignment:
        return jsonify({"message": "You are not assigned to this class/subject combination"}), 403

    subject = Subject.query.filter_by(id=subject_id, school_id=school_id).first()
    if not subject:
        return jsonify({"message": "Subject not found"}), 404

    try:
        papers = Papers.query.filter_by(
            subject_id=subject_id, school_id=school_id
        ).order_by(Papers.paper_name).all()

        ss_rows     = StudentStream.query.filter_by(stream_id=stream_id, school_id=school_id).all()
        student_ids = [ss.student_id for ss in ss_rows]
        students    = (
            Student.query
            .filter(Student.id.in_(student_ids))
            .order_by(Student.first_name, Student.last_name)
            .all()
        )

        config  = AcademicConfig.query.filter_by(school_id=school_id).first()
        term_id = config.current_term_id if config else None

        saved_map = {}
        if term_id:
            assessments = Assessment.query.filter_by(
                school_id=school_id,
                assignment_id=assignment.id,
                term_id=term_id,
                type=exam_enum,
            ).all()
            for asmt in assessments:
                marks = StudentMark.query.filter_by(
                    school_id=school_id,
                    assessment_id=asmt.id,
                ).all()
                for m in marks:
                    saved_map[(m.student_id, asmt.paper_id)] = m.score

        stream = Stream.query.get(stream_id)
        cls    = Class.query.get(stream.class_id) if stream else None
        stream_label = f"{cls.name} {stream.name}" if cls and stream else ""

        student_data = []
        for s in students:
            row = {
                "id":           s.id,
                "student_code": s.student_code,
                "name":         f"{s.first_name} {s.last_name}",
                "class_name":   s.class_.name if s.class_ else "",
                "stream_name":  stream_label,
            }
            if papers:
                row["saved_marks"] = {
                    str(p.id): saved_map.get((s.id, p.id))
                    for p in papers
                }
            else:
                row["saved_score"] = saved_map.get((s.id, None))
            student_data.append(row)

        return jsonify({
            "subject": {
                "id":    subject.id,
                "name":  subject.name,
                "level": subject.level,
            },
            "papers": [
                {"id": p.id, "paper_name": p.paper_name, "max_marks": p.max_marks}
                for p in papers
            ],
            "stream_label": stream_label,
            "students":     student_data,
        }), 200

    except Exception:
        logger.exception("load_marks_entry failed | stream_id=%s subject_id=%s", stream_id, subject_id)
        return jsonify({"message": "Failed to load marks data. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  SAVE MARKS  —  POST /api/teachers/marks-entry/save
# ═══════════════════════════════════════════════════════════════

@teachers_api.route("/marks-entry/save", methods=["POST"])
@jwt_required()
@limiter.limit(MARKS_SAVE_LIMIT)
def save_marks():
    guard = _any_role()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    user_     = User.query.filter_by(school_id=school_id, role="staff", id=claims.get("sub")).first()
    staff_id  = user_.staff_id if user_ else None
    staff, err = _resolve_staff(claims, school_id, staff_id)
    if err:
        return err

    payload    = request.get_json(silent=True) or {}
    stream_id  = payload.get("stream_id")
    subject_id = payload.get("subject_id")
    exam_type  = payload.get("exam_type")
    marks      = payload.get("marks", [])

    if not stream_id or not subject_id or not exam_type:
        return jsonify({"message": "stream_id, subject_id and exam_type are required"}), 400
    if not marks:
        return jsonify({"message": "marks list is empty"}), 400

    try:
        exam_enum = AssessmentType(exam_type)
    except ValueError:
        return jsonify({"message": f"Invalid exam_type '{exam_type}'"}), 400

    assignment = TeachAssignment.query.filter_by(
        school_id=school_id,
        staff_id=staff.id,
        stream_id=stream_id,
        subject_id=subject_id,
    ).first()
    if not assignment:
        return jsonify({"message": "You are not assigned to this class/subject"}), 403

    config = AcademicConfig.query.filter_by(school_id=school_id).first()
    if not config or not config.current_term_id:
        return jsonify({"message": "Current academic term not configured"}), 400

    papers_cache = {}

    for row in marks:
        score    = row.get("score")
        paper_id = row.get("paper_id")

        try:
            score = float(score)
            if score < 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"message": f"Score must be a non-negative number, got: {row.get('score')}"}), 400

        if paper_id:
            paper = papers_cache.get(paper_id) or Papers.query.filter_by(
                id=paper_id, subject_id=subject_id, school_id=school_id
            ).first()
            if not paper:
                return jsonify({"message": f"Paper {paper_id} not found for this subject"}), 404
            papers_cache[paper_id] = paper
            if paper.max_marks and score > paper.max_marks:
                return jsonify({
                    "message": f"Score {score} exceeds max marks {paper.max_marks} for {paper.paper_name}"
                }), 400

        row["score"] = score

    try:
        assessment_cache = {}
        saved = 0

        for row in marks:
            student_id = row.get("student_id")
            paper_id   = row.get("paper_id")
            score      = row.get("score")

            if student_id is None or score is None:
                continue

            if paper_id not in assessment_cache:
                asmt = Assessment.query.filter_by(
                    school_id=school_id,
                    assignment_id=assignment.id,
                    term_id=config.current_term_id,
                    type=exam_enum,
                    paper_id=paper_id,
                ).first()

                if not asmt:
                    paper     = papers_cache.get(paper_id)
                    max_score = paper.max_marks if paper else 100
                    asmt = Assessment(
                        school_id=school_id,
                        assignment_id=assignment.id,
                        term_id=config.current_term_id,
                        paper_id=paper_id,
                        type=exam_enum,
                        max_score=max_score,
                    )
                    db.session.add(asmt)
                    db.session.flush()

                assessment_cache[paper_id] = asmt

            asmt = assessment_cache[paper_id]

            existing = StudentMark.query.filter_by(
                school_id=school_id,
                assessment_id=asmt.id,
                student_id=student_id,
            ).first()

            if existing:
                existing.score = score
            else:
                db.session.add(StudentMark(
                    school_id=school_id,
                    assessment_id=asmt.id,
                    student_id=student_id,
                    score=score,
                ))

            saved += 1

        db.session.commit()
        return jsonify({"message": f"{saved} mark(s) saved successfully", "saved": saved}), 200

    except IntegrityError:
        db.session.rollback()
        logger.exception("save_marks IntegrityError | stream_id=%s subject_id=%s", stream_id, subject_id)
        return jsonify({"message": "A data integrity error occurred. Please try again."}), 400
    except Exception:
        db.session.rollback()
        logger.exception("save_marks failed | stream_id=%s subject_id=%s", stream_id, subject_id)
        return jsonify({"message": "Failed to save marks. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  FILTER SAVED MARKS  —  GET /api/teachers/marks-entry/filter
# ═══════════════════════════════════════════════════════════════

@teachers_api.route("/marks-entry/filter", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def filter_marks():
    guard = _any_role()
    if guard:
        return guard

    claims    = get_jwt()
    school_id = claims.get("school_id")
    user_     = User.query.filter_by(school_id=school_id, role="staff", id=claims.get("sub")).first()
    staff_id  = user_.staff_id if user_ else None
    staff, err = _resolve_staff(claims, school_id, staff_id)
    if err:
        return err

    year_id    = request.args.get("year_id",    type=int)
    term_id    = request.args.get("term_id",    type=int)
    exam_type  = request.args.get("exam_type")
    stream_id  = request.args.get("stream_id",  type=int)
    subject_id = request.args.get("subject_id", type=int)

    if not all([year_id, term_id, exam_type, stream_id, subject_id]):
        return jsonify({"message": "All filters (year_id, term_id, exam_type, stream_id, subject_id) are required"}), 400

    try:
        exam_enum = AssessmentType(exam_type)
    except ValueError:
        return jsonify({"message": f"Invalid exam_type '{exam_type}'"}), 400

    assignment = TeachAssignment.query.filter_by(
        school_id=school_id,
        staff_id=staff.id,
        stream_id=stream_id,
        subject_id=subject_id,
    ).first()
    if not assignment:
        return jsonify({"message": "You are not assigned to this class/subject"}), 403

    try:
        assessments = (
            Assessment.query
            .options(
                joinedload(Assessment.paper),
                joinedload(Assessment.marks).joinedload(StudentMark.student),
            )
            .filter_by(
                school_id=school_id,
                assignment_id=assignment.id,
                term_id=term_id,
                type=exam_enum,
            )
            .all()
        )

        subject    = Subject.query.get(subject_id)
        papers     = Papers.query.filter_by(subject_id=subject_id, school_id=school_id).all()
        has_papers = len(papers) > 0

        student_marks = {}
        for asmt in assessments:
            paper_name = asmt.paper.paper_name if asmt.paper else None
            for m in asmt.marks:
                student_marks.setdefault(m.student_id, {})
                student_marks[m.student_id][paper_name] = m.score

        if not student_marks:
            return jsonify([]), 200

        student_ids  = list(student_marks.keys())
        student_rows = Student.query.filter(Student.id.in_(student_ids)).all()
        student_map  = {s.id: s for s in student_rows}

        result = []
        for sid, marks_by_paper in student_marks.items():
            s = student_map.get(sid)
            if not s:
                continue

            row = {
                "student_code":  s.student_code,
                "student_name":  f"{s.first_name} {s.last_name}",
                "subject_name":  subject.name  if subject else "",
                "subject_level": subject.level if subject else "",
            }

            if has_papers:
                row["papers"] = [
                    {"paper_name": pname, "score": score}
                    for pname, score in sorted(marks_by_paper.items())
                    if pname is not None
                ]
            else:
                row["score"] = marks_by_paper.get(None)

            result.append(row)

        result.sort(key=lambda r: r["student_name"])
        return jsonify(result), 200

    except Exception:
        logger.exception("filter_marks failed | stream_id=%s subject_id=%s", stream_id, subject_id)
        return jsonify({"message": "Failed to load marks. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  ATTENDANCE PAGE  —  GET /api/teachers/attendance
# ═══════════════════════════════════════════════════════════════

@teachers_api.route("/attendance", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def attendance_page():
    guard = _any_role()
    if guard:
        return guard

    claims                      = get_jwt()
    school_id, user_id, modules = _get_context(claims)
    school, err                 = _school_or_404(school_id)
    if err:
        return err

    user_ = User.query.filter_by(school_id=school_id, role="staff", id=claims.get("sub")).first()
    staff_id = user_.staff_id if user_ else None
    staff, err = _resolve_staff(claims, school_id, staff_id)
    if err:
        return err

    raw         = _assignment_lookup(school_id, staff.id)
    assignments = [_serialize_assignment(a) for a in raw]

    return render_template(
        "modules/teachers/teacher_record_attendance.html",
        teacher_name = f"{staff.first_name} {staff.last_name}",
        assignments  = assignments,
        school       = school,
        modules      = modules,
    )


# ═══════════════════════════════════════════════════════════════
#  LOAD STUDENTS FOR ATTENDANCE  (paginated)
#
#  Query params:
#    assignment_id  int   required
#    date           str   required  (YYYY-MM-DD)
#    page           int   optional  default 1
#    per_page       int   optional  default 30  (max 100)
#
#  Response:
#    {
#      students: [...],
#      existing_attendance: [...],
#      pagination: { page, per_page, total, total_pages, has_next, has_prev }
#    }
# ═══════════════════════════════════════════════════════════════

@teachers_api.route("/attendance/students", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_attendance_students():
    guard = _any_role()
    if guard:
        return guard

    from datetime import datetime

    claims    = get_jwt()
    school_id = claims.get("school_id")

    user_    = User.query.filter_by(school_id=school_id, role="staff", id=claims.get("sub")).first()
    staff_id = user_.staff_id if user_ else None
    staff, err = _resolve_staff(claims, school_id, staff_id)
    if err:
        return err

    assignment_id = request.args.get("assignment_id", type=int)
    date_str      = request.args.get("date")
    page          = max(1, request.args.get("page", 1, type=int))
    per_page      = min(100, max(1, request.args.get("per_page", _STUDENTS_PER_PAGE, type=int)))

    if not assignment_id or not date_str:
        return jsonify({"message": "assignment_id and date are required"}), 400

    assignment = TeachAssignment.query.filter_by(
        id=assignment_id, school_id=school_id, staff_id=staff.id
    ).first()
    if not assignment:
        return jsonify({"message": "Assignment not found or not yours"}), 404

    try:
        att_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"message": "Invalid date format. Use YYYY-MM-DD"}), 400

    try:
        # ── Build base query ─────────────────────────────────────────────────
        if assignment.stream_id:
            ss_rows     = StudentStream.query.filter_by(
                stream_id=assignment.stream_id, school_id=school_id
            ).all()
            student_ids = [ss.student_id for ss in ss_rows]
            base_q = (
                Student.query
                .filter(Student.id.in_(student_ids), Student.school_id == school_id)
                .order_by(Student.first_name, Student.last_name)
            )
        else:
            base_q = (
                Student.query
                .filter_by(school_id=school_id)
                .order_by(Student.first_name, Student.last_name)
            )

        total    = base_q.count()
        students = base_q.offset((page - 1) * per_page).limit(per_page).all()

        # ── Existing attendance for this session ─────────────────────────────
        existing_session = LessonSession.query.filter_by(
            assignment_id=assignment_id, date=att_date, school_id=school_id
        ).first()

        existing_attendance = []
        if existing_session:
            existing_attendance = [
                {"student_id": a.student_id, "status": a.status}
                for a in StudentAttendance.query.filter_by(
                    lesson_id=existing_session.id, school_id=school_id
                ).all()
            ]

        total_pages = max(1, (total + per_page - 1) // per_page)

        return jsonify({
            "students": [
                {
                    "id":           s.id,
                    "first_name":   s.first_name,
                    "last_name":    s.last_name,
                    "student_code": s.student_code,
                }
                for s in students
            ],
            "existing_attendance": existing_attendance,
            "pagination": {
                "page":        page,
                "per_page":    per_page,
                "total":       total,
                "total_pages": total_pages,
                "has_next":    page < total_pages,
                "has_prev":    page > 1,
            },
        }), 200

    except Exception:
        logger.exception("get_attendance_students failed | assignment_id=%s", assignment_id)
        return jsonify({"message": "Failed to load attendance data. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  SAVE ATTENDANCE  —  POST /api/teachers/attendance/save
# ═══════════════════════════════════════════════════════════════

@teachers_api.route("/attendance/save", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def save_attendance():
    guard = _any_role()
    if guard:
        return guard

    from datetime import datetime

    claims    = get_jwt()
    school_id = claims.get("school_id")

    user_    = User.query.filter_by(school_id=school_id, role="staff", id=claims.get("sub")).first()
    staff_id = user_.staff_id if user_ else None
    staff, err = _resolve_staff(claims, school_id, staff_id)
    if err:
        return err

    payload       = request.get_json(silent=True) or {}
    assignment_id = payload.get("assignment_id")
    date_str      = payload.get("date")
    records       = payload.get("records", [])

    if not assignment_id or not date_str or not records:
        return jsonify({"message": "assignment_id, date and records are required"}), 400

    assignment = TeachAssignment.query.filter_by(
        id=assignment_id, school_id=school_id, staff_id=staff.id
    ).first()
    if not assignment:
        return jsonify({"message": "Assignment not found or not yours"}), 404

    try:
        att_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"message": "Invalid date format"}), 400

    valid_statuses = {"present", "absent", "late"}
    for r in records:
        if r.get("status") not in valid_statuses:
            return jsonify({"message": f"Invalid status '{r.get('status')}'"}), 400

    try:
        session = LessonSession.query.filter_by(
            assignment_id=assignment_id, date=att_date, school_id=school_id
        ).first()
        if not session:
            session = LessonSession(
                assignment_id=assignment_id, date=att_date, school_id=school_id
            )
            db.session.add(session)
            db.session.flush()

        existing_map = {
            a.student_id: a
            for a in StudentAttendance.query.filter_by(
                lesson_id=session.id, school_id=school_id
            ).all()
        }

        saved = 0
        for r in records:
            sid    = r.get("student_id")
            status = r.get("status")
            if not sid or not status:
                continue
            if sid in existing_map:
                existing_map[sid].status = status
            else:
                db.session.add(StudentAttendance(
                    school_id=school_id, lesson_id=session.id,
                    student_id=sid, status=status,
                ))
            saved += 1

        db.session.commit()
        return jsonify({"message": f"Attendance saved — {saved} record(s)", "saved": saved}), 200

    except Exception:
        db.session.rollback()
        logger.exception("save_attendance failed | assignment_id=%s", assignment_id)
        return jsonify({"message": "Failed to save attendance. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  ATTENDANCE HISTORY  —  GET /api/teachers/attendance/history
#
#  Query params:
#    date           str   optional  (YYYY-MM-DD)
#    assignment_id  int   optional
#    page           int   optional  default 1
#    per_page       int   optional  default 25  (max 100)
#
#  Response:
#    {
#      records: [...],
#      pagination: { page, per_page, total, total_pages, has_next, has_prev }
#    }
# ═══════════════════════════════════════════════════════════════

@teachers_api.route("/attendance/history", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def attendance_history():
    guard = _any_role()
    if guard:
        return guard

    from datetime import datetime

    claims    = get_jwt()
    school_id = claims.get("school_id")

    user_    = User.query.filter_by(school_id=school_id, role="staff", id=claims.get("sub")).first()
    staff_id = user_.staff_id if user_ else None
    staff, err = _resolve_staff(claims, school_id, staff_id)
    if err:
        return err

    date_str      = request.args.get("date")
    assignment_id = request.args.get("assignment_id", type=int)
    page          = max(1, request.args.get("page", 1, type=int))
    per_page      = min(100, max(1, request.args.get("per_page", _HISTORY_PER_PAGE, type=int)))

    try:
        teacher_ids = [
            a.id for a in TeachAssignment.query.filter_by(
                school_id=school_id, staff_id=staff.id
            ).all()
        ]
        if not teacher_ids:
            return jsonify({"records": [], "pagination": _empty_pagination(page, per_page)}), 200

        search_ids = [assignment_id] if assignment_id and assignment_id in teacher_ids else teacher_ids

        # ── Filter sessions ──────────────────────────────────────────────────
        session_q = LessonSession.query.filter(
            LessonSession.assignment_id.in_(search_ids),
            LessonSession.school_id == school_id,
        )
        if date_str:
            try:
                session_q = session_q.filter(
                    LessonSession.date == datetime.strptime(date_str, "%Y-%m-%d").date()
                )
            except ValueError:
                return jsonify({"message": "Invalid date format"}), 400

        sessions    = session_q.order_by(LessonSession.date.desc()).all()
        if not sessions:
            return jsonify({"records": [], "pagination": _empty_pagination(page, per_page)}), 200

        # ── Build attendance rows in memory, then paginate ───────────────────
        assignment_map = {
            a.id: _serialize_assignment(a)
            for a in TeachAssignment.query.filter(
                TeachAssignment.id.in_(search_ids)
            ).all()
        }
        session_map = {s.id: s for s in sessions}

        att_rows = StudentAttendance.query.filter(
            StudentAttendance.lesson_id.in_([s.id for s in sessions]),
            StudentAttendance.school_id == school_id,
        ).all()

        student_ids = list({a.student_id for a in att_rows})
        student_map = (
            {s.id: s for s in Student.query.filter(Student.id.in_(student_ids)).all()}
            if student_ids else {}
        )

        # ── Assemble full list (date-sorted, newest first) ───────────────────
        all_records = []
        for att in att_rows:
            sess    = session_map.get(att.lesson_id)
            student = student_map.get(att.student_id)
            asmt    = assignment_map.get(sess.assignment_id) if sess else None
            if not sess or not student or not asmt:
                continue
            all_records.append({
                "date":         sess.date.strftime("%Y-%m-%d"),
                "student_id":   att.student_id,
                "first_name":   student.first_name,
                "last_name":    student.last_name,
                "student_code": student.student_code,
                "subject_name": asmt["subject_name"],
                "stream_label": asmt["stream_label"],
                "status":       att.status,
            })

        all_records.sort(key=lambda r: r["date"], reverse=True)

        # ── Slice to requested page ──────────────────────────────────────────
        total       = len(all_records)
        total_pages = max(1, (total + per_page - 1) // per_page)
        start       = (page - 1) * per_page
        page_records = all_records[start: start + per_page]

        return jsonify({
            "records": page_records,
            "pagination": {
                "page":        page,
                "per_page":    per_page,
                "total":       total,
                "total_pages": total_pages,
                "has_next":    page < total_pages,
                "has_prev":    page > 1,
            },
        }), 200

    except Exception:
        logger.exception("attendance_history failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to load attendance history. Please try again."}), 500


def _empty_pagination(page: int, per_page: int) -> dict:
    return {
        "page": page, "per_page": per_page,
        "total": 0, "total_pages": 1,
        "has_next": False, "has_prev": False,
    }