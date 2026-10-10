import os
import re
import uuid
import logging
from pathlib import Path
from urllib.parse import urlparse
from functools import wraps
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    send_from_directory,
    abort,
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from flask_wtf.csrf import CSRFProtect
from PIL import Image

from config import Config
import database
from services.gemma import analyze_structural_report
from services.earthquake import get_recent_earthquakes

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config.from_object(Config)

# Ensure SQLite DB is initialized with schema and migrations
database.init_db()

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to access this page."
login_manager.login_message_category = "warning"

# Initialize CSRF Protection
csrf = CSRFProtect(app)


class User(UserMixin):
    """Flask-Login User model wrapper for SQLite records."""

    def __init__(self, id: str, email: str, role: str, created_at: str = None):
        self.id = id
        self.email = email
        self.role = role
        self.created_at = created_at

    @property
    def is_engineer(self) -> bool:
        return self.role == "engineer"

    @property
    def is_regular_user(self) -> bool:
        return self.role == "user"


@login_manager.user_loader
def load_user(user_id: str):
    """Load user by user_id for Flask-Login session."""
    u = database.get_user_by_id(user_id)
    if not u:
        return None
    return User(id=u["id"], email=u["email"], role=u["role"], created_at=u.get("created_at"))


EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_safe_redirect_url(target: str) -> bool:
    """Ensure target redirect URL is a safe local relative path to avoid open redirects."""
    if not target:
        return False
    parsed = urlparse(target)
    return parsed.scheme == "" and parsed.netloc == "" and target.startswith("/") and not target.startswith("//")


