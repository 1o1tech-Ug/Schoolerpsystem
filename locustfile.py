"""
locustfile.py  —  Load test for the School Management System
=============================================================

SETUP (run once before locust):
    pip install locust faker werkzeug sqlalchemy

    # Seed the test database
    python locustfile.py --seed

    # Then run locust
    locust -f locustfile.py --host=http://127.0.0.1:5000

USER MIX (reflects real-world usage):
    50%  Staff       — academics, marks, attendance, assignments
    25%  Admin       — terms, fee structures, staff, dashboard
    20%  Students    — portal, report file
     5%  Anonymous   — login pages, unauthenticated probes

NOTES:
    - All credentials drawn from seed pool written to locust_seed_data.json
    - JWTs are cookie-based (same as real browser)
    - Tasks are weighted to stress the heaviest routes
    - Failed logins are intentionally included to stress the auth layer
    - 404/403 responses on list endpoints are treated as success
      (route may exist but return empty for this school)
"""

from __future__ import annotations

import random
import sys
import os
import json
from datetime import date, datetime

from faker import Faker
from werkzeug.security import generate_password_hash

# ─────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────

DB_URL               = os.getenv("TEST_DB_URL", "sqlite:///instance/app.db")
BASE_URL             = "http://127.0.0.1:5000"

NUM_SCHOOLS          = 3
STAFF_PER_SCHOOL     = 10
STUDENTS_PER_SCHOOL  = 50
CLASSES_PER_SCHOOL   = 4
STREAMS_PER_CLASS    = 2

DEFAULT_STAFF_PASSWORD   = "StaffPass1!"
DEFAULT_STUDENT_PASSWORD = "StudentPass1!"

fake = Faker()


# ═════════════════════════════════════════════════════════════
#  SEED SCRIPT  —  python locustfile.py --seed
# ═════════════════════════════════════════════════════════════

