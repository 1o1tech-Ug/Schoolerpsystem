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
  - Marks entry supports an optional per-mark `comment` alongside the
    score. This is what report_card_service.py reads to populate the
    "Comment" column on the nursery report card next to each Learning
    Area score. load_marks_entry() returns saved_comments alongside
    saved_marks so the UI can pre-fill existing comments when
    reopening the form; save_marks() accepts an optional "comment" key
    per row in the `marks` payload and persists it to
    StudentMark.comment.
  - [NEW] Nursery Learning Activities. Mirrors app/apis/academics_api_2.py:
    for streams whose parent Class is Daycare/KG1/KG2/KG3, the teacher
    marks-entry flow also surfaces NurseryActivity rows and lets the
    teacher leave a per-activity remark per student, persisted to
    StudentActivityComment. Activities are NOT subject-specific (they
    apply to the whole nursery stream), so they're gated purely on
    `_is_nursery_stream()` and are shown regardless of which subject
    the teacher is currently marking. Every non-nursery class is
    completely untouched by this — see the helpers below.
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
    LessonSession, StudentAttendance,StudentDailyAttendance,
    NurseryActivity, StudentActivityComment,   # [NEW]
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
#  [NEW] NURSERY DETECTION & LEARNING-ACTIVITY HELPERS
#
#  Mirrors app/apis/academics_api_2.py's helpers of the same name —
#  "Nursery" means the stream's parent Class is one of the school's
#  early-years classes (Daycare, KG1, KG2, KG3). Everything below is
#  only ever consulted when a request's stream resolves to one of
#  these; every other class is completely untouched, so subject marks
#  entry for Primary/Secondary streams behaves exactly as before.
# ═══════════════════════════════════════════════════════════════

NURSERY_CLASS_NAMES = {"daycare", "kg1", "kg2", "kg3"}


