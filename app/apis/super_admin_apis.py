from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt
from app.models.core import School, Admin, Subscription,Notification,Blacklist
from app.models.people import Student
from app.models.academic_structure import Class
from app.models.user import User ,StudentAuth
from app.extensions import db
from app.utils.utilities import generate_school_code
from werkzeug.security import generate_password_hash

school_api = Blueprint("school_api", __name__)


# =========================================================
# SUPERADMIN GUARD HELPER
# =========================================================
def superadmin_required():
    claims = get_jwt()
    if claims.get("role") != "superadmin":
        return jsonify({"message": "Unauthorized. Superadmin access only."}), 403
    return None


# =========================================================
# GET ALL SCHOOLS
# =========================================================
@school_api.route("/api/schools", methods=["GET"])
@jwt_required()
def get_schools():

    guard = superadmin_required()
    if guard:
        return guard

    schools = School.query.all()

    return jsonify([
        {
            "id":             s.id,
            "name":           s.name,
            "school_type":    s.school_type,
            "status":         s.status,
            "motto":          s.motto,
            "address":        s.address,
            "plan":           s.subscription.payment_plan   if s.subscription else "—",
            "payment_status": s.subscription.payment_status if s.subscription else "—"
        }
        for s in schools
    ])


# =========================================================
# GET SINGLE SCHOOL
# =========================================================
@school_api.route("/api/schools/<int:id>", methods=["GET"])
@jwt_required()
def get_school(id):

    guard = superadmin_required()
    if guard:
        return guard

    school = School.query.get_or_404(id)

    admin = Admin.query.filter_by(school_id=school.id).first()

    return jsonify({
        "id":             school.id,
        "name":           school.name,
        "school_type":    school.school_type,
        "status":         school.status,
        "motto":          school.motto,
        "address":        school.address,
        "plan":           school.subscription.payment_plan   if school.subscription else "",
        "payment_status": school.subscription.payment_status if school.subscription else "",
        "username":       admin.username if admin else "",
        "contact":        admin.contact  if admin else ""
    })


# =========================================================
# CREATE SCHOOL
@school_api.route("/api/schools", methods=["POST"])
@jwt_required()
def create_school():

    print("========== CREATE SCHOOL STARTED ==========")

    guard = superadmin_required()

    if guard:
        print("Superadmin guard failed")
        return guard

    data = request.json

    print("Incoming data:", data)

    if not data:
        print("No data provided")
        return jsonify({
            "message": "No data provided"
        }), 400

    required = [
        "name",
        "school_type",
        "address",
        "username",
        "password"
    ]

    missing = [
        field for field in required
        if not data.get(field)
    ]

    if missing:

        print("Missing fields:", missing)

        return jsonify({
            "message": f"Missing required fields: {', '.join(missing)}"
        }), 400

    try:

        # =========================================
        # CHECK DUPLICATE USERNAME
        # =========================================

        print("Checking existing username...")

        existing_user = User.query.filter_by(
            username=data["username"]
        ).first()

        if existing_user:

            print("Username already exists")

            return jsonify({
                "message": "Username already exists"
            }), 400

        # =========================================
        # CREATE SCHOOL
        # =========================================

        print("Creating school...")

        school = School(
            name=data["name"],
            school_code=generate_school_code(),
            school_type=data["school_type"],
            motto=data.get("motto"),
            address=data.get("address"),
            status="active"
        )

        db.session.add(school)

        # Flush to get school.id
        db.session.flush()

        print(f"School created successfully. ID = {school.id}")

        # =========================================
        # HASH PASSWORD
        # =========================================

        print("Hashing password...")

        hashed_password = generate_password_hash(
            data["password"]
        )

        # =========================================
        # CREATE USER
        # =========================================

        print("Creating user...")

        user = User(
            school_id=school.id,
            username=data["username"],
            password_hash=hashed_password,
            role="admin",
            status="active"
        )

        db.session.add(user)

        print("User created successfully")

        # =========================================
        # CREATE ADMIN PROFILE
        # =========================================

        print("Creating admin profile...")

        admin = Admin(
            school_id=school.id,
            username=data["username"],
            contact=data.get("contact"),
            password_hash=hashed_password,
            status="active"
        )

        db.session.add(admin)

        print("Admin profile created successfully")

        # =========================================
        # CREATE SUBSCRIPTION
        # =========================================

        print("Creating subscription...")

        subscription = Subscription(
            school_id=school.id,
            payment_plan=data.get("plan", "Basic"),
            payment_status=data.get("payment_status", "Trial")
        )

        db.session.add(subscription)

        print("Subscription created successfully")

        # =========================================
        # CREATE DEFAULT CLASSES
        # =========================================

        school_type = data["school_type"].lower()

        print(f"Detected school type: {school_type}")

        if school_type == "secondary":

            class_names = [
                "S1",
                "S2",
                "S3",
                "S4",
                "S5",
                "S6"
            ]

        elif school_type == "primary":

            class_names = [
                "KG1",
                "KG2",
                "KG3",
                "P1",
                "P2",
                "P3",
                "P4",
                "P5",
                "P6",
                "P7"
            ]

        else:

            class_names = []

            print("Unknown school type")

        print("Creating classes...")

        for class_name in class_names:

            school_class = Class(
                name=class_name,
                school_id=school.id
            )

            db.session.add(school_class)

            print(f"Created class: {class_name}")

        # =========================================
        # FINAL COMMIT
        # =========================================

        print("Attempting final commit...")

        db.session.commit()

        print("========== TRANSACTION SUCCESS ==========")

        return jsonify({
            "message": "School created successfully",
            "school_id": school.id
        }), 201

    except Exception as e:

        print("========== TRANSACTION FAILED ==========")
        print(str(e))

        db.session.rollback()

        return jsonify({
            "message": f"Failed to create school: {str(e)}"
        }), 500