def seed_database():
    print(f"[seed] Connecting to {DB_URL} ...")

    try:
        from app import create_app
        from app.extensions import db as _db
        from app.models.core import School
        from app.models.user import User, StudentAuth
        from app.models.people import Student, Staff
        from app.models.academic_structure import (
            AcademicYear, Term, AcademicConfig,
            Class, Stream, Subject,
            StudentStream, StudentSubject,
            StudentEnrollment,
        )
        from app.models.reportcards import ReportCard, SchoolDetail
    except ImportError as exc:
        print(f"[seed] Import error: {exc}")
        print("[seed] Run from your project root with venv active.")
        sys.exit(1)

    app = create_app()

    with app.app_context():

        seed_data = {"schools": [], "staff_users": [], "student_users": []}

        for s_idx in range(NUM_SCHOOLS):
            school_code = f"TESTSCH{s_idx+1:03d}"

            # ── School ───────────────────────────────────────
            school = School.query.filter_by(school_code=school_code).first()
            if not school:
                school = School(
                    name        = f"Test School {s_idx+1}",
                    school_code = school_code,
                    school_type = "secondary",
                    address     = fake.address(),
                    status      = "active",
                )
                _db.session.add(school)
                _db.session.flush()
                print(f"[seed]  School created: {school.name} id={school.id}")
            else:
                print(f"[seed]  School exists : {school.name} id={school.id}")

            # ── SchoolDetail ─────────────────────────────────
            from app.models.reportcards import SchoolDetail
            if not SchoolDetail.query.filter_by(school_id=school.id).first():
                _db.session.add(SchoolDetail(
                    school_id = school.id,
                    contact_1 = fake.phone_number()[:20],
                    district  = fake.city(),
                    email     = fake.company_email(),
                ))
                _db.session.flush()

            # ── Academic year + term ─────────────────────────
            ay = AcademicYear.query.filter_by(name="2024").first()
            if not ay:
                ay = AcademicYear(
                    name       = "2024",
                    start_date = date(2024, 1, 1),
                    end_date   = date(2024, 12, 31),
                    is_active  = True,
                )
                _db.session.add(ay)
                _db.session.flush()

            term = Term.query.filter_by(school_id=school.id, name="Term 1").first()
            if not term:
                term = Term(
                    academic_year_id = ay.id,
                    school_id        = school.id,
                    name             = "Term 1",
                    start_date       = date(2024, 1, 15),
                    end_date         = date(2024, 4, 15),
                    status           = "active",
                )
                _db.session.add(term)
                _db.session.flush()

            if not AcademicConfig.query.filter_by(school_id=school.id).first():
                _db.session.add(AcademicConfig(
                    school_id                = school.id,
                    current_academic_year_id = ay.id,
                    current_term_id          = term.id,
                ))
                _db.session.flush()

            # ── Classes + streams ────────────────────────────
            classes = []
            streams = []
            for c_idx in range(CLASSES_PER_SCHOOL):
                cls_name = f"S{c_idx+1}"
                cls = Class.query.filter_by(school_id=school.id, name=cls_name).first()
                if not cls:
                    cls = Class(name=cls_name, school_id=school.id)
                    _db.session.add(cls)
                    _db.session.flush()
                classes.append(cls)

                for st_idx in range(STREAMS_PER_CLASS):
                    st_name = chr(65 + st_idx)
                    st = Stream.query.filter_by(class_id=cls.id, name=st_name).first()
                    if not st:
                        st = Stream(name=st_name, class_id=cls.id, capacity=40)
                        _db.session.add(st)
                        _db.session.flush()
                    streams.append(st)

            # ── Subjects ─────────────────────────────────────
            subject_names = ["Mathematics", "English", "Physics", "Chemistry", "Biology"]
            subjects = []
            for sn in subject_names:
                subj = Subject.query.filter_by(school_id=school.id, name=sn).first()
                if not subj:
                    subj = Subject(
                        school_id   = school.id,
                        name        = sn,
                        level       = "O Level",
                        description = f"{sn} O Level",
                    )
                    _db.session.add(subj)
                    _db.session.flush()
                subjects.append(subj)

            # ── Staff + user accounts ─────────────────────────
            school_staff_creds = []
            for t_idx in range(STAFF_PER_SCHOOL):
                staff_code = f"ST{school.id:02d}{t_idx+1:03d}"
                staff = Staff.query.filter_by(
                    school_id=school.id, staff_code=staff_code
                ).first()
                if not staff:
                    staff = Staff(
                        school_id  = school.id,
                        staff_code = staff_code,
                        first_name = fake.first_name(),
                        last_name  = fake.last_name(),
                        gender     = random.choice(["Male", "Female"]),
                        phone      = fake.phone_number()[:20],
                        staff_type = "teaching",
                    )
                    _db.session.add(staff)
                    _db.session.flush()

                role     = "admin" if t_idx == 0 else "staff"
                username = f"staff_{school.id}_{t_idx+1}"
                user = User.query.filter_by(
                    school_id=school.id, username=username
                ).first()
                if not user:
                    user = User(
                        school_id     = school.id,
                        username      = username,
                        password_hash = generate_password_hash(DEFAULT_STAFF_PASSWORD),
                        role          = role,
                        staff_id      = staff.id,
                        status        = "active",
                    )
                    _db.session.add(user)
                    _db.session.flush()

                school_staff_creds.append({
                    "school_id": school.id,
                    "username":  username,
                    "password":  DEFAULT_STAFF_PASSWORD,
                    "role":      role,
                })

            seed_data["staff_users"].extend(school_staff_creds)

            # ── Students ──────────────────────────────────────
            school_student_creds = []
            for stu_idx in range(STUDENTS_PER_SCHOOL):
                student_code  = f"SC{school.id:02d}{stu_idx+1:04d}"
                admission_num = f"ADM{school.id:02d}{stu_idx+1:04d}"
                assigned_class  = random.choice(classes)
                assigned_stream = random.choice(streams)

                stu = Student.query.filter_by(
                    school_id=school.id, student_code=student_code
                ).first()
                if not stu:
                    stu = Student(
                        school_id        = school.id,
                        student_code     = student_code,
                        admission_number = admission_num,
                        first_name       = fake.first_name(),
                        last_name        = fake.last_name(),
                        gender           = random.choice(["Male", "Female"]),
                        date_of_birth    = fake.date_of_birth(
                            minimum_age=12, maximum_age=18
                        ),
                        student_type     = "day",
                        class_id         = assigned_class.id,
                    )
                    _db.session.add(stu)
                    _db.session.flush()

                    if not StudentStream.query.filter_by(
                        school_id=school.id, student_id=stu.id
                    ).first():
                        _db.session.add(StudentStream(
                            school_id  = school.id,
                            student_id = stu.id,
                            stream_id  = assigned_stream.id,
                        ))

                    for subj in subjects:
                        if not StudentSubject.query.filter_by(
                            school_id=school.id,
                            student_id=stu.id,
                            subject_id=subj.id,
                        ).first():
                            _db.session.add(StudentSubject(
                                school_id  = school.id,
                                student_id = stu.id,
                                subject_id = subj.id,
                            ))

                    if not StudentEnrollment.query.filter_by(
                        school_id=school.id, student_id=stu.id
                    ).first():
                        _db.session.add(StudentEnrollment(
                            school_id        = school.id,
                            student_id       = stu.id,
                            academic_year_id = ay.id,
                            class_id         = assigned_class.id,
                            stream_id        = assigned_stream.id,
                            status           = "active",
                        ))
                    _db.session.flush()

                # StudentAuth
                auth = StudentAuth.query.filter_by(
                    school_id=school.id, student_id=stu.id
                ).first()
                if not auth:
                    auth = StudentAuth(
                        school_id     = school.id,
                        student_id    = stu.id,
                        password_hash = generate_password_hash(DEFAULT_STUDENT_PASSWORD),
                        term_id       = term.id,
                        status        = "active",
                    )
                    _db.session.add(auth)
                    _db.session.flush()

                # Minimal ReportCard so portal has something to show
                rc = ReportCard.query.filter_by(
                    school_id  = school.id,
                    student_id = stu.id,
                    term_id    = term.id,
                    exam_type  = "EOT",
                ).first()
                if not rc:
                    _db.session.add(ReportCard(
                        school_id     = school.id,
                        student_id    = stu.id,
                        term_id       = term.id,
                        exam_type     = "EOT",
                        academic_year = "2024",
                        status        = "generated",
                        firebase_url  = "/static/report_cards/sample.html",
                    ))
                    _db.session.flush()

                school_student_creds.append({
                    "school_id":    school.id,
                    "student_code": student_code,
                    "password":     DEFAULT_STUDENT_PASSWORD,
                    "stream_id":    assigned_stream.id,
                    "term_id":      term.id,
                    "class_id":     assigned_class.id,
                })

            seed_data["student_users"].extend(school_student_creds)
            seed_data["schools"].append({
                "id":          school.id,
                "school_code": school_code,
                "term_id":     term.id,
                "ay_id":       ay.id,
                "stream_ids":  [s.id for s in streams],
                "class_ids":   [c.id for c in classes],
                "subject_ids": [s.id for s in subjects],
            })

        _db.session.commit()

        print(f"\n[seed] ✓ Done.")
        print(f"[seed]   Schools  : {NUM_SCHOOLS}")
        print(f"[seed]   Staff    : {len(seed_data['staff_users'])}")
        print(f"[seed]   Students : {len(seed_data['student_users'])}")

        with open("locust_seed_data.json", "w") as f:
            json.dump(seed_data, f, indent=2)
        print("[seed]   Credentials → locust_seed_data.json")


