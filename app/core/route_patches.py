"""
app/core/route_patches.py
==========================
Rate limit additions for reportcardgeneration.py and sendreportcards.py.

These two files are large and already well-structured.  Rather than
re-writing them in full, this module documents exactly which decorators
to add to each route.  The changes follow the same pattern used in all
other updated files.

HOW TO APPLY:
Add the @limiter.limit(...) decorator immediately after @jwt_required()
on each route listed below.

Import at the top of each file:
    from app.extensions import limiter
    from app.core.rate_limit import (
        READ_LIMIT, WRITE_LIMIT, REPORT_GEN_LIMIT,
        REPORT_ALL_LIMIT, BULK_LIMIT,
    )

─────────────────────────────────────────────────────────────
reportcardgeneration.py — route patches
─────────────────────────────────────────────────────────────

Route                                   Limit
──────────────────────────────────────────────────────────────────
GET  /report-cards                      READ_LIMIT
GET  /api/report-cards/students         READ_LIMIT
POST /api/report-cards/generate         REPORT_GEN_LIMIT
POST /api/report-cards/generate-all     REPORT_ALL_LIMIT
GET  /api/report_cards                  READ_LIMIT
DELETE /api/report-cards/<id>           WRITE_LIMIT
GET  /static/report_cards/<path>        READ_LIMIT
POST /api/school/details                WRITE_LIMIT
GET  /api/report-cards/<id>/download    READ_LIMIT

─────────────────────────────────────────────────────────────
sendreportcards.py — route patches
─────────────────────────────────────────────────────────────

Route                                        Limit
──────────────────────────────────────────────────────────────────
GET  /send-report-cards                      READ_LIMIT
GET  /api/send-report-cards/students         READ_LIMIT
POST /api/send-report-cards/push             BULK_LIMIT
GET  /api/send-report-cards/credentials      READ_LIMIT
DELETE /api/send-report-cards/revoke/<sid>   WRITE_LIMIT

─────────────────────────────────────────────────────────────
academics_api.py — route patches
─────────────────────────────────────────────────────────────

Route                                        Limit
──────────────────────────────────────────────────────────────────
GET  /api/academics/school/info              READ_LIMIT
GET  /api/academics/classes                  READ_LIMIT
POST /api/academics/classes                  WRITE_LIMIT
PUT  /api/academics/classes/<id>             WRITE_LIMIT
DELETE /api/academics/classes/<id>           WRITE_LIMIT
GET  /api/academics/classes/list             READ_LIMIT
PUT  /api/academics/streams/<id>             WRITE_LIMIT
DELETE /api/academics/streams/<id>           WRITE_LIMIT
GET  /api/academics/streams/<id>/detail      READ_LIMIT
GET  /api/academics/subjects                 READ_LIMIT
POST /api/academics/subjects                 WRITE_LIMIT
PUT  /api/academics/subjects/<id>            WRITE_LIMIT
DELETE /api/academics/subjects/<id>          WRITE_LIMIT
GET  /api/academics/subjects/list            READ_LIMIT
GET  /api/academics/subjects/<id>/detail     READ_LIMIT
GET  /api/academics/students                 SEARCH_LIMIT
PUT  /api/academics/students/<id>            WRITE_LIMIT
GET  /api/academics/students/<id>            READ_LIMIT
GET  /api/academics/assignments              READ_LIMIT
POST /api/academics/assignments              WRITE_LIMIT
PUT  /api/academics/assignments/<id>         WRITE_LIMIT
DELETE /api/academics/assignments/<id>       WRITE_LIMIT
GET  /api/academics/assignments/list         READ_LIMIT
GET  /api/academics/student                  READ_LIMIT
GET  /api/academics/student-attendance/filter READ_LIMIT
GET  /api/academics/staff-attendance        READ_LIMIT
GET  /api/academics/staff-attendance/filter  READ_LIMIT
POST /api/academics/staff-attendances        WRITE_LIMIT

─────────────────────────────────────────────────────────────
academics_api_2.py — route patches
─────────────────────────────────────────────────────────────

Route                                           Limit
──────────────────────────────────────────────────────────────────
GET  /api/academics2/marks-entry               READ_LIMIT
GET  /api/academics2/marks-entry/load          READ_LIMIT
GET  /api/academics2/marks-entry/student       READ_LIMIT
POST /api/academics2/marks-entry/save          MARKS_SAVE_LIMIT
GET  /api/academics2/marks-entry/saved         READ_LIMIT
GET  /api/academics2/grading-system-page       READ_LIMIT
GET  /api/academics2/grading-system            READ_LIMIT
POST /api/academics2/grading-system/add        WRITE_LIMIT
DELETE /api/academics2/grading-system/<id>     WRITE_LIMIT

─────────────────────────────────────────────────────────────
teachers_api.py — route patches
─────────────────────────────────────────────────────────────

Route                                           Limit
──────────────────────────────────────────────────────────────────
GET  /api/teachers/marks-entry                 READ_LIMIT
GET  /api/teachers/marks-entry/load            READ_LIMIT
POST /api/teachers/marks-entry/save            MARKS_SAVE_LIMIT
GET  /api/teachers/marks-entry/filter          READ_LIMIT
GET  /api/teachers/attendance                  READ_LIMIT
GET  /api/teachers/attendance/students         READ_LIMIT
POST /api/teachers/attendance/save             WRITE_LIMIT
GET  /api/teachers/attendance/history          READ_LIMIT

─────────────────────────────────────────────────────────────
alumni_api.py — route patches
─────────────────────────────────────────────────────────────

Route                                              Limit
──────────────────────────────────────────────────────────────────
GET  /api/academics/alumni/                        READ_LIMIT
GET  /api/academics/alumni/summary                 READ_LIMIT
GET  /api/academics/alumni/cohort                  SEARCH_LIMIT
GET  /api/academics/alumni/student/<id>/timeline   READ_LIMIT
GET  /api/academics/alumni/student/<id>/profile    READ_LIMIT
GET  /api/academics/alumni/student/<id>            READ_LIMIT

─────────────────────────────────────────────────────────────
studentsportal.py — route patches
─────────────────────────────────────────────────────────────

Route                              Limit
──────────────────────────────────────────────────────────────────
GET  /student/login                READ_LIMIT
GET  /student/portal               READ_LIMIT
GET  /student/report-file          "10 per minute"   (students view PDF)

─────────────────────────────────────────────────────────────
Also fix in these files: replace str(e) in except blocks
─────────────────────────────────────────────────────────────

In academics_api.py:
  - create_class except Exception: return jsonify({"message": str(e)})
    → log and return safe message

  - update_class except Exception: return jsonify({"message": str(e)})
    → log and return safe message

  - delete_class except Exception: return jsonify({"message": str(e)})
    → log and return safe message

  - update_stream except Exception: return jsonify({"message": str(e)})
    → log and return safe message

  - delete_stream except Exception: return jsonify({"message": str(e)})
    → log and return safe message

  - create_subject except Exception: return jsonify({"message": str(e)})
    → log and return safe message

  - update_subject except Exception: return jsonify({"message": str(e)})
    → log and return safe message

  - delete_subject except Exception: return jsonify({"message": str(e)})
    → log and return safe message

  - create_assignment: return jsonify({"message": str(e)})
    → log and return safe message

  - update_assignment: return jsonify({"message": str(e)})
    → log and return safe message

  - delete_assignment: return jsonify({"message": str(e)})
    → log and return safe message

  - save_staff_attendance: return jsonify({"message": str(e)})
    → log and return safe message

In academics_api_2.py:
  - save_student_marks: return jsonify({"message": str(exc)})
    → log and return safe message

  - add_grading_rule: return jsonify({"message": str(exc)})
    → log and return safe message

  - delete_grading_rule: return jsonify({"message": str(exc)})
    → log and return safe message

In teachers_api.py:
  - save_marks: return jsonify({"message": str(exc)})
    → log and return safe message

  - save_attendance: return jsonify({"message": str(exc)})
    → log and return safe message

Template for all replacements:
─────────────────────────────
    except Exception:
        db.session.rollback()            # if inside a DB transaction
        logger.exception(
            "route_name failed | context=%s", relevant_id
        )
        return jsonify({"message": "Operation failed. Please try again."}), 500
"""

# This file is documentation only — no executable code.
# Apply the patches described above to each listed file.