def _normalize_class_name(name):
    return (name or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")


def _is_nursery_stream(stream_id, school_id):
    """
    True only if `stream_id` belongs (via its Class) to one of the
    school's nursery classes. Gates every nursery-only branch below —
    non-nursery streams never look at NurseryActivity /
    StudentActivityComment at all.
    """
    if not stream_id:
        return False

    stream = (
        Stream.query
        .options(joinedload(Stream.class_))
        .join(Class)
        .filter(Stream.id == stream_id, Class.school_id == school_id)
        .first()
    )
    if not stream or not stream.class_:
        return False

    return _normalize_class_name(stream.class_.name) in NURSERY_CLASS_NAMES


def _get_nursery_activities(school_id):
    return (
        NurseryActivity.query
        .filter_by(school_id=school_id)
        .order_by(NurseryActivity.position, NurseryActivity.id)
        .all()
    )


def _serialize_activity(activity):
    return {
        "id":        activity.id,
        "name":      activity.name,
        "icon_path": activity.icon_path,
        "position":  activity.position,
    }


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



def _sync_daily_attendance(school_id, student_ids, att_date):
    """
    Recomputes the daily attendance status for each student on att_date
    from ALL their lesson sessions that day (not just the one just saved),
    and upserts into StudentDailyAttendance.

    Rule: present if present/late in ANY lesson that day, else absent.
    """
    if not student_ids:
        return

    # All lesson sessions for this school on this date
    session_ids = [
        s.id for s in LessonSession.query.filter_by(
            school_id=school_id, date=att_date
        ).all()
    ]
    if not session_ids:
        return

    # All lesson-level attendance rows for these students on that date
    rows = StudentAttendance.query.filter(
        StudentAttendance.school_id == school_id,
        StudentAttendance.lesson_id.in_(session_ids),
        StudentAttendance.student_id.in_(student_ids),
    ).all()

    status_by_student = {}
    for r in rows:
        if r.status in ("present", "late"):
            status_by_student[r.student_id] = "present"
        else:
            status_by_student.setdefault(r.student_id, "absent")

    existing = {
        d.student_id: d
        for d in StudentDailyAttendance.query.filter_by(
            school_id=school_id, date=att_date
        ).filter(StudentDailyAttendance.student_id.in_(student_ids)).all()
    }

    for sid, status in status_by_student.items():
        if sid in existing:
            existing[sid].status = status
        else:
            db.session.add(StudentDailyAttendance(
                school_id=school_id, student_id=sid,
                date=att_date, status=status,
            ))

def _get_or_create_assessment(
    school_id,
    stream_id,
    subject_id,
    term_id,
    exam_type_enum,
    paper_id=None,
    assignment=None,
):
    assessment = Assessment.query.filter_by(
        school_id=school_id,
        stream_id=stream_id,
        subject_id=subject_id,
        term_id=term_id,
        type=exam_type_enum,
        paper_id=paper_id,
    ).first()

    if not assessment:
        max_score = 100

        if paper_id:
            paper = Papers.query.get(paper_id)
            if paper and paper.max_marks:
                max_score = float(paper.max_marks)

        assessment = Assessment(
            school_id=school_id,
            assignment_id=assignment.id if assignment else None,
            stream_id=stream_id,
            subject_id=subject_id,
            term_id=term_id,
            type=exam_type_enum,
            paper_id=paper_id,
            max_score=max_score,
        )

        db.session.add(assessment)
        db.session.flush()

    return assessment


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

    # [NEW] Only Nursery (Daycare/KG1/KG2/KG3) streams carry Learning
    # Activities. Activities aren't subject-specific, so this is
    # resolved purely from the stream, independent of subject_id below.
    is_nursery = _is_nursery_stream(stream_id, school_id)

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

        saved_map      = {}
        saved_comments = {}   # (student_id, paper_id) -> comment
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
                    saved_map[(m.student_id, asmt.paper_id)]      = m.score
                    saved_comments[(m.student_id, asmt.paper_id)] = m.comment

        # [NEW] Nursery Learning Activities for this stream, plus any
        # comments already saved for this exact term/exam_type. Shown
        # regardless of which subject is currently selected.
        activities_data = []
        activity_comments_by_student = {}
        if is_nursery:
            activities = _get_nursery_activities(school_id)
            activities_data = [_serialize_activity(a) for a in activities]

            if activities and term_id and student_ids:
                activity_ids = [a.id for a in activities]
                for c in StudentActivityComment.query.filter(
                    StudentActivityComment.activity_id.in_(activity_ids),
                    StudentActivityComment.student_id.in_(student_ids),
                    StudentActivityComment.term_id == term_id,
                    StudentActivityComment.exam_type == exam_enum,
                ).all():
                    activity_comments_by_student.setdefault(c.student_id, {})[c.activity_id] = c.comment

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
                # Parallel map of any comment already left for each
                # paper, so re-opening the edit form shows it.
                row["saved_comments"] = {
                    str(p.id): saved_comments.get((s.id, p.id)) or ""
                    for p in papers
                }
            else:
                row["saved_score"]   = saved_map.get((s.id, None))
                row["saved_comment"] = saved_comments.get((s.id, None)) or ""

            # [NEW] activity_comments keyed by activity id, mirroring
            # the shape of saved_comments above. Empty dict for
            # non-nursery streams so the frontend never has to
            # special-case it.
            if is_nursery:
                row["activity_comments"] = {
                    str(a["id"]): activity_comments_by_student.get(s.id, {}).get(a["id"]) or ""
                    for a in activities_data
                }
            else:
                row["activity_comments"] = {}

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
            "activities":   activities_data,   # [NEW]
            "is_nursery":   is_nursery,         # [NEW]
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

    # [NEW] Learning Activity remarks — not subject-specific, so kept
    # apart from `marks`. `student_id` here identifies who the
    # activity_comments belong to (the modal only ever edits one
    # student at a time, same as `marks` above).
    activity_comments = payload.get("activity_comments", [])
    student_id_top    = payload.get("student_id")

    if not stream_id or not subject_id or not exam_type:
        return jsonify({"message": "stream_id, subject_id and exam_type are required"}), 400
    if not marks and not activity_comments:
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

    # [NEW] Activity comments are only ever persisted for nursery
    # streams — if a non-nursery request somehow includes them, they
    # are silently ignored rather than erroring the whole save.
    is_nursery = _is_nursery_stream(stream_id, school_id)

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

        # Optional per-mark comment — trim, cap length, blank → None
        # so we don't store empty strings that would otherwise render as
        # a populated-but-blank comment cell on the report.
        raw_comment = row.get("comment")
        row["comment"] = (str(raw_comment).strip()[:1000] or None) if raw_comment else None

    try:
        assessment_cache = {}
        saved = 0

        for row in marks:
            student_id = row.get("student_id")
            paper_id   = row.get("paper_id")
            score      = row.get("score")
            comment    = row.get("comment")

            if student_id is None or score is None:
                continue

            if paper_id not in assessment_cache:
            	asmt = _get_or_create_assessment(
        school_id=school_id,
        stream_id=stream_id,
        subject_id=subject_id,
        term_id=config.current_term_id,
        exam_type_enum=exam_enum,
        paper_id=paper_id,
        assignment=assignment,
    )

            assessment_cache[paper_id] = asmt

            asmt = assessment_cache[paper_id]

            existing = StudentMark.query.filter_by(
                school_id=school_id,
                assessment_id=asmt.id,
                student_id=student_id,
            ).first()

            if existing:
                existing.score = score
                # Only overwrite the comment if one was actually
                # submitted this time — leaving the field blank in the
                # UI on a later save shouldn't silently wipe a comment
                # a teacher left earlier for this same assessment.
                if comment is not None:
                    existing.comment = comment
            else:
                db.session.add(StudentMark(
                    school_id=school_id,
                    assessment_id=asmt.id,
                    student_id=student_id,
                    score=score,
                    comment=comment,
                ))

            saved += 1

        # [NEW] Persist Learning Activity remarks (nursery only). Each
        # comment lands directly in StudentActivityComment, keyed on
        # (activity_id, student_id, term_id, exam_type) — no Assessment
        # or StudentMark row is created for these, since activities are
        # comment-only and don't carry a score. Any teacher assigned to
        # this nursery stream can leave/update these remarks, same as
        # the academics-module admin view.
        activities_saved = 0
        if is_nursery and activity_comments:
            for item in activity_comments:
                activity_id        = item.get("activity_id")
                target_student_id  = item.get("student_id") or student_id_top
                raw_comment        = item.get("comment")

                if not activity_id or not target_student_id:
                    continue

                comment_val = str(raw_comment).strip()[:1000] if raw_comment else ""
                if not comment_val:
                    # Blank submission = "leave whatever is already saved
                    # alone", mirroring the mark-comment behaviour above.
                    continue

                activity = NurseryActivity.query.filter_by(
                    id=activity_id, school_id=school_id
                ).first()
                if not activity:
                    continue  # unknown/foreign activity id — skip quietly

                existing_ac = StudentActivityComment.query.filter_by(
                    activity_id=activity_id,
                    student_id=target_student_id,
                    term_id=config.current_term_id,
                    exam_type=exam_enum,
                ).first()

                if existing_ac:
                    existing_ac.comment = comment_val
                else:
                    db.session.add(StudentActivityComment(
                        school_id=school_id,
                        activity_id=activity_id,
                        student_id=target_student_id,
                        term_id=config.current_term_id,
                        exam_type=exam_enum,
                        comment=comment_val,
                    ))
                activities_saved += 1

        db.session.commit()

        message = f"{saved} mark(s) saved successfully"
        if activities_saved:
            message += f", {activities_saved} activity comment(s) saved"

        return jsonify({
            "message":          message,
            "saved":            saved,
            "activities_saved": activities_saved,   # [NEW]
        }), 200

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

        # [NEW] Not subject-specific — surfaced alongside this
        # subject's saved marks whenever the stream is nursery.
        is_nursery = _is_nursery_stream(stream_id, school_id)

        student_marks = {}
        for asmt in assessments:
            paper_name = asmt.paper.paper_name if asmt.paper else None
            for m in asmt.marks:
                student_marks.setdefault(m.student_id, {})
                # carry the comment through alongside the score so the
                # saved-marks browser can show it too if desired.
                student_marks[m.student_id][paper_name] = {
                    "score": m.score, "comment": m.comment or "",
                }

        if not student_marks:
            return jsonify({
                "records":          [],
                "activity_columns": [],
                "is_nursery":       is_nursery,
            }), 200

        student_ids  = list(student_marks.keys())
        student_rows = Student.query.filter(Student.id.in_(student_ids)).all()
        student_map  = {s.id: s for s in student_rows}

        # [NEW] Learning Activity columns + saved comments, nursery only.
        activity_columns = []
        activity_comments_map = {}
        if is_nursery:
            activities = _get_nursery_activities(school_id)
            activity_columns = [
                {
                    "key":         f"activity_{a.id}",
                    "label":       a.name,
                    "activity_id": a.id,
                }
                for a in activities
            ]
            if activities:
                activity_ids = [a.id for a in activities]
                for c in StudentActivityComment.query.filter(
                    StudentActivityComment.activity_id.in_(activity_ids),
                    StudentActivityComment.student_id.in_(student_ids),
                    StudentActivityComment.term_id == term_id,
                    StudentActivityComment.exam_type == exam_enum,
                ).all():
                    activity_comments_map.setdefault(c.activity_id, {})[c.student_id] = c.comment

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
                    {"paper_name": pname, "score": data["score"], "comment": data["comment"]}
                    for pname, data in sorted(marks_by_paper.items())
                    if pname is not None
                ]
            else:
                data = marks_by_paper.get(None) or {"score": None, "comment": ""}
                row["score"]   = data["score"]
                row["comment"] = data["comment"]

            # [NEW] activity remarks for this student, keyed the same
            # way as the columns above, nursery streams only.
            if is_nursery:
                row["activities"] = {
                    col["key"]: activity_comments_map.get(col["activity_id"], {}).get(sid) or ""
                    for col in activity_columns
                }
            else:
                row["activities"] = {}

            result.append(row)

        result.sort(key=lambda r: r["student_name"])
        return jsonify({
            "records":          result,
            "activity_columns": activity_columns,   # [NEW]
            "is_nursery":       is_nursery,           # [NEW]
        }), 200

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
      

        try:
            student_ids = [r.get("student_id") for r in records if r.get("student_id")]
            _sync_daily_attendance(school_id, student_ids, att_date)
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception("daily attendance sync failed | assignment_id=%s date=%s", assignment_id, date_str)
            # don't fail the request — lesson attendance was already saved successfully

        return jsonify({"message": f"Attendance saved — {saved} record(s)", "saved": saved}), 200
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