# ═════════════════════════════════════════════════════════════
#  CREDENTIAL POOL  —  loaded at locust startup
# ═════════════════════════════════════════════════════════════

def _load_seed_data() -> dict:
    path = "locust_seed_data.json"
    if not os.path.exists(path):
        print("[locust] locust_seed_data.json not found. Run: python locustfile.py --seed")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)


# ═════════════════════════════════════════════════════════════
#  LOCUST SETUP
# ═════════════════════════════════════════════════════════════

from locust import HttpUser, TaskSet, task, between, events
from locust.exception import StopUser

STAFF_CREDS:   list[dict] = []
STUDENT_CREDS: list[dict] = []
SCHOOLS:       list[dict] = []


@events.init.add_listener
def on_locust_init(environment, **kwargs):
    global STAFF_CREDS, STUDENT_CREDS, SCHOOLS
    data          = _load_seed_data()
    STAFF_CREDS   = data["staff_users"]
    STUDENT_CREDS = data["student_users"]
    SCHOOLS       = data["schools"]
    print(
        f"[locust] {len(STAFF_CREDS)} staff + "
        f"{len(STUDENT_CREDS)} students across "
        f"{len(SCHOOLS)} schools loaded."
    )


# ─────────────────────────────────────────────────────────────
#  AUTH HELPERS
# ─────────────────────────────────────────────────────────────

def _staff_login(client, cred: dict) -> bool:
    with client.post(
        "/auth/staff/login",
        json={
            "school_id": cred["school_id"],
            "username":  cred["username"],
            "password":  cred["password"],
        },
        name="/auth/staff/login",
        catch_response=True,
    ) as r:
        if r.status_code == 200:
            r.success()
            return True
        r.failure(f"{r.status_code}: {r.text[:100]}")
        return False