def role_required(*allowed_roles):
    """Decorator to enforce role-based access control on routes."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role not in allowed_roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def is_allowed_file(filename: str) -> bool:
    """Verify file extension is in allowed set."""
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in Config.ALLOWED_EXTENSIONS


def validate_and_save_image(file_storage) -> tuple[str, str, int, str]:
    """
    Validate uploaded image:
    - Check allowed extension
    - Verify actual image content using Pillow
    - Generate unique safe filename
    Returns (saved_filename, original_filename, file_size, mime_type)
    """
    if not file_storage or not file_storage.filename:
        raise ValueError("No file provided.")

    original_filename = secure_filename(file_storage.filename) or "photo.jpg"
    if not is_allowed_file(original_filename):
        raise ValueError(
            f"Invalid file type '{original_filename}'. Allowed formats: {', '.join(Config.ALLOWED_EXTENSIONS)}"
        )

    ext = original_filename.rsplit(".", 1)[1].lower() if "." in original_filename else "jpg"
    unique_filename = f"{uuid.uuid4().hex}.{ext}"
    dest_path = Path(Config.UPLOAD_FOLDER) / unique_filename

    file_storage.save(dest_path)
    file_size = dest_path.stat().st_size

    try:
        with Image.open(dest_path) as img:
            img.verify()
            mime_type = Image.MIME.get(img.format, f"image/{ext}")
    except Exception as e:
        if dest_path.exists():
            dest_path.unlink()
        raise ValueError(f"Uploaded file '{original_filename}' is corrupted or not a valid image: {e}")

    return unique_filename, original_filename, file_size, mime_type


# ---------------------------------------------------------
# Authentication Routes
# ---------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    """
    Registration Page:
    Allows visitors to create a User or Engineer account.
    """
    if current_user.is_authenticated:
        if current_user.role == "engineer":
            return redirect(url_for("engineer_view"))
        return redirect(url_for("index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        role = (request.form.get("role") or "user").strip().lower()

        # Validation
        if not email or not EMAIL_REGEX.match(email):
            flash("Please enter a valid email address.", "danger")
            return render_template("register.html", email=email, role=role), 400

        if len(password) < 8:
            flash("Password must be at least 8 characters long.", "danger")
            return render_template("register.html", email=email, role=role), 400

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("register.html", email=email, role=role), 400

        if role not in database.ALLOWED_ROLES:
            flash("Invalid account type selected.", "danger")
            return render_template("register.html", email=email, role=role), 400

        existing = database.get_user_by_email(email)
        if existing:
            flash("An account with this email address already exists.", "danger")
            return render_template("register.html", email=email, role=role), 400

        password_hash = generate_password_hash(password)
        user_id = database.create_user(email=email, password_hash=password_hash, role=role)
        created_user = database.get_user_by_id(user_id)
        user_obj = User(
            id=created_user["id"],
            email=created_user["email"],
            role=created_user["role"],
            created_at=created_user.get("created_at"),
        )
        login_user(user_obj)
        flash(f"Account registered successfully as {role.capitalize()}.", "success")

        if role == "engineer":
            return redirect(url_for("engineer_view"))
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    Login Page:
    Authenticates user and redirects to their role-specific interface.
    """
    if current_user.is_authenticated:
        if current_user.role == "engineer":
            return redirect(url_for("engineer_view"))
        return redirect(url_for("index"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        next_url = request.form.get("next") or request.args.get("next")

        user_dict = database.get_user_by_email(email)
        if not user_dict or not check_password_hash(user_dict["password_hash"], password):
            flash("Invalid email or password.", "danger")
            return render_template("login.html", email=email, next=next_url), 401

        user_obj = User(
            id=user_dict["id"],
            email=user_dict["email"],
            role=user_dict["role"],
            created_at=user_dict.get("created_at"),
        )
        login_user(user_obj)
        flash("Logged in successfully.", "success")

        if next_url and is_safe_redirect_url(next_url):
            return redirect(next_url)

        if user_obj.role == "engineer":
            return redirect(url_for("engineer_view"))
        return redirect(url_for("index"))

    return render_template("login.html", next=request.args.get("next"))


@app.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    """Logout current user and redirect to login page."""
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------
# User Reporting Routes
# ---------------------------------------------------------

@app.route("/")
@login_required
def index():
    """
    User View: Live camera preview + image upload, GPS capture.
    Engineers visiting the root are redirected to their dashboard.
    """
    if current_user.role == "engineer":
        return redirect(url_for("engineer_view"))

    return render_template(
        "submit.html",
        allowed_extensions=",".join(f".{e}" for e in Config.ALLOWED_EXTENSIONS),
    )


@app.route("/submit", methods=["POST"])
@login_required
def submit_report():
    """
    Process user submitted images, Gemma analysis, earthquake context, and area priority.
    Requires an authenticated regular User account. Report is associated with current_user.id.
    """
    if current_user.role != "user":
        abort(403)

    files = request.files.getlist("images")
    if not files or all(f.filename == "" for f in files):
        if "image" in request.files and request.files["image"].filename:
            files = [request.files["image"]]
        else:
            flash("Please capture or select at least one photograph of the structural damage.", "danger")
            return redirect(url_for("index"))

    # Parse GPS coordinates
    lat = None
    lon = None
    acc = None
    try:
        if request.form.get("latitude"):
            lat = float(request.form.get("latitude"))
        if request.form.get("longitude"):
            lon = float(request.form.get("longitude"))
        if request.form.get("accuracy"):
            acc = float(request.form.get("accuracy"))
    except (ValueError, TypeError):
        lat, lon, acc = None, None, None

    saved_images = []
    for f in files:
        if not f.filename:
            continue
        try:
            saved_fn, orig_fn, fsize, mtype = validate_and_save_image(f)
            saved_images.append({
                "filename": saved_fn,
                "original_filename": orig_fn,
                "file_size": fsize,
                "mime_type": mtype,
            })
        except ValueError as e:
            flash(str(e), "danger")
            return redirect(url_for("index"))

    if not saved_images:
        flash("No valid image files received.", "danger")
        return redirect(url_for("index"))

    report_id = f"rep_{uuid.uuid4().hex[:12]}"

    # Save to SQLite immediately associating with current_user.id from session
    database.create_report(
        report_id=report_id,
        latitude=lat,
        longitude=lon,
        gps_accuracy=acc,
        user_id=current_user.id,
    )
    database.add_report_images(report_id, saved_images)

    image_paths = [Path(Config.UPLOAD_FOLDER) / img["filename"] for img in saved_images]

    # Query public USGS Earthquake Catalog API (with strict timeout)
    eq_info = get_recent_earthquakes(latitude=lat, longitude=lon, max_radius_km=300.0, days=30)

    # Gemma Vision Structural Analysis
    try:
        analysis_result = analyze_structural_report(image_paths=image_paths)

        summary = analysis_result.get("summary", "Visual structural screening completed.")
        safety_score = analysis_result.get("safety_score", 5)
        recommended_action = analysis_result.get("recommended_action", "PROFESSIONAL_INSPECTION")
        evidence = analysis_result.get("evidence", "Surface visual inspection.")

        # Calculate Area Priority comparing nearby reports and earthquake context
        area_priority, area_rationale, nearby_count, _ = database.analyze_nearby_reports(
            report_id=report_id,
            latitude=lat,
            longitude=lon,
            safety_score=safety_score,
            recommended_action=recommended_action,
            radius_meters=float(Config.CLUSTER_RADIUS_METERS),
            time_window_days=14,
            eq_info=eq_info,
        )

        database.update_report_analysis(
            report_id=report_id,
            status="completed",
            summary=summary,
            safety_score=safety_score,
            recommended_action=recommended_action,
            evidence=evidence,
            area_priority=area_priority,
            area_priority_rationale=area_rationale,
            nearby_report_count=nearby_count,
            earthquake_info=eq_info,
            model_name=analysis_result.get("model_name", Config.GEMMA_MODEL_NAME),
            provider_used=analysis_result.get("provider_used", "unknown"),
        )

        flash("Report submitted and screened successfully.", "success")
    except Exception as e:
        logger.error(f"Inference failure for report {report_id}: {e}", exc_info=True)
        database.update_report_status(report_id, status="failed", error_message=str(e))
        flash(
            f"Images and GPS saved safely, but AI model inference encountered an error: {e}. You can run offline screening or retry.",
            "warning",
        )

    return redirect(url_for("view_report", report_id=report_id))


@app.route("/reports")
@login_required
def list_reports():
    """
    Display only reports owned by the authenticated User.
    Engineers are redirected to the Engineer Dashboard.
    """
    if current_user.role == "engineer":
        return redirect(url_for("engineer_view"))

    user_reports = database.get_user_reports(current_user.id)
    return render_template("reports_list.html", reports=user_reports)


@app.route("/report/<report_id>")
@app.route("/reports/<report_id>")
@login_required
def view_report(report_id: str):
    """
    Concise Result Page:
    - Verifies ownership if current user is a regular User.
    - Engineers can view any report.
    - Reads strictly from SQLite without re-invoking Gemma API.
    """
    report = database.get_report(report_id)
    if not report:
        abort(404)

    # Authorization check: regular users can only view their own reports
    if current_user.role == "user" and report.get("user_id") != current_user.id:
        abort(403)

    return render_template("report.html", report=report)


# ---------------------------------------------------------
# Engineer / Help-Team Dashboard & Actions
# ---------------------------------------------------------

@app.route("/engineer")
@login_required
@role_required("engineer")
def engineer_view():
    """
    Engineer / Help-Team View:
    Restricted to Engineer accounts.
    Partitioned into two incident views:
    - 'active': Not Done and In Progress incidents
    - 'past': Done incidents
    Interactive Leaflet map showing risk-colored markers plus simple incident list.
    """
    view_mode = request.args.get("view", "active").lower()
    if view_mode not in ("active", "past"):
        view_mode = "active"

    incidents = database.get_engineer_incidents(view=view_mode, limit=100)
    counts = database.get_incident_counts()

    return render_template(
        "engineer.html",
        reports=incidents,
        view_mode=view_mode,
        incident_counts=counts,
    )


@app.route("/report/<report_id>/status", methods=["POST"])
@login_required
def update_report_status_route(report_id: str):
    """
    Update work status of an individual report. Restricted to Engineer accounts.
    Supports form POST and JSON requests without modifying AI safety score or area priority.
    """
    if current_user.role != "engineer":
        if request.is_json:
            return jsonify({"error": "Forbidden: Engineer role required"}), 403
        abort(403)

    report = database.get_report(report_id)
    if not report:
        if request.is_json:
            return jsonify({"error": "Report not found"}), 404
        abort(404)

    if request.is_json:
        data = request.get_json(silent=True) or {}
        new_status = data.get("work_status") or data.get("status")
    else:
        new_status = request.form.get("work_status") or request.form.get("status")

    if not new_status or new_status not in database.ALLOWED_WORK_STATUSES:
        err_msg = f"Invalid status '{new_status}'. Allowed values: {sorted(list(database.ALLOWED_WORK_STATUSES))}"
        if request.is_json:
            return jsonify({"error": err_msg}), 400
        flash(err_msg, "danger")
        return redirect(url_for("view_report", report_id=report_id))

    database.update_report_work_status(report_id, new_status)

    if request.is_json:
        return jsonify({
            "success": True,
            "report_id": report_id,
            "work_status": new_status,
            "work_status_label": database.WORK_STATUS_LABELS.get(new_status, new_status),
        })

    flash(f"Work status updated to '{database.WORK_STATUS_LABELS.get(new_status, new_status)}'.", "success")
    next_url = request.form.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("view_report", report_id=report_id))


@app.route("/api/reports/bulk-status", methods=["POST"])
@login_required
def bulk_update_reports_status():
    """Bulk update work status for multiple selected reports (Engineer only)."""
    if current_user.role != "engineer":
        return jsonify({"error": "Forbidden: Engineer role required"}), 403

    if not request.is_json:
        report_ids_raw = request.form.getlist("report_ids") or request.form.get("report_ids", "").split(",")
        new_status = request.form.get("work_status") or request.form.get("status")
        report_ids = [r.strip() for r in report_ids_raw if r.strip()]
    else:
        data = request.get_json(silent=True) or {}
        report_ids = data.get("report_ids", [])
        new_status = data.get("work_status") or data.get("status")

    if not isinstance(report_ids, list) or not report_ids:
        return jsonify({"error": "No report IDs provided for bulk status update."}), 400

    clean_ids = list(dict.fromkeys(str(r).strip() for r in report_ids if r))
    if not clean_ids:
        return jsonify({"error": "No valid report IDs provided."}), 400

    if not new_status or new_status not in database.ALLOWED_WORK_STATUSES:
        return jsonify({
            "error": f"Invalid status '{new_status}'. Allowed values: {sorted(list(database.ALLOWED_WORK_STATUSES))}"
        }), 400

    updated_count = database.bulk_update_work_status(clean_ids, new_status)

    return jsonify({
        "success": True,
        "updated_count": updated_count,
        "work_status": new_status,
        "work_status_label": database.WORK_STATUS_LABELS.get(new_status, new_status),
        "report_ids": clean_ids,
    })


@app.route("/report/<report_id>/simulate", methods=["POST"])
@login_required
def simulate_analysis(report_id: str):
    """Execute local offline structural screening fallback."""
    report = database.get_report(report_id)
    if not report:
        abort(404)

    if current_user.role == "user" and report.get("user_id") != current_user.id:
        abort(403)

    image_paths = [Path(Config.UPLOAD_FOLDER) / img["filename"] for img in report["images"]]

    try:
        analysis_result = analyze_structural_report(image_paths=image_paths, force_offline=True)
        summary = analysis_result.get("summary", "")
        safety_score = analysis_result.get("safety_score", 4)
        recommended_action = analysis_result.get("recommended_action", "PROFESSIONAL_INSPECTION")
        evidence = analysis_result.get("evidence", "")

        eq_info = get_recent_earthquakes(latitude=report.get("latitude"), longitude=report.get("longitude"))

        area_priority, area_rationale, nearby_count, _ = database.analyze_nearby_reports(
            report_id=report_id,
            latitude=report.get("latitude"),
            longitude=report.get("longitude"),
            safety_score=safety_score,
            recommended_action=recommended_action,
            radius_meters=float(Config.CLUSTER_RADIUS_METERS),
            time_window_days=14,
            eq_info=eq_info,
        )

        database.update_report_analysis(
            report_id=report_id,
            status="completed",
            summary=summary,
            safety_score=safety_score,
            recommended_action=recommended_action,
            evidence=evidence,
            area_priority=area_priority,
            area_priority_rationale=area_rationale,
            nearby_report_count=nearby_count,
            earthquake_info=eq_info,
            model_name=analysis_result.get("model_name", "Local Screener"),
            provider_used="offline_simulation",
            error_message=None,
        )

        flash("Offline structural screening completed.", "info")
    except Exception as e:
        logger.error(f"Offline screening failed for report {report_id}: {e}", exc_info=True)
        database.update_report_status(report_id, status="failed", error_message=str(e))
        flash(f"Offline screening failed: {e}", "danger")

    return redirect(url_for("view_report", report_id=report_id))


@app.route("/report/<report_id>/retry", methods=["POST"])
@login_required
def retry_analysis(report_id: str):
    """Retry Gemma live inference on an existing preserved report."""
    report = database.get_report(report_id)
    if not report:
        abort(404)

    if current_user.role == "user" and report.get("user_id") != current_user.id:
        abort(403)

    image_paths = [Path(Config.UPLOAD_FOLDER) / img["filename"] for img in report["images"]]

    try:
        analysis_result = analyze_structural_report(image_paths=image_paths)
        summary = analysis_result.get("summary", "")
        safety_score = analysis_result.get("safety_score", 5)
        recommended_action = analysis_result.get("recommended_action", "PROFESSIONAL_INSPECTION")
        evidence = analysis_result.get("evidence", "")

        eq_info = get_recent_earthquakes(latitude=report.get("latitude"), longitude=report.get("longitude"))

        area_priority, area_rationale, nearby_count, _ = database.analyze_nearby_reports(
            report_id=report_id,
            latitude=report.get("latitude"),
            longitude=report.get("longitude"),
            safety_score=safety_score,
            recommended_action=recommended_action,
            radius_meters=float(Config.CLUSTER_RADIUS_METERS),
            time_window_days=14,
            eq_info=eq_info,
        )

        database.update_report_analysis(
            report_id=report_id,
            status="completed",
            summary=summary,
            safety_score=safety_score,
            recommended_action=recommended_action,
            evidence=evidence,
            area_priority=area_priority,
            area_priority_rationale=area_rationale,
            nearby_report_count=nearby_count,
            earthquake_info=eq_info,
            model_name=analysis_result.get("model_name", Config.GEMMA_MODEL_NAME),
            provider_used=analysis_result.get("provider_used", "unknown"),
            error_message=None,
        )

        flash("Re-analysis completed successfully.", "success")
    except Exception as e:
        logger.error(f"Retry inference failure for report {report_id}: {e}", exc_info=True)
        database.update_report_status(report_id, status="failed", error_message=str(e))
        flash(f"Retry failed: {e}", "danger")

    return redirect(url_for("view_report", report_id=report_id))


@app.route("/uploads/<filename>")
def uploaded_file(filename: str):
    """Safely serve uploaded image files."""
    safe_name = secure_filename(filename)
    return send_from_directory(Config.UPLOAD_FOLDER, safe_name)


@app.route("/api/config-status")
def config_status():
    """Config status endpoint."""
    return jsonify({
        "status": "online",
        "model_name": Config.GEMMA_MODEL_NAME,
        "provider": Config.GEMMA_PROVIDER,
        "has_api_key": bool(Config.GEMMA_API_KEY),
        "mock_fallback_enabled": Config.GEMMA_MOCK_FALLBACK,
    })


@app.errorhandler(403)
def forbidden_error(error):
    if request.is_json:
        return jsonify({"error": "Forbidden: You do not have permission to access this resource"}), 403
    return render_template("403.html"), 403


@app.errorhandler(404)
def not_found_error(error):
    return render_template("404.html"), 404


@app.errorhandler(413)
def request_entity_too_large(error):
    flash(
        f"Uploaded files are too large. Maximum allowed size is {Config.MAX_CONTENT_LENGTH // (1024 * 1024)} MB.",
        "danger",
    )
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=Config.DEBUG)
