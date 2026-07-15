from flask import Blueprint, request, jsonify, render_template
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
import logging

from app.extensions import db, limiter
from app.models.core import School, UserModule
from app.models.people import Student
from app.models.academic_structure import (
    Class, Stream, Subject, Papers,
    TeachAssignment,
    StudentStream, StudentSubject,
    AcademicConfig, AcademicYear, Term,
    Assessment, AssessmentType,
    StudentMark, GradeScale,
    NurseryActivity, StudentActivityComment,   # [NEW]
)
from app.core.rate_limit import (
    READ_LIMIT, WRITE_LIMIT, MARKS_SAVE_LIMIT,
)

logger = logging.getLogger(__name__)

academics_api_2 = Blueprint(
    "academics_api_2",
    __name__,
    url_prefix="/api/academics2",
)


# ═══════════════════════════════════════════════════════════════
#  GUARDS & CONTEXT
# ═══════════════════════════════════════════════════════════════

def _staff_role_required(claims):
    if claims.get("role") not in {"staff", "teacher"}:
        return jsonify({"message": "Unauthorized"}), 403
    return None


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


# ═══════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ═══════════════════════════════════════════════════════════════

def _exam_type_to_enum(exam_type_str):
    try:
        return AssessmentType(exam_type_str.upper())
    except (ValueError, AttributeError):
        return None


def _get_or_create_assessment(
    school_id, stream_id, subject_id, term_id, exam_type_enum,
    paper_id=None, assignment=None,
):
    assessment = Assessment.query.filter_by(
        school_id  = school_id,
        stream_id  = stream_id,
        subject_id = subject_id,
        term_id    = term_id,
        type       = exam_type_enum,
        paper_id   = paper_id,
    ).first()

    if not assessment:
        max_score = 100.0
        if paper_id:
            paper = Papers.query.get(paper_id)
            if paper and paper.max_marks:
                max_score = float(paper.max_marks)

        assessment = Assessment(
            school_id     = school_id,
            assignment_id = assignment.id if assignment else None,
            stream_id     = stream_id,
            subject_id    = subject_id,
            term_id       = term_id,
            type          = exam_type_enum,
            paper_id      = paper_id,
            max_score     = max_score,
        )
        db.session.add(assessment)
        db.session.flush()

    return assessment


# ═══════════════════════════════════════════════════════════════
#  [NEW] NURSERY DETECTION & LEARNING-ACTIVITY HELPERS
#
#  "Nursery" here means the stream's parent Class is one of the
#  school's early-years classes (Daycare, KG1, KG2, KG3). Everything
#  below is only ever consulted when a request's stream resolves to
#  one of these — every other class is completely untouched, so
#  Subject/Assessment/StudentMark behaviour for Primary/Secondary
#  streams is unchanged.
# ═══════════════════════════════════════════════════════════════

NURSERY_CLASS_NAMES = {"daycare", "kg1", "kg2", "kg3"}