def _student_login(client, cred: dict) -> bool:
    with client.post(
        "/auth/student/login",
        json={
            "school_id":    cred["school_id"],
            "student_code": cred["student_code"],
            "password":     cred["password"],
        },
        name="/auth/student/login",
        catch_response=True,
    ) as r:
        if r.status_code == 200:
            r.success()
            return True
        r.failure(f"{r.status_code}: {r.text[:100]}")
        return False


def _logout(client):
    client.post("/auth/logout", name="/auth/logout")


def _ok(r, name=""):
    """Accept 200/302; mark 404/403 as success (expected for missing data)."""
    if r.status_code in (200, 302, 404, 403):
        r.success()
    else:
        r.failure(f"{name} → {r.status_code}")


# ═════════════════════════════════════════════════════════════
#  STAFF BEHAVIOUR  (50 %)
#  Covers: academics (classes, subjects, assignments, marks,
#  attendance), report cards, finance helpers
# ═════════════════════════════════════════════════════════════

class StaffBehavior(TaskSet):

    def on_start(self):
        self.cred = random.choice(STAFF_CREDS)
        if not _staff_login(self.client, self.cred):
            raise StopUser()
        self.school_id = self.cred["school_id"]
        school         = next((s for s in SCHOOLS if s["id"] == self.school_id), None)
        self.school    = school or {}
        self.stream_ids  = school.get("stream_ids", [1]) if school else [1]
        self.class_ids   = school.get("class_ids",  [1]) if school else [1]
        self.subject_ids = school.get("subject_ids",[1]) if school else [1]
        self.term_id     = school.get("term_id",     1)  if school else 1

    def on_stop(self):
        _logout(self.client)

    # ── Academics: classes ────────────────────────────────────

    @task(4)
    def classes_page(self):
        with self.client.get(
            "/api/academics/classes",
            name="/api/academics/classes [page]",
            catch_response=True,
        ) as r:
            _ok(r, "classes_page")

    @task(3)
    def list_classes_json(self):
        with self.client.get(
            "/api/academics/classes/list",
            name="/api/academics/classes/list",
            catch_response=True,
        ) as r:
            _ok(r, "list_classes_json")

    # ── Academics: subjects ───────────────────────────────────

    @task(4)
    def subjects_page(self):
        with self.client.get(
            "/api/academics/subjects",
            name="/api/academics/subjects [page]",
            catch_response=True,
        ) as r:
            _ok(r, "subjects_page")

    @task(3)
    def list_subjects_json(self):
        with self.client.get(
            "/api/academics/subjects/list",
            name="/api/academics/subjects/list",
            catch_response=True,
        ) as r:
            _ok(r, "list_subjects_json")

    @task(2)
    def subject_detail(self):
        sid = random.choice(self.subject_ids)
        with self.client.get(
            f"/api/academics/subjects/{sid}/detail",
            name="/api/academics/subjects/[id]/detail",
            catch_response=True,
        ) as r:
            _ok(r, "subject_detail")

    # ── Academics: students ───────────────────────────────────

    @task(4)
    def student_list_page(self):
        with self.client.get(
            "/api/academics/students",
            name="/api/academics/students [page]",
            catch_response=True,
        ) as r:
            _ok(r, "student_list_page")

    @task(2)
    def student_list_search(self):
        with self.client.get(
            "/api/academics/students?search=a",
            name="/api/academics/students [search]",
            catch_response=True,
        ) as r:
            _ok(r, "student_list_search")

    @task(2)
    def student_list_filter_class(self):
        cid = random.choice(self.class_ids)
        with self.client.get(
            f"/api/academics/students?class_id={cid}",
            name="/api/academics/students [filter class]",
            catch_response=True,
        ) as r:
            _ok(r, "student_list_filter_class")

    # ── Academics: assignments ────────────────────────────────

    @task(3)
    def assignments_page(self):
        with self.client.get(
            "/api/academics/assignments",
            name="/api/academics/assignments [page]",
            catch_response=True,
        ) as r:
            _ok(r, "assignments_page")

    @task(2)
    def list_assignments_json(self):
        with self.client.get(
            "/api/academics/assignments/list",
            name="/api/academics/assignments/list",
            catch_response=True,
        ) as r:
            _ok(r, "list_assignments_json")

    @task(1)
    def list_assignments_by_stream(self):
        sid = random.choice(self.stream_ids)
        with self.client.get(
            f"/api/academics/assignments/list?stream_id={sid}",
            name="/api/academics/assignments/list [by stream]",
            catch_response=True,
        ) as r:
            _ok(r, "list_assignments_by_stream")

    # ── Academics: school info ────────────────────────────────

    @task(1)
    def school_info(self):
        with self.client.get(
            "/api/academics/school/info",
            name="/api/academics/school/info",
            catch_response=True,
        ) as r:
            _ok(r, "school_info")

    # ── Academics: attendance ─────────────────────────────────

    @task(3)
    def student_attendance_page(self):
        with self.client.get(
            "/api/academics/student",
            name="/api/academics/student [attendance page]",
            catch_response=True,
        ) as r:
            _ok(r, "student_attendance_page")

    @task(2)
    def filter_student_attendance(self):
        cid = random.choice(self.class_ids)
        with self.client.get(
            f"/api/academics/student-attendance/filter?class_id={cid}",
            name="/api/academics/student-attendance/filter",
            catch_response=True,
        ) as r:
            _ok(r, "filter_student_attendance")

    @task(3)
    def staff_attendance_page(self):
        with self.client.get(
            "/api/academics/staff-attendance",
            name="/api/academics/staff-attendance [page]",
            catch_response=True,
        ) as r:
            _ok(r, "staff_attendance_page")

    @task(2)
    def filter_staff_attendance(self):
        with self.client.get(
            "/api/academics/staff-attendance/filter",
            name="/api/academics/staff-attendance/filter",
            catch_response=True,
        ) as r:
            _ok(r, "filter_staff_attendance")

    # ── Academics2: marks entry ───────────────────────────────

    @task(4)
    def marks_entry_page(self):
        with self.client.get(
            "/api/academics2/marks-entry",
            name="/api/academics2/marks-entry [page]",
            catch_response=True,
        ) as r:
            _ok(r, "marks_entry_page")

    @task(4)
    def load_marks_students(self):
        sid       = random.choice(self.stream_ids)
        exam_type = random.choice(["BOT", "MID", "EOT"])
        with self.client.get(
            f"/api/academics2/marks-entry/load"
            f"?stream_id={sid}&term_id={self.term_id}&exam_type={exam_type}",
            name="/api/academics2/marks-entry/load",
            catch_response=True,
        ) as r:
            _ok(r, "load_marks_students")

    @task(3)
    def load_saved_marks(self):
        sid       = random.choice(self.stream_ids)
        exam_type = random.choice(["BOT", "MID", "EOT"])
        with self.client.get(
            f"/api/academics2/marks-entry/saved"
            f"?stream_id={sid}&term_id={self.term_id}&exam_type={exam_type}",
            name="/api/academics2/marks-entry/saved",
            catch_response=True,
        ) as r:
            _ok(r, "load_saved_marks")

    # ── Academics2: grading ───────────────────────────────────

    @task(2)
    def grading_system_page(self):
        with self.client.get(
            "/api/academics2/grading-system-page",
            name="/api/academics2/grading-system-page",
            catch_response=True,
        ) as r:
            _ok(r, "grading_system_page")

    @task(2)
    def list_grading_system(self):
        with self.client.get(
            "/api/academics2/grading-system",
            name="/api/academics2/grading-system",
            catch_response=True,
        ) as r:
            _ok(r, "list_grading_system")

    # ── Report cards ──────────────────────────────────────────

    @task(3)
    def report_cards_page(self):
        with self.client.get(
            "/report-cards",
            name="/report-cards [page]",
            catch_response=True,
        ) as r:
            _ok(r, "report_cards_page")

    @task(3)
    def get_students_for_generation(self):
        sid       = random.choice(self.stream_ids)
        exam_type = random.choice(["BOT", "MID", "EOT"])
        with self.client.get(
            f"/api/report-cards/students"
            f"?stream_id={sid}&term_id={self.term_id}&exam_type={exam_type}",
            name="/api/report-cards/students",
            catch_response=True,
        ) as r:
            _ok(r, "get_students_for_generation")

    @task(1)
    def get_report_cards(self):
        sid       = random.choice(self.stream_ids)
        ay_id     = self.school.get("ay_id", 1)
        exam_type = random.choice(["BOT", "MID", "EOT"])
        with self.client.get(
            f"/api/report_cards"
            f"?stream_id={sid}&term_id={self.term_id}"
            f"&academic_year_id={ay_id}&exam_type={exam_type}",
            name="/api/report_cards [list]",
            catch_response=True,
        ) as r:
            _ok(r, "get_report_cards")

    # ── Token refresh ─────────────────────────────────────────

    @task(1)
    def refresh_token(self):
        with self.client.post(
            "/auth/refresh",
            name="/auth/refresh",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 401, 422):
                r.success()
            else:
                r.failure(f"refresh → {r.status_code}")

    # ── Bad login probe ───────────────────────────────────────

    @task(1)
    def bad_login(self):
        cred = random.choice(STAFF_CREDS)
        with self.client.post(
            "/auth/staff/login",
            json={
                "school_id": cred["school_id"],
                "username":  cred["username"],
                "password":  "wrongpassword",
            },
            name="/auth/staff/login [bad creds]",
            catch_response=True,
        ) as r:
            if r.status_code == 401:
                r.success()
            else:
                r.failure(f"Expected 401, got {r.status_code}")