# =========================================================
# UPDATE SCHOOL
# =========================================================
@school_api.route("/api/schools/<int:id>", methods=["PUT"])
@jwt_required()
def update_school(id):

    guard = superadmin_required()
    if guard:
        return guard

    school = School.query.get_or_404(id)
    data   = request.json

    if not data:
        return jsonify({"message": "No data provided"}), 400

    try:
        # Update school fields
        school.name        = data.get("name",        school.name)
        school.school_type = data.get("school_type", school.school_type)
        school.motto       = data.get("motto",       school.motto)
        school.address     = data.get("address",     school.address)
        school.status      = data.get("status",      school.status)

        # Update subscription
        if school.subscription:
            school.subscription.payment_plan   = data.get("plan",           school.subscription.payment_plan)
            school.subscription.payment_status = data.get("payment_status", school.subscription.payment_status)
        else:
            sub = Subscription(
                school_id=school.id,
                payment_plan=data.get("plan", "Basic"),
                payment_status=data.get("payment_status", "Trial")
            )
            db.session.add(sub)

        # Update admin 
        
        admin = Admin.query.filter_by(school_id=school.id).first()
        user  = User.query.filter_by(school_id=school.id, role="admin").first()

        if admin:
            admin.username = data.get("username", admin.username)
            admin.contact  = data.get("contact",  admin.contact)

            if data.get("password"):
                admin.password_hash = generate_password_hash(data["password"])

        if user:
            if data.get("username"):
                user.username = data["username"]

            if data.get("password"):
                user.password_hash = generate_password_hash(data["password"])

        db.session.commit()

        return jsonify({"message": "School updated successfully"})

    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Failed to update school: {str(e)}"}), 500


# =========================================================
# DELETE SCHOOL
# =========================================================

@school_api.route("/api/schools/<int:id>", methods=["DELETE"])
@jwt_required()
def delete_school(id):
    # Enforce your custom superadmin authorization guard check
    guard = superadmin_required()
    if guard:
        return guard

    # Locate the target parent record instantly, or throw an automatic 404
    school = School.query.get_or_404(id)

    try:
        # =========================================================
        # STEP 1: LAYERED DEPENDENCY CLEANUP (REVERSE INTERCONNECTION ORDER)
        # =========================================================
        
        # 1. Clear out all Student Authentication records tied to this school code/id
        # (This matches your StudentAuth model from your login blueprint file)
        StudentAuth.query.filter_by(school_id=school.id).delete()
        #students
        Student.query.filter_by(school_id=school.id).delete()

        # 2. Clear out specialized Staff Admin table links explicitly
        Admin.query.filter_by(school_id=school.id).delete()

        # 3. Clear out core relational User accounts (Admins, Teachers, Staff profiles)
        User.query.filter_by(school_id=school.id).delete()

        # 4. Clear out financial profiles or subscription parameters securely
        if hasattr(school, 'subscription') and school.subscription:
            db.session.delete(school.subscription)
        else:
            # Fallback direct database query if relationship binding is unmapped
            Subscription.query.filter_by(school_id=school.id).delete()

        # 5. Clear out any academic module footprints if applicable to prevent ghost references
        # (Add execution deletions here if you have separate Academic/Module tables)

        # =========================================================
        # STEP 2: PARENT DESTROY SEQUENCE & TRANSACTION COMMIT
        # =========================================================
        db.session.delete(school)
        db.session.commit()

        print(f"🗑️ System Infrastructure: Completely scrubbed school cluster instance #{id}")
        return jsonify({
            "success": True,
            "message": "School and all associated system records deleted successfully."
        }), 200

    except Exception as e:
        # Roll back changes immediately if any single deletion step crashes
        db.session.rollback()
        print(f"❌ Structural Deletion Crash on school #{id}: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Failed to delete school ecosystem parameters cleanly: {str(e)}"
        }), 500