def _normalize_class_name(name):
    return (name or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")


def _is_nursery_stream(stream_id, school_id):
    """
    True only if `stream_id` belongs (via its Class) to one of the
    school's nursery classes. Used to gate every nursery-only branch
    below — non-nursery streams never look at NurseryActivity /
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
#  MARKS ENTRY PAGE  —  GET /api/academics2/marks-entry
# ═══════════════════════════════════════════════════════════════

@academics_api_2.route("/marks-entry", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def marks_entry_page():
    claims = get_jwt()

    err = _staff_role_required(claims)
    if err:
        return err

    school_id, user_id, modules = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    streams = (
        Stream.query
        .options(joinedload(Stream.class_))
        .join(Class)
        .filter(Class.school_id == school_id)
        .order_by(Class.name, Stream.name)
        .all()
    )

    academic_years = AcademicYear.query.order_by(AcademicYear.name).all()
    terms          = Term.query.filter_by(school_id=school_id).order_by(Term.name).all()

    config          = AcademicConfig.query.filter_by(school_id=school_id).first()
    current_year_id = config.current_academic_year_id if config else None
    current_term_id = config.current_term_id          if config else None

    return render_template(
        "modules/academics/marks_entry.html",
        streams         = streams,
        academic_years  = academic_years,
        terms           = terms,
        current_year_id = current_year_id,
        current_term_id = current_term_id,
        school          = school,
        modules         = modules,
    )


# ═══════════════════════════════════════════════════════════════
#  LOAD STUDENTS  —  GET /api/academics2/marks-entry/load
# ═══════════════════════════════════════════════════════════════

@academics_api_2.route("/marks-entry/load", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def load_marks_students():
    claims = get_jwt()

    err = _staff_role_required(claims)
    if err:
        return err

    school_id = claims.get("school_id")
    stream_id = request.args.get("stream_id", type=int)
    term_id   = request.args.get("term_id",   type=int)
    exam_type = request.args.get("exam_type")

    if not stream_id or not term_id or not exam_type:
        return jsonify({"message": "stream_id, term_id and exam_type are required"}), 400

    exam_enum = _exam_type_to_enum(exam_type)
    if exam_enum is None:
        return jsonify({"message": f"Invalid exam_type '{exam_type}'. Use BOT, MID or EOT"}), 400

    if not Stream.query.get(stream_id):
        return jsonify({"message": "Stream not found"}), 404

    # [NEW] Only Nursery (Daycare/KG1/KG2/KG3) streams carry Learning
    # Activities — every other class is handled exactly as before.
    is_nursery = _is_nursery_stream(stream_id, school_id)

    try:
        ss_rows     = StudentStream.query.filter_by(stream_id=stream_id, school_id=school_id).all()
        student_ids = [ss.student_id for ss in ss_rows]

        if not student_ids:
            return jsonify({
                "students": [], "subjects": [],
                "activities": [], "is_nursery": is_nursery,
            }), 200

        students = (
            Student.query
            .filter(Student.id.in_(student_ids))
            .order_by(Student.first_name, Student.last_name)
            .all()
        )

        student_subject_rows = StudentSubject.query.filter(
            StudentSubject.student_id.in_(student_ids)
        ).all()

        student_subject_ids_map = {}
        for ss in student_subject_rows:
            student_subject_ids_map.setdefault(ss.student_id, set()).add(ss.subject_id)

        visible_subject_ids = list({ss.subject_id for ss in student_subject_rows})

        subjects = []
        if visible_subject_ids:
            subjects = (
                Subject.query
                .filter(Subject.id.in_(visible_subject_ids))
                .order_by(Subject.name)
                .all()
            )

        assessments = []
        if visible_subject_ids:
            assessments = Assessment.query.filter(
                Assessment.school_id  == school_id,
                Assessment.stream_id  == stream_id,
                Assessment.subject_id.in_(visible_subject_ids),
                Assessment.term_id    == term_id,
                Assessment.type       == exam_enum,
            ).all()

        asmt_to_subj_paper = {
            asmt.id: (asmt.subject_id, asmt.paper_id)
            for asmt in assessments
        }

        saved_scores   = {}
        saved_comments = {}   # (subject_id, paper_id, student_id) -> comment
        if assessments:
            for mark in StudentMark.query.filter(
                StudentMark.assessment_id.in_([a.id for a in assessments]),
                StudentMark.student_id.in_(student_ids),
            ).all():
                key = asmt_to_subj_paper.get(mark.assessment_id)
                if key:
                    subj_id, paper_id = key
                    saved_scores[(subj_id, paper_id, mark.student_id)]   = mark.score
                    saved_comments[(subj_id, paper_id, mark.student_id)] = mark.comment

        papers_by_subject = {}
        subject_data = []
        for subject in subjects:
            papers = Papers.query.filter_by(subject_id=subject.id).all()
            papers_by_subject[subject.id] = papers
            subject_data.append({
                "id":         subject.id,
                "name":       subject.name,
                "has_papers": len(papers) > 0,
                "papers": [
                    {"id": p.id, "name": p.paper_name, "max_marks": p.max_marks}
                    for p in papers
                ],
            })

        # [NEW] Nursery Learning Activities for this school, plus any
        # comments already saved for this exact term/exam_type.
        activities_data = []
        activity_comments_by_student = {}
        if is_nursery:
            activities = _get_nursery_activities(school_id)
            activities_data = [_serialize_activity(a) for a in activities]

            if activities:
                activity_ids = [a.id for a in activities]
                for c in StudentActivityComment.query.filter(
                    StudentActivityComment.activity_id.in_(activity_ids),
                    StudentActivityComment.student_id.in_(student_ids),
                    StudentActivityComment.term_id == term_id,
                    StudentActivityComment.exam_type == exam_enum,
                ).all():
                    activity_comments_by_student.setdefault(c.student_id, {})[c.activity_id] = c.comment

        student_data = []
        for student in students:
            my_subjects = student_subject_ids_map.get(student.id, set())
            row = {
                "id":           student.id,
                "student_code": student.student_code,
                "name":         f"{student.first_name} {student.last_name}",
                "scores":       {},
                "comments":     {},
            }
            for subject in subjects:
                if subject.id not in my_subjects:
                    row["scores"][str(subject.id)]   = None
                    row["comments"][str(subject.id)] = None
                    continue

                papers = papers_by_subject[subject.id]
                if papers:
                    paper_scores   = {}
                    paper_comments = {}
                    for p in papers:
                        s = saved_scores.get((subject.id, p.id, student.id))
                        if s is not None:
                            paper_scores[str(p.id)] = s
                        c = saved_comments.get((subject.id, p.id, student.id))
                        if c:
                            paper_comments[str(p.id)] = c
                    row["scores"][str(subject.id)]   = paper_scores
                    row["comments"][str(subject.id)] = paper_comments
                else:
                    s = saved_scores.get((subject.id, None, student.id))
                    c = saved_comments.get((subject.id, None, student.id))
                    row["scores"][str(subject.id)]   = s
                    row["comments"][str(subject.id)] = c

            # [NEW] activity_comments keyed by activity id, mirroring
            # the shape of `comments` above. Empty dict for non-nursery
            # streams so the frontend never has to special-case it.
            if is_nursery:
                row["activity_comments"] = {
                    str(a["id"]): activity_comments_by_student.get(student.id, {}).get(a["id"]) or ""
                    for a in activities_data
                }
            else:
                row["activity_comments"] = {}

            student_data.append(row)

        return jsonify({
            "students":   student_data,
            "subjects":   subject_data,
            "activities": activities_data,   # [NEW]
            "is_nursery": is_nursery,         # [NEW]
        }), 200

    except Exception:
        logger.exception("load_marks_students failed | school_id=%s stream_id=%s", school_id, stream_id)
        return jsonify({"message": "Failed to load student marks data."}), 500


# ═══════════════════════════════════════════════════════════════
#  SINGLE STUDENT SUBJECTS  —  GET /api/academics2/marks-entry/student
# ═══════════════════════════════════════════════════════════════

@academics_api_2.route("/marks-entry/student", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def get_student_marks_entry():
    claims = get_jwt()

    err = _staff_role_required(claims)
    if err:
        return err

    school_id  = claims.get("school_id")
    student_id = request.args.get("student_id", type=int)
    stream_id  = request.args.get("stream_id",  type=int)
    term_id    = request.args.get("term_id",    type=int)
    exam_type  = request.args.get("exam_type")

    if not student_id:
        return jsonify({"message": "student_id is required"}), 400
    if not stream_id:
        return jsonify({"message": "stream_id is required"}), 400
    if not term_id:
        return jsonify({"message": "term_id is required"}), 400
    if not exam_type:
        return jsonify({"message": "exam_type is required"}), 400

    exam_enum = _exam_type_to_enum(exam_type)
    if exam_enum is None:
        return jsonify({"message": f"Invalid exam_type '{exam_type}'. Use BOT, MID or EOT"}), 400

    if not Student.query.get(student_id):
        return jsonify({"message": "Student not found"}), 404

    # [NEW] Resolved once up front so we can still return activities
    # even for a nursery student who (unusually) has no StudentSubject
    # rows yet.
    is_nursery = _is_nursery_stream(stream_id, school_id)

    try:
        student_subject_ids = [
            ss.subject_id
            for ss in StudentSubject.query.filter_by(student_id=student_id).all()
        ]

        subjects = []
        if student_subject_ids:
            subjects = (
                Subject.query
                .filter(Subject.id.in_(student_subject_ids))
                .order_by(Subject.name)
                .all()
            )

        assessments = []
        if student_subject_ids:
            assessments = Assessment.query.filter(
                Assessment.school_id  == school_id,
                Assessment.stream_id  == stream_id,
                Assessment.subject_id.in_(student_subject_ids),
                Assessment.term_id    == term_id,
                Assessment.type       == exam_enum,
            ).all()

        assessment_map = {
            (asmt.subject_id, asmt.paper_id): asmt
            for asmt in assessments
        }

        marks_by_assessment    = {}
        comments_by_assessment = {}
        if assessments:
            for mark in StudentMark.query.filter(
                StudentMark.assessment_id.in_([a.id for a in assessments]),
                StudentMark.student_id == student_id,
            ).all():
                marks_by_assessment[mark.assessment_id]    = mark.score
                comments_by_assessment[mark.assessment_id] = mark.comment

        results = []
        for subject in subjects:
            papers = Papers.query.filter_by(subject_id=subject.id).all()

            if papers:
                paper_data = []
                for paper in papers:
                    asmt    = assessment_map.get((subject.id, paper.id))
                    score   = marks_by_assessment.get(asmt.id, "") if asmt else ""
                    comment = comments_by_assessment.get(asmt.id, "") if asmt else ""
                    paper_data.append({
                        "id":        paper.id,
                        "name":      paper.paper_name,
                        "max_marks": paper.max_marks,
                        "score":     score,
                        "comment":   comment or "",
                    })
                results.append({
                    "id":     subject.id,
                    "name":   subject.name,
                    "papers": paper_data,
                })
            else:
                asmt    = assessment_map.get((subject.id, None))
                score   = marks_by_assessment.get(asmt.id, "") if asmt else ""
                comment = comments_by_assessment.get(asmt.id, "") if asmt else ""
                results.append({
                    "id":      subject.id,
                    "name":    subject.name,
                    "score":   score,
                    "comment": comment or "",
                    "papers":  [],
                })

        # [NEW] Learning Activities + this student's saved comments for
        # this term/exam_type — nursery streams only.
        activities_result = []
        if is_nursery:
            activities = _get_nursery_activities(school_id)
            if activities:
                activity_ids   = [a.id for a in activities]
                comments_map = {
                    c.activity_id: c.comment
                    for c in StudentActivityComment.query.filter(
                        StudentActivityComment.activity_id.in_(activity_ids),
                        StudentActivityComment.student_id == student_id,
                        StudentActivityComment.term_id == term_id,
                        StudentActivityComment.exam_type == exam_enum,
                    ).all()
                }
                for a in activities:
                    activities_result.append({
                        "id":        a.id,
                        "name":      a.name,
                        "icon_path": a.icon_path,
                        "comment":   comments_map.get(a.id, "") or "",
                    })

        return jsonify({
            "subjects":   results,
            "activities": activities_result,   # [NEW]
            "is_nursery": is_nursery,           # [NEW]
        }), 200

    except Exception:
        logger.exception("get_student_marks_entry failed | student_id=%s", student_id)
        return jsonify({"message": "Failed to load student marks."}), 500


# ═══════════════════════════════════════════════════════════════
#  SAVE MARKS  —  POST /api/academics2/marks-entry/save
# ═══════════════════════════════════════════════════════════════

@academics_api_2.route("/marks-entry/save", methods=["POST"])
@jwt_required()
@limiter.limit(MARKS_SAVE_LIMIT)
def save_student_marks():
    claims = get_jwt()

    err = _staff_role_required(claims)
    if err:
        return err

    school_id = claims.get("school_id")
    data      = request.get_json(force=True) or {}

    student_id        = data.get("student_id")
    stream_id         = data.get("stream_id")
    term_id           = data.get("term_id")
    exam_type         = data.get("exam_type")
    marks             = data.get("marks", [])
    activity_comments = data.get("activity_comments", [])   # [NEW]

    if not student_id or not stream_id or not term_id or not exam_type:
        return jsonify({"message": "student_id, stream_id, term_id and exam_type are required"}), 400

    exam_enum = _exam_type_to_enum(exam_type)
    if exam_enum is None:
        return jsonify({"message": f"Invalid exam_type '{exam_type}'. Use BOT, MID or EOT"}), 400

    if not Student.query.get(student_id):
        return jsonify({"message": "Student not found"}), 404

    assignments = TeachAssignment.query.filter_by(
        school_id=school_id,
        stream_id=stream_id,
    ).all()
    assignment_by_subject = {a.subject_id: a for a in assignments}

    # [NEW] Activity comments are only ever persisted for nursery
    # streams — if a non-nursery request somehow includes them, they
    # are silently ignored rather than erroring the whole save.
    is_nursery = _is_nursery_stream(stream_id, school_id)

    saved             = 0
    skipped           = 0
    activities_saved  = 0   # [NEW]

    try:
        for item in marks:
            raw_score  = item.get("score")
            subject_id = item.get("subject_id")
            paper_id   = item.get("paper_id") or None

            if raw_score in (None, ""):
                skipped += 1
                continue

            if not subject_id:
                skipped += 1
                continue

            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                skipped += 1
                continue

            if score < 0:
                skipped += 1
                continue

            if paper_id:
                paper = Papers.query.filter_by(
                    id=paper_id,
                    subject_id=subject_id,
                    school_id=school_id,
                ).first()
                if not paper:
                    return jsonify({
                        "message": f"Paper {paper_id} not found for subject {subject_id}"
                    }), 404
                if paper.max_marks and score > paper.max_marks:
                    return jsonify({
                        "message": (
                            f"Score {score} exceeds max marks {paper.max_marks} "
                            f"for paper '{paper.paper_name}'"
                        )
                    }), 400

            # Optional per-mark comment — trim, cap length, blank → None
            # so we don't store empty strings that would otherwise render as
            # a populated-but-blank comment cell on the report.
            raw_comment = item.get("comment")
            comment = (str(raw_comment).strip()[:1000] or None) if raw_comment else None

            assignment = assignment_by_subject.get(subject_id)

            assessment = _get_or_create_assessment(
                school_id      = school_id,
                stream_id      = stream_id,
                subject_id     = subject_id,
                term_id        = term_id,
                exam_type_enum = exam_enum,
                paper_id       = paper_id,
                assignment     = assignment,
            )

            existing = StudentMark.query.filter_by(
                assessment_id = assessment.id,
                student_id    = student_id,
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
                    school_id     = school_id,
                    assessment_id = assessment.id,
                    student_id    = student_id,
                    score         = score,
                    comment       = comment,
                ))
            saved += 1

        # [NEW] Persist Learning Activity remarks (nursery only). Each
        # comment lands directly in StudentActivityComment, keyed on
        # (activity_id, student_id, term_id, exam_type) — no Assessment
        # or StudentMark row is created for these, since activities are
        # comment-only and don't carry a score.
        if is_nursery:
            for item in activity_comments:
                activity_id = item.get("activity_id")
                raw_comment = item.get("comment")

                if not activity_id:
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

                existing = StudentActivityComment.query.filter_by(
                    activity_id=activity_id,
                    student_id=student_id,
                    term_id=term_id,
                    exam_type=exam_enum,
                ).first()

                if existing:
                    existing.comment = comment_val
                else:
                    db.session.add(StudentActivityComment(
                        school_id=school_id,
                        activity_id=activity_id,
                        student_id=student_id,
                        term_id=term_id,
                        exam_type=exam_enum,
                        comment=comment_val,
                    ))
                activities_saved += 1

        db.session.commit()

        message = f"{saved} mark(s) saved successfully"
        if activities_saved:
            message += f", {activities_saved} activity comment(s) saved"
        if skipped:
            message += f" ({skipped} skipped)"

        return jsonify({
            "message":          message,
            "saved":            saved,
            "skipped":          skipped,
            "activities_saved": activities_saved,   # [NEW]
        }), 200

    except Exception:
        db.session.rollback()
        logger.exception("save_student_marks failed | student_id=%s school_id=%s", student_id, school_id)
        return jsonify({"message": "Failed to save marks. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  SAVED MARKS TABLE  —  GET /api/academics2/marks-entry/saved
# ═══════════════════════════════════════════════════════════════

@academics_api_2.route("/marks-entry/saved", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def load_saved_marks():
    claims = get_jwt()

    err = _staff_role_required(claims)
    if err:
        return err

    school_id = claims.get("school_id")
    stream_id = request.args.get("stream_id", type=int)
    term_id   = request.args.get("term_id",   type=int)
    exam_type = request.args.get("exam_type")

    if not stream_id or not term_id or not exam_type:
        return jsonify({"message": "stream_id, term_id and exam_type are required"}), 400

    exam_enum = _exam_type_to_enum(exam_type)
    if exam_enum is None:
        return jsonify({"message": f"Invalid exam_type '{exam_type}'. Use BOT, MID or EOT"}), 400

    is_nursery = _is_nursery_stream(stream_id, school_id)   # [NEW]

    try:
        ss_rows     = StudentStream.query.filter_by(stream_id=stream_id, school_id=school_id).all()
        student_ids = [ss.student_id for ss in ss_rows]

        if not student_ids:
            return jsonify({
                "columns": [], "students": [],
                "activity_columns": [], "is_nursery": is_nursery,
            }), 200

        students = (
            Student.query
            .filter(Student.id.in_(student_ids))
            .order_by(Student.first_name, Student.last_name)
            .all()
        )

        student_subject_rows = StudentSubject.query.filter(
            StudentSubject.student_id.in_(student_ids)
        ).all()

        student_subject_map = {}
        for ss in student_subject_rows:
            student_subject_map.setdefault(ss.student_id, set()).add(ss.subject_id)

        visible_subject_ids = list({ss.subject_id for ss in student_subject_rows})

        subjects = []
        if visible_subject_ids:
            subjects = (
                Subject.query
                .filter(Subject.id.in_(visible_subject_ids))
                .order_by(Subject.name)
                .all()
            )

        papers_by_subject = {
            subject.id: Papers.query
            .filter_by(subject_id=subject.id)
            .order_by(Papers.paper_name)
            .all()
            for subject in subjects
        }

        columns = []
        for subject in subjects:
            papers = papers_by_subject.get(subject.id, [])
            if papers:
                for paper in papers:
                    columns.append({
                        "key":          f"paper_{paper.id}",
                        "label":        paper.paper_name,
                        "subject_id":   subject.id,
                        "subject_name": subject.name,
                        "paper_id":     paper.id,
                        "max_marks":    paper.max_marks,
                    })
            else:
                columns.append({
                    "key":          f"subj_{subject.id}",
                    "label":        subject.name,
                    "subject_id":   subject.id,
                    "subject_name": subject.name,
                    "paper_id":     None,
                    "max_marks":    None,
                })

        assessments = []
        if visible_subject_ids:
            assessments = Assessment.query.filter(
                Assessment.school_id  == school_id,
                Assessment.stream_id  == stream_id,
                Assessment.subject_id.in_(visible_subject_ids),
                Assessment.term_id    == term_id,
                Assessment.type       == exam_enum,
            ).all()

        assessment_ids_map = {
            (asmt.subject_id, asmt.paper_id): asmt.id
            for asmt in assessments
        }

        marks_map    = {}
        comments_map = {}
        if assessment_ids_map:
            for mark in StudentMark.query.filter(
                StudentMark.assessment_id.in_(list(assessment_ids_map.values())),
                StudentMark.student_id.in_(student_ids),
            ).all():
                marks_map.setdefault(mark.assessment_id, {})[mark.student_id]    = mark.score
                comments_map.setdefault(mark.assessment_id, {})[mark.student_id] = mark.comment

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

        student_rows = []
        for student in students:
            my_subjects = student_subject_map.get(student.id, set())
            scores   = {}
            comments = {}

            for col in columns:
                subj_id  = col["subject_id"]
                paper_id = col["paper_id"]
                key      = col["key"]

                if subj_id not in my_subjects:
                    scores[key]   = "N/A"
                    comments[key] = ""
                    continue

                asmt_id = assessment_ids_map.get((subj_id, paper_id))
                if asmt_id is None:
                    scores[key]   = "-"
                    comments[key] = ""
                else:
                    val = marks_map.get(asmt_id, {}).get(student.id)
                    scores[key] = val if val is not None else "-"
                    comments[key] = comments_map.get(asmt_id, {}).get(student.id) or ""

            # [NEW] activity remarks for this student, keyed the same
            # way as `comments` above so the frontend can treat them
            # the same way it treats subject comments.
            activities_row = {}
            if is_nursery:
                for col in activity_columns:
                    activities_row[col["key"]] = (
                        activity_comments_map.get(col["activity_id"], {}).get(student.id) or ""
                    )

            student_rows.append({
                "student_code": student.student_code,
                "name":         f"{student.first_name} {student.last_name}",
                "scores":       scores,
                "comments":     comments,
                "activities":   activities_row,   # [NEW]
            })

        return jsonify({
            "columns":          columns,
            "students":         student_rows,
            "activity_columns": activity_columns,   # [NEW]
            "is_nursery":       is_nursery,           # [NEW]
        }), 200

    except Exception:
        logger.exception("load_saved_marks failed | school_id=%s stream_id=%s", school_id, stream_id)
        return jsonify({"message": "Failed to load saved marks."}), 500


# ═══════════════════════════════════════════════════════════════
#  GRADING SYSTEM HELPERS
# ═══════════════════════════════════════════════════════════════

def _levels_for_school(school) -> list:
    school_type = (school.school_type or "").strip().lower()
    mapping = {
        "secondary": ["O Level", "A Level"],
        "primary":   ["Lower Primary", "Upper Primary", "Nursery"],
        "nursery":   ["Nursery"],
        "college":   ["O Level", "A Level"],
    }
    return mapping.get(school_type, [])


def _serialize_grade_scale(gs: GradeScale) -> dict:
    return {
        "id":               gs.id,
        "school_id":        gs.school_id,
        "section_category": gs.section_category,
        "grade":            gs.grade,
        "min_score":        gs.min_score,
        "max_score":        gs.max_score,
        "remark":           gs.remark or "",
    }


# ═══════════════════════════════════════════════════════════════
#  GRADING SYSTEM PAGE
# ═══════════════════════════════════════════════════════════════

@academics_api_2.route("/grading-system-page", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def grading_system_page():
    claims = get_jwt()

    err = _staff_role_required(claims)
    if err:
        return err

    school_id, user_id, modules = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    levels = _levels_for_school(school)

    return render_template(
        "modules/academics/grading_system.html",
        school=school,
        modules=modules,
        levels=levels,
    )


# ═══════════════════════════════════════════════════════════════
#  LIST GRADING RULES
# ═══════════════════════════════════════════════════════════════

@academics_api_2.route("/grading-system", methods=["GET"])
@jwt_required()
@limiter.limit(READ_LIMIT)
def list_grading_system():
    claims = get_jwt()

    err = _staff_role_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)
    _, err = _school_or_404(school_id)
    if err:
        return err

    q = GradeScale.query.filter_by(school_id=school_id)

    level = request.args.get("level", "").strip()
    grade = request.args.get("grade", "").strip().upper()

    if level:
        q = q.filter(GradeScale.section_category == level)
    if grade:
        q = q.filter(GradeScale.grade.ilike(grade))

    rules = (
        q.order_by(
            GradeScale.section_category.asc(),
            GradeScale.min_score.desc(),
        )
        .all()
    )

    return jsonify([_serialize_grade_scale(r) for r in rules]), 200


# ═══════════════════════════════════════════════════════════════
#  ADD GRADING RULE
# ═══════════════════════════════════════════════════════════════

@academics_api_2.route("/grading-system/add", methods=["POST"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def add_grading_rule():
    claims = get_jwt()

    err = _staff_role_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)
    school, err = _school_or_404(school_id)
    if err:
        return err

    payload = request.get_json(silent=True) or {}

    section_category = str(payload.get("section_category", "")).strip()
    grade            = str(payload.get("grade",            "")).strip().upper()
    remark           = str(payload.get("remark",           "")).strip()

    try:
        min_score = float(payload["min_score"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"message": "min_score must be a number"}), 400

    try:
        max_score = float(payload["max_score"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"message": "max_score must be a number"}), 400

    if not section_category:
        return jsonify({"message": "section_category (level) is required"}), 400

    VALID_GRADES = {"D1", "D2", "C3", "C4", "C5", "C6", "P7", "P8", "F9"}
    if grade not in VALID_GRADES:
        return jsonify({
            "message": f"Invalid grade '{grade}'. Must be one of: {', '.join(sorted(VALID_GRADES))}"
        }), 400

    if not (0 <= min_score <= 100):
        return jsonify({"message": "min_score must be between 0 and 100"}), 400
    if not (0 <= max_score <= 100):
        return jsonify({"message": "max_score must be between 0 and 100"}), 400
    if max_score <= min_score:
        return jsonify({"message": "max_score must be greater than min_score"}), 400

    valid_levels = _levels_for_school(school)
    if valid_levels and section_category not in valid_levels:
        return jsonify({
            "message": (
                f"'{section_category}' is not a valid level for this school. "
                f"Valid levels: {', '.join(valid_levels)}"
            )
        }), 400

    existing = GradeScale.query.filter_by(
        school_id=school_id,
        section_category=section_category,
        grade=grade,
    ).first()
    if existing:
        return jsonify({
            "message": (
                f"Grade '{grade}' for '{section_category}' already exists "
                f"(score range {existing.min_score}–{existing.max_score}). "
                "Delete the existing rule before adding a new one."
            )
        }), 400

    try:
        rule = GradeScale(
            school_id        = school_id,
            section_category = section_category,
            grade            = grade,
            min_score        = min_score,
            max_score        = max_score,
            remark           = remark or None,
        )
        db.session.add(rule)
        db.session.commit()

        return jsonify({
            "message": f"Grading rule {grade} ({section_category}) saved successfully.",
            "rule":    _serialize_grade_scale(rule),
        }), 201

    except IntegrityError:
        db.session.rollback()
        return jsonify({
            "message": "Duplicate grading rule — this grade already exists for the selected level."
        }), 400
    except Exception:
        db.session.rollback()
        logger.exception("add_grading_rule failed | school_id=%s", school_id)
        return jsonify({"message": "Failed to save grading rule. Please try again."}), 500


# ═══════════════════════════════════════════════════════════════
#  DELETE GRADING RULE
# ═══════════════════════════════════════════════════════════════

@academics_api_2.route("/grading-system/<int:rule_id>", methods=["DELETE"])
@jwt_required()
@limiter.limit(WRITE_LIMIT)
def delete_grading_rule(rule_id: int):
    claims = get_jwt()

    err = _staff_role_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)
    _, err = _school_or_404(school_id)
    if err:
        return err

    rule = GradeScale.query.filter_by(id=rule_id, school_id=school_id).first()
    if not rule:
        return jsonify({"message": "Grading rule not found"}), 404

    try:
        db.session.delete(rule)
        db.session.commit()
        return jsonify({
            "message": f"Grading rule {rule.grade} ({rule.section_category}) removed."
        }), 200

    except Exception:
        db.session.rollback()
        logger.exception("delete_grading_rule failed | rule_id=%s", rule_id)
        return jsonify({"message": "Failed to delete grading rule. Please try again."}), 500