class StaffUser(HttpUser):
    tasks     = [StaffBehavior]
    weight    = 50
    wait_time = between(1, 5)


# ═════════════════════════════════════════════════════════════
#  ADMIN BEHAVIOUR  (25 %)
#  Covers: terms, fee structures, staff CRUD, dashboard,
#  roles/permissions, system settings
# ═════════════════════════════════════════════════════════════

class AdminBehavior(TaskSet):

    def on_start(self):
        admin_creds = [c for c in STAFF_CREDS if c["role"] == "admin"]
        if not admin_creds:
            raise StopUser()
        self.cred = random.choice(admin_creds)
        if not _staff_login(self.client, self.cred):
            raise StopUser()
        self.school_id = self.cred["school_id"]

    def on_stop(self):
        _logout(self.client)

    # ── Dashboard ─────────────────────────────────────────────

    @task(5)
    def dashboard_page(self):
        with self.client.get(
            "/admin/dashboard",
            name="/admin/dashboard [page]",
            catch_response=True,
        ) as r:
            _ok(r, "dashboard_page")

    @task(4)
    def dashboard_api(self):
        with self.client.get(
            "/admin/api/dashboard",
            name="/admin/api/dashboard",
            catch_response=True,
        ) as r:
            _ok(r, "dashboard_api")

    # ── Terms ─────────────────────────────────────────────────

    @task(4)
    def terms_page(self):
        with self.client.get(
            "/admin/terms",
            name="/admin/terms [page]",
            catch_response=True,
        ) as r:
            _ok(r, "terms_page")

    @task(4)
    def get_terms(self):
        with self.client.get(
            "/admin/api/terms",
            name="/admin/api/terms [list]",
            catch_response=True,
        ) as r:
            _ok(r, "get_terms")

    # ── Classes ───────────────────────────────────────────────

    @task(2)
    def get_classes(self):
        with self.client.get(
            "/admin/api/classes",
            name="/admin/api/classes",
            catch_response=True,
        ) as r:
            _ok(r, "get_classes")

    # ── Fee structures ────────────────────────────────────────

    @task(4)
    def fee_structures_page(self):
        with self.client.get(
            "/admin/fee-structures",
            name="/admin/fee-structures [page]",
            catch_response=True,
        ) as r:
            _ok(r, "fee_structures_page")

    @task(3)
    def get_fee_structures(self):
        with self.client.get(
            "/admin/api/fee-structures",
            name="/admin/api/fee-structures [list]",
            catch_response=True,
        ) as r:
            _ok(r, "get_fee_structures")

    # ── Staff ─────────────────────────────────────────────────

    @task(3)
    def staff_profiles_page(self):
        with self.client.get(
            "/admin/staff/profiles",
            name="/admin/staff/profiles [page]",
            catch_response=True,
        ) as r:
            _ok(r, "staff_profiles_page")

    @task(3)
    def get_staff(self):
        with self.client.get(
            "/admin/api/staff",
            name="/admin/api/staff [list]",
            catch_response=True,
        ) as r:
            _ok(r, "get_staff")

    # ── Roles & permissions ───────────────────────────────────

    @task(2)
    def roles_permissions_page(self):
        with self.client.get(
            "/admin/roles-permissions",
            name="/admin/roles-permissions [page]",
            catch_response=True,
        ) as r:
            _ok(r, "roles_permissions_page")

    # ── System settings ───────────────────────────────────────

    @task(1)
    def system_settings_page(self):
        with self.client.get(
            "/admin/system-settings",
            name="/admin/system-settings [page]",
            catch_response=True,
        ) as r:
            _ok(r, "system_settings_page")

    # ── Token refresh ─────────────────────────────────────────

    @task(1)
    def refresh_token(self):
        with self.client.post(
            "/auth/refresh",
            name="/auth/refresh",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 401, 422):
                r.success()
            else:
                r.failure(f"refresh → {r.status_code}")


