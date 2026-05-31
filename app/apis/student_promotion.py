from flask import Blueprint, render_template, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
import logging

from app.extensions import db

from app.models.core import School, UserModule
from app.models.people import Student
from app.models.user import User

from app.models.academic_structure import (
    Class,
    Stream,
    AcademicYear,
    StudentStream,
    StudentEnrollment,
)

promotion_api = Blueprint(
    "promotion_api",
    __name__,
    url_prefix="/api/academics/progression"
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
#  CLASS PROGRESSION MAP
# ═══════════════════════════════════════════════════════════════

CLASS_PROGRESSION = {
    # Nursery
    "KG1": "KG2",
    "KG2": "KG3",
    "Baby": "Middle",
    "Middle": "Top",

    # Nursery → Primary  *** CROSS-LEVEL: BLOCKED ***
    # "KG3" / "Top" intentionally omitted — these are level exits

    # Primary
    "P1": "P2",
    "P2": "P3",
    "P3": "P4",
    "P4": "P5",
    "P5": "P6",
    "P6": "P7",

    # Primary → Secondary  *** CROSS-LEVEL: BLOCKED ***
    # "P7" intentionally omitted

    # O-Level
    "S1": "S2",
    "S2": "S3",
    "S3": "S4",

    # O-Level → A-Level  *** CROSS-LEVEL: BLOCKED ***
    # "S4" intentionally omitted

    # A-Level
    "S5": "S6",

    # S6 → College  *** GRADUATING CLASS: no promotion ***
    # "S6" intentionally omitted
}

# Classes that are the last in their level (graduating from that level).
# Students here cannot be promoted using this tool; they need a
# "level transition" enrolment handled separately (e.g. admission to P1 / S1 / S5).
LEVEL_EXIT_CLASSES = {
    "KG3",   # Nursery exit → must be admitted to P1
    "Top",   # Nursery exit → must be admitted to P1
    "P7",    # Primary exit → must be admitted to S1
    "S4",    # O-Level exit → must be admitted to S5
    "S6",    # A-Level exit → graduating (college / university)
}

# What status to record on StudentEnrollment when a student is discharged
# from a level-exit class.
LEVEL_EXIT_STATUSES = {
    "KG3": "level_complete",   # → awaits P1 admission
    "Top": "level_complete",   # → awaits P1 admission
    "P7":  "level_complete",   # → awaits S1 admission
    "S4":  "level_complete",   # → awaits S5 admission
    "S6":  "graduated",        # → truly done with school
}

# Human-readable next-level hint shown in error messages
LEVEL_EXIT_HINTS = {
    "KG3": "admitted to Primary (P1)",
    "Top": "admitted to Primary (P1)",
    "P7":  "admitted to Secondary O-Level (S1)",
    "S4":  "admitted to A-Level (S5)",
    "S6":  "graduated — no further promotion available",
}

# Labels shown in the UI / history view
LEVEL_EXIT_LABELS = {
    "KG3": "Completed Nursery",
    "Top": "Completed Nursery",
    "P7":  "Completed Primary",
    "S4":  "Completed O-Level",
    "S6":  "Graduated",
}


# ═══════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════

def _staff_required(claims):
    """Ensure caller is staff/teacher."""
    if claims.get("role") not in {"staff", "teacher"}:
        return None, (jsonify({"message": "Unauthorized"}), 403)

    school_id = claims.get("school_id")

    user_ = User.query.filter_by(
        school_id=school_id,
        role="staff",
        id=claims.get("sub")
    ).first()

    if not user_:
        return None, (jsonify({"message": "User not found"}), 404)

    return user_, None


def _get_context(claims):
    school_id = claims.get("school_id")
    user_id   = claims.get("sub")

    modules = [
        m.module_name
        for m in UserModule.query.filter_by(user_id=user_id).all()
    ]

    return school_id, user_id, modules


def _school_or_404(school_id):
    school = School.query.get(school_id)

    if not school:
        return None, (jsonify({"message": "School not found"}), 404)

    return school, None


def _get_next_class(current_class):
    """
    Return the next Class within the same level, or None.
    Level-exit classes (KG3, Top, P7, S4, S6) return None intentionally.
    """
    next_name = CLASS_PROGRESSION.get(current_class.name)

    if not next_name:
        return None

    return Class.query.filter_by(
        school_id=current_class.school_id,
        name=next_name
    ).first()


def _is_level_exit(class_name: str) -> bool:
    """Return True when a class sits at the boundary of an educational level."""
    return class_name in LEVEL_EXIT_CLASSES


def _level_exit_message(class_name: str) -> str:
    hint = LEVEL_EXIT_HINTS.get(class_name, "no further promotion available at this level")
    return (
        f"Students in {class_name} cannot be promoted through this tool. "
        f"They must be {hint}."
    )


def _serialize_student(student, current_class, next_class, promoted=False):
    """
    Serialise a student for the promotion preview table.

    Key design: 'current_class' is the class the student was enrolled in
    when the query was made (i.e. the class passed in from the URL param),
    NOT Student.class_id which may already point to the promoted class.
    This keeps the UI consistent even after promotion.
    """
    is_exit = _is_level_exit(current_class.name) if current_class else False

    return {
        "id":               student.id,
        "student_code":     getattr(student, "student_code", None),
        "admission_number": getattr(student, "admission_number", None),
        "name":             f"{student.first_name} {student.last_name}",
        "current_class":    current_class.name if current_class else None,
        "next_class": (
            next_class.name if next_class
            else ("Level exit — use admission" if is_exit else "Graduating Class")
        ),
        "eligible":   next_class is not None and not is_exit,
        "promoted":   promoted,
        "level_exit": is_exit,
    }


def _serialize_discharged_student(student, enrollment, class_obj, stream_obj, year_obj):
    """Serialise a student for the level-exit history / graduated view."""
    return {
        "id":               student.id,
        "student_code":     student.student_code,
        "admission_number": student.admission_number,
        "name":             f"{student.first_name} {student.last_name}",
        "gender":           student.gender,
        "class_name":       class_obj.name  if class_obj  else None,
        "stream_name":      stream_obj.name if stream_obj else None,
        "academic_year":    year_obj.name   if year_obj   else None,
        "status":           enrollment.status,
        "status_label":     LEVEL_EXIT_LABELS.get(class_obj.name if class_obj else "", enrollment.status),
        "discharged_on":    enrollment.created_at.strftime("%d %b %Y") if enrollment.created_at else None,
    }


# ═══════════════════════════════════════════════════════════════
#  PAGE
#  GET /api/academics/progression/promote-students
# ═══════════════════════════════════════════════════════════════

@promotion_api.route("/promote-students", methods=["GET"])
@jwt_required()
def promote_students_page():

    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, user_id, modules = _get_context(claims)

    school, err = _school_or_404(school_id)
    if err:
        return err

    classes = (
        Class.query
        .filter_by(school_id=school_id)
        .order_by(Class.name)
        .all()
    )

    streams = (
        Stream.query
        .join(Class)
        .filter(Class.school_id == school_id)
        .options(joinedload(Stream.class_))
        .order_by(Class.name, Stream.name)
        .all()
    )

    academic_years = (
        AcademicYear.query
        .order_by(AcademicYear.name.desc())
        .all()
    )

    return render_template(
        "modules/academics/student_promotion.html",
        school=school,
        modules=modules,
        classes=classes,
        streams=streams,
        academic_years=academic_years,
    )


# ═══════════════════════════════════════════════════════════════
#  LOAD STUDENTS
#  GET /api/academics/progression/students
# ═══════════════════════════════════════════════════════════════

@promotion_api.route("/students", methods=["GET"])
@jwt_required()
def load_students():
    """
    Return students who CURRENTLY belong to the requested stream,
    as recorded in StudentStream — regardless of what Student.class_id
    currently says.  This means promoted students are no longer visible
    under their old stream, which is the correct behaviour.
    """
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    class_id  = request.args.get("class_id",  type=int)
    stream_id = request.args.get("stream_id", type=int)

    if not class_id or not stream_id:
        return jsonify({
            "message": "class_id and stream_id are required"
        }), 400

    current_class = Class.query.filter_by(
        id=class_id,
        school_id=school_id
    ).first()

    if not current_class:
        return jsonify({"message": "Class not found"}), 404

    stream = Stream.query.filter_by(id=stream_id).first()

    if not stream:
        return jsonify({"message": "Stream not found"}), 404

    # ── Level-exit early return ───────────────────────────────────────────────
    # Return the students still in the stream so the UI can show them
    # with a "Discharge from Level" action instead of "Promote".
    if _is_level_exit(current_class.name):
        student_streams = StudentStream.query.filter_by(
            school_id=school_id,
            stream_id=stream_id,
        ).all()

        student_ids = [s.student_id for s in student_streams]

        students = (
            Student.query
            .filter(Student.id.in_(student_ids))
            .order_by(Student.first_name, Student.last_name)
            .all()
        ) if student_ids else []

        data = []
        for student in students:
            # Check if already discharged (idempotent display)
            already_discharged = StudentEnrollment.query.filter(
                StudentEnrollment.school_id  == school_id,
                StudentEnrollment.student_id == student.id,
                StudentEnrollment.class_id   == current_class.id,
                StudentEnrollment.status.in_(["level_complete", "graduated"]),
            ).first()

            data.append({
                "id":               student.id,
                "student_code":     student.student_code,
                "admission_number": student.admission_number,
                "name":             f"{student.first_name} {student.last_name}",
                "current_class":    current_class.name,
                "next_class":       "Level exit — use admission",
                "eligible":         False,
                "level_exit":       True,
                "discharged":       already_discharged is not None,
                "discharge_status": already_discharged.status if already_discharged else None,
            })

        return jsonify({
            "students":          data,
            "next_class":        None,
            "level_exit":        True,
            "can_discharge":     True,
            "enrollment_status": LEVEL_EXIT_STATUSES.get(current_class.name),
            "message":           _level_exit_message(current_class.name),
        }), 200

    next_class = _get_next_class(current_class)

    # ── Students via StudentStream (source of truth for current placement) ───
    student_streams = StudentStream.query.filter_by(
        school_id=school_id,
        stream_id=stream_id,
    ).all()

    student_ids = [s.student_id for s in student_streams]

    students = (
        Student.query
        .filter(Student.id.in_(student_ids))
        .order_by(Student.first_name, Student.last_name)
        .all()
    ) if student_ids else []

    data = []

    for student in students:

        promoted = False

        if next_class:
            promoted = (
                StudentEnrollment.query.filter_by(
                    school_id=school_id,
                    student_id=student.id,
                    class_id=next_class.id,
                    status="promoted",
                ).first()
                is not None
            )

        data.append(
            _serialize_student(
                student=student,
                current_class=current_class,
                next_class=next_class,
                promoted=promoted,
            )
        )

    return jsonify({
        "students":   data,
        "next_class": next_class.name if next_class else None,
        "level_exit": False,
    }), 200


# ═══════════════════════════════════════════════════════════════
#  PROMOTE SINGLE STUDENT
#  POST /api/academics/progression/promote/<student_id>
# ═══════════════════════════════════════════════════════════════

@promotion_api.route(
    "/promote/<int:student_id>",
    methods=["POST"]
)
@jwt_required()
def promote_student(student_id):
    """
    Promote a single student to the next class within the same level.

    What changes after promotion
    ────────────────────────────
    1.  A new StudentEnrollment row is written for next_class (status="promoted").
    2.  StudentStream is updated: the old stream row is deleted, a new one
        pointing at next_stream is added.
    3.  Student.class_id is updated to next_class.id so that the student's
        "current" record reflects reality.

    What does NOT change
    ────────────────────
    • All historical StudentMark, Assessment, StudentAttendance, ReportCard,
      Invoice, Payment rows remain linked to their original term/year context.
      They are NOT keyed on Student.class_id, so they stay exactly where they
      were — belonging to the class the student was in at the time.
    • The old StudentEnrollment row (if one existed for current_class) is left
      intact so the history is preserved.

    Cross-level promotions are blocked here.  Students leaving KG3/Top, P7,
    S4, or S6 must go through a separate admission/enrolment flow.
    """
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    payload = request.get_json(force=True) or {}

    academic_year_id = payload.get("academic_year_id")
    next_stream_id   = payload.get("next_stream_id")
    current_class_id = payload.get("current_class_id")

    if not academic_year_id:
        return jsonify({"message": "academic_year_id is required"}), 400

    if not next_stream_id:
        return jsonify({"message": "next_stream_id is required"}), 400

    if not current_class_id:
        return jsonify({"message": "current_class_id is required"}), 400

    student = Student.query.filter_by(
        id=student_id,
        school_id=school_id
    ).first()

    if not student:
        return jsonify({"message": "Student not found"}), 404

    current_class = Class.query.filter_by(
        id=current_class_id,
        school_id=school_id
    ).first()

    if not current_class:
        return jsonify({"message": "Current class not found"}), 404

    if _is_level_exit(current_class.name):
        return jsonify({
            "message": _level_exit_message(current_class.name)
        }), 400

    next_class = _get_next_class(current_class)

    if not next_class:
        return jsonify({
            "message": (
                "This student is in a graduating class "
                "and cannot be promoted."
            )
        }), 400

    next_stream = Stream.query.filter_by(id=next_stream_id).first()

    if not next_stream:
        return jsonify({"message": "Next stream not found"}), 404

    if next_stream.class_id != next_class.id:
        return jsonify({
            "message": "Selected stream does not belong to the next class."
        }), 400

    existing = StudentEnrollment.query.filter_by(
        school_id=school_id,
        student_id=student.id,
        academic_year_id=academic_year_id,
        class_id=next_class.id,
        status="promoted",
    ).first()

    if existing:
        return jsonify({"message": "Student has already been promoted."}), 400

    try:
        enrollment = StudentEnrollment(
            school_id=school_id,
            student_id=student.id,
            academic_year_id=academic_year_id,
            class_id=next_class.id,
            stream_id=next_stream_id,
            status="promoted",
        )
        db.session.add(enrollment)

        student.class_id = next_class.id

        StudentStream.query.filter_by(
            school_id=school_id,
            student_id=student.id,
        ).delete()

        db.session.add(
            StudentStream(
                school_id=school_id,
                student_id=student.id,
                stream_id=next_stream_id,
            )
        )

        db.session.commit()

        logger.info(
            "Student %s promoted from %s → %s (stream %s)",
            student.id,
            current_class.name,
            next_class.name,
            next_stream_id,
        )

        return jsonify({
            "message":    "Student promoted successfully",
            "student_id": student.id,
            "from_class": current_class.name,
            "to_class":   next_class.name,
        }), 200

    except SQLAlchemyError as exc:
        db.session.rollback()
        logger.error("Promotion failed for student %s : %s", student.id, str(exc))
        return jsonify({"message": "Promotion failed"}), 500


# ═══════════════════════════════════════════════════════════════
#  PROMOTE ALL STUDENTS
#  POST /api/academics/progression/promote-all
# ═══════════════════════════════════════════════════════════════

@promotion_api.route(
    "/promote-all",
    methods=["POST"]
)
@jwt_required()
def promote_all_students():
    """
    Promote every student in a stream to the next class (same level only).

    See promote_student() for the data-preservation contract.
    Students who have already been promoted for this academic_year_id +
    next_class combination are silently skipped (idempotent).
    """
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    payload = request.get_json(force=True) or {}

    class_id         = payload.get("class_id")
    stream_id        = payload.get("stream_id")
    academic_year_id = payload.get("academic_year_id")
    next_stream_id   = payload.get("next_stream_id")

    if not class_id:
        return jsonify({"message": "class_id is required"}), 400

    if not stream_id:
        return jsonify({"message": "stream_id is required"}), 400

    if not academic_year_id:
        return jsonify({"message": "academic_year_id is required"}), 400

    if not next_stream_id:
        return jsonify({"message": "next_stream_id is required"}), 400

    current_class = Class.query.filter_by(
        id=class_id,
        school_id=school_id
    ).first()

    if not current_class:
        return jsonify({"message": "Class not found"}), 404

    if _is_level_exit(current_class.name):
        return jsonify({
            "message": _level_exit_message(current_class.name)
        }), 400

    next_class = _get_next_class(current_class)

    if not next_class:
        return jsonify({
            "message": "This class has no next class configured for promotion."
        }), 400

    next_stream = Stream.query.filter_by(id=next_stream_id).first()

    if not next_stream:
        return jsonify({"message": "Next stream not found"}), 404

    if next_stream.class_id != next_class.id:
        return jsonify({
            "message": "Selected stream does not belong to the next class."
        }), 400

    student_streams = StudentStream.query.filter_by(
        school_id=school_id,
        stream_id=stream_id,
    ).all()

    student_ids = [s.student_id for s in student_streams]

    students = (
        Student.query
        .filter(Student.id.in_(student_ids))
        .all()
    ) if student_ids else []

    if not students:
        return jsonify({"message": "No students found in this stream"}), 404

    promoted_count = 0
    skipped_count  = 0

    try:
        for student in students:

            existing = StudentEnrollment.query.filter_by(
                school_id=school_id,
                student_id=student.id,
                academic_year_id=academic_year_id,
                class_id=next_class.id,
                status="promoted",
            ).first()

            if existing:
                skipped_count += 1
                continue

            db.session.add(StudentEnrollment(
                school_id=school_id,
                student_id=student.id,
                academic_year_id=academic_year_id,
                class_id=next_class.id,
                stream_id=next_stream_id,
                status="promoted",
            ))

            student.class_id = next_class.id

            StudentStream.query.filter_by(
                school_id=school_id,
                student_id=student.id,
            ).delete()

            db.session.add(
                StudentStream(
                    school_id=school_id,
                    student_id=student.id,
                    stream_id=next_stream_id,
                )
            )

            promoted_count += 1

        db.session.commit()

        logger.info(
            "Bulk promotion: class=%s → %s | promoted=%d skipped=%d",
            current_class.name,
            next_class.name,
            promoted_count,
            skipped_count,
        )

        return jsonify({
            "message":    f"{promoted_count} student(s) promoted successfully",
            "promoted":   promoted_count,
            "skipped":    skipped_count,
            "from_class": current_class.name,
            "to_class":   next_class.name,
        }), 200

    except Exception as exc:
        db.session.rollback()
        logger.error(str(exc))
        return jsonify({"message": "Bulk promotion failed"}), 500


# ═══════════════════════════════════════════════════════════════
#  GRADUATE / DISCHARGE FROM LEVEL  (single or bulk)
#  POST /api/academics/progression/graduate-from-level
# ═══════════════════════════════════════════════════════════════

@promotion_api.route("/graduate-from-level", methods=["POST"])
@jwt_required()
def graduate_from_level():
    """
    Discharge students who have completed a level (KG3/Top, P7, S4, S6).

    These students cannot be promoted within the same level — they must
    either be admitted to the next level (P1, S1, S5) or are truly
    graduating (S6).  This endpoint:

    1.  Creates a StudentEnrollment row with status="level_complete"
        (or "graduated" for S6) to permanently record they finished
        this level.
    2.  Removes their StudentStream row so they vanish from class lists,
        attendance registers, and marks entry — they are "unclassed".
    3.  Sets Student.class_id = None so they no longer appear under any
        active class.  Historical marks, report cards, fees, and
        attendance records are keyed on student_id + term_id/year_id and
        are completely unaffected.

    Idempotent: re-submitting a student who is already discharged for
    the same class is a no-op (counted as skipped).

    Payload
    ───────
    {
        "class_id":         <int>,          # required
        "stream_id":        <int>,          # required
        "academic_year_id": <int>,          # required
        "student_ids":      [<int>, ...]    # optional — omit for whole stream
    }
    """
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    payload          = request.get_json(force=True) or {}
    class_id         = payload.get("class_id")
    stream_id        = payload.get("stream_id")
    academic_year_id = payload.get("academic_year_id")
    student_ids      = payload.get("student_ids")   # list or None (= whole stream)

    if not class_id:
        return jsonify({"message": "class_id is required"}), 400
    if not stream_id:
        return jsonify({"message": "stream_id is required"}), 400
    if not academic_year_id:
        return jsonify({"message": "academic_year_id is required"}), 400

    current_class = Class.query.filter_by(
        id=class_id,
        school_id=school_id
    ).first()

    if not current_class:
        return jsonify({"message": "Class not found"}), 404

    if not _is_level_exit(current_class.name):
        return jsonify({
            "message": (
                f"{current_class.name} is not a level-exit class. "
                "Use the standard promotion endpoint instead."
            )
        }), 400

    enrollment_status = LEVEL_EXIT_STATUSES[current_class.name]   # "level_complete" | "graduated"

    # ── Resolve which students to process ────────────────────────────────────
    if student_ids:
        students = (
            Student.query
            .filter(
                Student.id.in_(student_ids),
                Student.school_id == school_id,
            )
            .all()
        )
    else:
        # Whole stream
        ss_rows       = StudentStream.query.filter_by(
            school_id=school_id,
            stream_id=stream_id,
        ).all()
        ids_in_stream = [r.student_id for r in ss_rows]
        students      = (
            Student.query.filter(Student.id.in_(ids_in_stream)).all()
            if ids_in_stream else []
        )

    if not students:
        return jsonify({"message": "No students found to process"}), 404

    processed = 0
    skipped   = 0

    try:
        for student in students:

            # Idempotency: already discharged for this year + class?
            already = StudentEnrollment.query.filter_by(
                school_id=school_id,
                student_id=student.id,
                academic_year_id=academic_year_id,
                class_id=current_class.id,
                status=enrollment_status,
            ).first()

            if already:
                skipped += 1
                continue

            # 1. Record level completion / graduation
            db.session.add(StudentEnrollment(
                school_id=school_id,
                student_id=student.id,
                academic_year_id=academic_year_id,
                class_id=current_class.id,
                stream_id=stream_id,
                status=enrollment_status,
            ))

            # 2. Remove from stream → disappears from registers
            StudentStream.query.filter_by(
                school_id=school_id,
                student_id=student.id,
            ).delete()

            # 3. Clear class pointer → student is "unclassed" until
            #    admitted to the next level or truly graduated
            student.class_id = None

            processed += 1

        db.session.commit()

        label = "graduated" if enrollment_status == "graduated" else "discharged"

        logger.info(
            "Level-exit %s: class=%s | status=%s | processed=%d skipped=%d",
            label,
            current_class.name,
            enrollment_status,
            processed,
            skipped,
        )

        return jsonify({
            "message":   f"{processed} student(s) {label} from {current_class.name}",
            "status":    enrollment_status,
            "processed": processed,
            "skipped":   skipped,
            "class":     current_class.name,
        }), 200

    except SQLAlchemyError as exc:
        db.session.rollback()
        logger.error("Level-exit discharge failed: %s", str(exc))
        return jsonify({"message": "Operation failed"}), 500


# ═══════════════════════════════════════════════════════════════
#  UNDO DISCHARGE (single student)
#  POST /api/academics/progression/undo-discharge/<student_id>
# ═══════════════════════════════════════════════════════════════

@promotion_api.route("/undo-discharge/<int:student_id>", methods=["POST"])
@jwt_required()
def undo_discharge(student_id):
    """
    Reverse a level-exit discharge for a student who was discharged by mistake.

    Removes the level_complete / graduated enrollment record and
    re-inserts the student into their original stream, restoring their
    class_id so they reappear in day-to-day operations.

    Payload
    ───────
    {
        "class_id":         <int>,   # the class they were discharged from
        "stream_id":        <int>,   # the stream to put them back into
        "academic_year_id": <int>
    }
    """
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    payload          = request.get_json(force=True) or {}
    class_id         = payload.get("class_id")
    stream_id        = payload.get("stream_id")
    academic_year_id = payload.get("academic_year_id")

    if not class_id or not stream_id or not academic_year_id:
        return jsonify({"message": "class_id, stream_id, and academic_year_id are required"}), 400

    student = Student.query.filter_by(
        id=student_id,
        school_id=school_id
    ).first()

    if not student:
        return jsonify({"message": "Student not found"}), 404

    current_class = Class.query.filter_by(
        id=class_id,
        school_id=school_id
    ).first()

    if not current_class:
        return jsonify({"message": "Class not found"}), 404

    enrollment_status = LEVEL_EXIT_STATUSES.get(current_class.name)

    if not enrollment_status:
        return jsonify({"message": "This class is not a level-exit class"}), 400

    enrollment = StudentEnrollment.query.filter_by(
        school_id=school_id,
        student_id=student.id,
        academic_year_id=academic_year_id,
        class_id=current_class.id,
        status=enrollment_status,
    ).first()

    if not enrollment:
        return jsonify({"message": "No discharge record found for this student"}), 404

    stream = Stream.query.filter_by(id=stream_id).first()

    if not stream:
        return jsonify({"message": "Stream not found"}), 404

    try:
        # Remove discharge enrollment record
        db.session.delete(enrollment)

        # Restore stream
        StudentStream.query.filter_by(
            school_id=school_id,
            student_id=student.id,
        ).delete()

        db.session.add(StudentStream(
            school_id=school_id,
            student_id=student.id,
            stream_id=stream_id,
        ))

        # Restore class pointer
        student.class_id = current_class.id

        db.session.commit()

        logger.info(
            "Discharge undone for student %s — restored to class=%s stream=%s",
            student.id,
            current_class.name,
            stream_id,
        )

        return jsonify({
            "message":     "Discharge reversed successfully",
            "student_id":  student.id,
            "class":       current_class.name,
        }), 200

    except SQLAlchemyError as exc:
        db.session.rollback()
        logger.error("Undo discharge failed: %s", str(exc))
        return jsonify({"message": "Undo failed"}), 500


# ═══════════════════════════════════════════════════════════════
#  LEVEL-EXIT HISTORY  (graduated / level_complete students)
#  GET /api/academics/progression/level-exit-history
# ═══════════════════════════════════════════════════════════════

@promotion_api.route("/level-exit-history", methods=["GET"])
@jwt_required()
def level_exit_history():
    """
    Return all students who have been discharged from a level-exit class
    (status in: level_complete, graduated).

    Query params (all optional)
    ───────────────────────────
    academic_year_id  — filter by year
    class_id          — filter by the class they exited from
    status            — "level_complete" | "graduated" | "all" (default "all")
    search            — case-insensitive name / admission number filter
    page              — 1-based page number (default 1)
    per_page          — results per page (default 50, max 200)
    """
    claims = get_jwt()

    _, err = _staff_required(claims)
    if err:
        return err

    school_id, _, _ = _get_context(claims)

    academic_year_id = request.args.get("academic_year_id", type=int)
    class_id         = request.args.get("class_id",         type=int)
    status_filter    = request.args.get("status",           default="all")
    search           = request.args.get("search",           default="").strip()
    page             = request.args.get("page",    type=int, default=1)
    per_page         = min(request.args.get("per_page", type=int, default=50), 200)

    # Build base query — only level-exit statuses
    query = (
        db.session.query(StudentEnrollment, Student, Class, Stream, AcademicYear)
        .join(Student,      Student.id      == StudentEnrollment.student_id)
        .join(Class,        Class.id        == StudentEnrollment.class_id)
        .join(AcademicYear, AcademicYear.id == StudentEnrollment.academic_year_id)
        .outerjoin(Stream,  Stream.id       == StudentEnrollment.stream_id)
        .filter(
            StudentEnrollment.school_id == school_id,
            StudentEnrollment.status.in_(["level_complete", "graduated"]),
        )
    )

    if academic_year_id:
        query = query.filter(StudentEnrollment.academic_year_id == academic_year_id)

    if class_id:
        query = query.filter(StudentEnrollment.class_id == class_id)

    if status_filter in ("level_complete", "graduated"):
        query = query.filter(StudentEnrollment.status == status_filter)

    if search:
        like = f"%{search}%"
        query = query.filter(
            db.or_(
                Student.first_name.ilike(like),
                Student.last_name.ilike(like),
                Student.admission_number.ilike(like),
                Student.student_code.ilike(like),
            )
        )

    total  = query.count()
    offset = (page - 1) * per_page
    rows   = query.order_by(
        StudentEnrollment.created_at.desc()
    ).offset(offset).limit(per_page).all()

    data = [
        _serialize_discharged_student(student, enrollment, class_obj, stream_obj, year_obj)
        for enrollment, student, class_obj, stream_obj, year_obj in rows
    ]

    return jsonify({
        "students":   data,
        "total":      total,
        "page":       page,
        "per_page":   per_page,
        "pages":      (total + per_page - 1) // per_page,
    }), 200