# =========================================================
# SEND NOTIFICATION
# =========================================================
@school_api.route("/api/notifications", methods=["POST"])
@jwt_required()
def send_notification():

    guard = superadmin_required()

    if guard:
        return guard

    data = request.get_json()

    if not data:
        return jsonify({
            "message": "No data provided"
        }), 400

    school_id = data.get("school_id")
    message = data.get("message")

    # =========================
    # VALIDATION
    # =========================
    if not school_id:
        return jsonify({
            "message": "School ID is required"
        }), 400

    if not message:
        return jsonify({
            "message": "Message is required"
        }), 400

    school = School.query.get(school_id)

    if not school:
        return jsonify({
            "message": "School not found"
        }), 404

    try:

        notification = Notification(
            school_id=school_id,
            message=message
        )

        db.session.add(notification)
        db.session.commit()

        return jsonify({
            "message": "Notification sent successfully"
        }), 201

    except Exception as e:

        db.session.rollback()

        return jsonify({
            "message": str(e)
        }), 500
        
  # =========================================================
# GET NOTIFICATIONS
# =========================================================
@school_api.route("/api/notifications", methods=["GET"])
@jwt_required()
def get_notifications():

    guard = superadmin_required()

    if guard:
        return guard

    notifications = Notification.query.order_by(
        Notification.created_at.desc()
    ).all()

    return jsonify([
        {
            "id": n.id,
            "school_id": n.school_id,
            "message": n.message,
            "date": n.created_at.strftime("%d %b %Y")
        }
        for n in notifications
    ])
    
    
 # =========================================================
# DELETE NOTIFICATION
# =========================================================
@school_api.route(
    "/api/notifications/<int:id>",
    methods=["DELETE"]
)
@jwt_required()
def delete_notification(id):

    guard = superadmin_required()

    if guard:
        return guard

    notification = Notification.query.get_or_404(id)

    try:

        db.session.delete(notification)
        db.session.commit()

        return jsonify({
            "message": "Notification deleted successfully"
        })

    except Exception as e:

        db.session.rollback()

        return jsonify({
            "message": str(e)
        }), 500
        
 

# =========================================================
# BLACKLIST A SCHOOL
# =========================================================
@school_api.route("/api/schools/<int:id>/blacklist", methods=["POST"])
@jwt_required()
def blacklist_school(id):

    guard = superadmin_required()
    if guard:
        return guard

    school = School.query.get_or_404(id)
    data   = request.get_json() or {}

    try:
        existing = Blacklist.query.filter_by(school_id=id).first()

        if existing:
            # Update reason instead of rejecting
            existing.reason     = data.get("reason", existing.reason)
            existing.created_at = db.func.now()
        else:
            entry = Blacklist(
                school_id=school.id,
                reason=data.get("reason", "No reason provided")
            )
            db.session.add(entry)

        # Suspend all Users tied to this school
        User.query.filter_by(school_id=school.id).update({"status": "suspended"})

        # Suspend all StudentAuth records tied to this school
        StudentAuth.query.filter_by(school_id=school.id).update({"status": "suspended"})

        # Mark the school itself as suspended
        school.status = "suspended"

        db.session.commit()

        return jsonify({"message": "School blacklisted successfully"}), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Failed to blacklist school: {str(e)}"}), 500


# =========================================================
# GET ALL BLACKLISTED SCHOOLS
# =========================================================
@school_api.route("/api/blacklist", methods=["GET"])
@jwt_required()
def get_blacklisted_schools():

    guard = superadmin_required()
    if guard:
        return guard

    entries = Blacklist.query.order_by(Blacklist.created_at.desc()).all()

    return jsonify([
        {
            "id":        e.id,
            "school_id": e.school_id,
            "name":      e.school.name if e.school else "—",
            "reason":    e.reason,
            "date":      e.created_at.strftime("%d %b %Y")
        }
        for e in entries
    ])


# =========================================================
# UNBLACKLIST A SCHOOL
# =========================================================
@school_api.route("/api/schools/<int:id>/blacklist", methods=["DELETE"])
@jwt_required()
def unblacklist_school(id):

    guard = superadmin_required()
    if guard:
        return guard

    school = School.query.get_or_404(id)
    entry  = Blacklist.query.filter_by(school_id=id).first()

    if not entry:
        return jsonify({"message": "School is not blacklisted"}), 404

    try:
        db.session.delete(entry)

        User.query.filter_by(school_id=school.id).update({"status": "active"})
        StudentAuth.query.filter_by(school_id=school.id).update({"status": "active"})

        school.status = "active"

        db.session.commit()

        return jsonify({"message": "School unblacklisted successfully"})

    except Exception as e:
        db.session.rollback()
        return jsonify({"message": f"Failed to unblacklist school: {str(e)}"}), 500  # was 50