class AdminUser(HttpUser):
    tasks     = [AdminBehavior]
    weight    = 25
    wait_time = between(1, 4)


# ═════════════════════════════════════════════════════════════
#  STUDENT BEHAVIOUR  (20 %)
#  Covers: portal, report file, bad-password probe
# ═════════════════════════════════════════════════════════════

class StudentBehavior(TaskSet):

    def on_start(self):
        self.cred = random.choice(STUDENT_CREDS)
        if not _student_login(self.client, self.cred):
            raise StopUser()

    def on_stop(self):
        _logout(self.client)

    @task(5)
    def view_portal(self):
        with self.client.get(
            "/student/portal",
            name="/student/portal",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 302):
                r.success()
            else:
                r.failure(f"portal → {r.status_code}")

    @task(3)
    def download_report(self):
        with self.client.get(
            "/student/report-file",
            name="/student/report-file",
            catch_response=True,
        ) as r:
            # 404 = no file on disk yet (expected in test env)
            if r.status_code in (200, 404):
                r.success()
            else:
                r.failure(f"report-file → {r.status_code}")

    @task(1)
    def refresh_token(self):
        with self.client.post(
            "/auth/refresh",
            name="/auth/refresh",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 401, 422):
                r.success()
            else:
                r.failure(f"refresh → {r.status_code}")

    @task(1)
    def bad_login(self):
        cred = random.choice(STUDENT_CREDS)
        with self.client.post(
            "/auth/student/login",
            json={
                "school_id":    cred["school_id"],
                "student_code": cred["student_code"],
                "password":     "wrongpassword",
            },
            name="/auth/student/login [bad creds]",
            catch_response=True,
        ) as r:
            if r.status_code == 401:
                r.success()
            else:
                r.failure(f"Expected 401, got {r.status_code}")


class StudentUser(HttpUser):
    tasks     = [StudentBehavior]
    weight    = 20
    wait_time = between(3, 10)


# ═════════════════════════════════════════════════════════════
#  ANONYMOUS BEHAVIOUR  (5 %)
#  Simulates unauthenticated traffic and malformed requests
# ═════════════════════════════════════════════════════════════

class AnonymousBehavior(TaskSet):

    @task(3)
    def staff_login_page(self):
        with self.client.get(
            "/auth/login",
            name="/auth/login [page]",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 302):
                r.success()
            else:
                r.failure(f"{r.status_code}")

    @task(2)
    def student_login_page(self):
        with self.client.get(
            "/student/login",
            name="/student/login [page]",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 302):
                r.success()
            else:
                r.failure(f"{r.status_code}")

    @task(2)
    def hit_portal_without_token(self):
        with self.client.get(
            "/student/portal",
            name="/student/portal [no token]",
            catch_response=True,
        ) as r:
            # Should redirect or return 401
            if r.status_code in (200, 302, 401):
                r.success()
            else:
                r.failure(f"{r.status_code}")

    @task(1)
    def hit_api_without_token(self):
        with self.client.get(
            "/api/academics/classes/list",
            name="/api/academics/classes/list [no token]",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 401, 422):
                r.success()
            else:
                r.failure(f"{r.status_code}")

    @task(1)
    def missing_fields_login(self):
        with self.client.post(
            "/auth/staff/login",
            json={"school_id": 1},
            name="/auth/staff/login [missing fields]",
            catch_response=True,
        ) as r:
            if r.status_code == 400:
                r.success()
            else:
                r.failure(f"Expected 400, got {r.status_code}")


class AnonymousUser(HttpUser):
    tasks     = [AnonymousBehavior]
    weight    = 5
    wait_time = between(3, 10)


# ═════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--seed" in sys.argv:
        seed_database()
    else:
        print("Usage:")
        print("  python locustfile.py --seed          # seed DB first")
        print("  locust -f locustfile.py --host=http://127.0.0.1:5000")