import sqlite3
import json
import math
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from config import Config


@contextmanager
def get_db():
    """Context manager for SQLite connection with automatic close and row_factory."""
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def utc_now_iso() -> str:
    """Return ISO formatted UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in meters between two GPS points."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return R * c


ALLOWED_WORK_STATUSES = {"not_done", "in_progress", "done"}
WORK_STATUS_LABELS = {
    "not_done": "Not Done",
    "in_progress": "In Progress",
    "done": "Done",
}
ALLOWED_ROLES = {"user", "engineer"}


def init_db() -> None:
    """Initialize SQLite database tables and apply migrations for Phase 2 and work status schema."""
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'engineer')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            work_status TEXT NOT NULL DEFAULT 'not_done',
            latitude REAL,
            longitude REAL,
            gps_accuracy REAL,
            summary TEXT,
            safety_score INTEGER DEFAULT 5,
            recommended_action TEXT DEFAULT 'PROFESSIONAL_INSPECTION',
            evidence TEXT,
            area_priority TEXT DEFAULT 'LOW',
            area_priority_rationale TEXT,
            nearby_report_count INTEGER DEFAULT 0,
            earthquake_info TEXT,
            model_name TEXT,
            provider_used TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS report_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            mime_type TEXT DEFAULT 'image/jpeg',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (report_id) REFERENCES reports (id) ON DELETE CASCADE
        );
        """)

        # Migration: Ensure all Phase 2 and Work Status columns exist
        existing_cols = [row["name"] for row in conn.execute("PRAGMA table_info(reports)").fetchall()]
        columns_to_add = [
            ("user_id", "TEXT REFERENCES users(id)"),
            ("work_status", "TEXT DEFAULT 'not_done'"),
            ("latitude", "REAL"),
            ("longitude", "REAL"),
            ("gps_accuracy", "REAL"),
            ("summary", "TEXT"),
            ("safety_score", "INTEGER DEFAULT 5"),
            ("recommended_action", "TEXT DEFAULT 'PROFESSIONAL_INSPECTION'"),
            ("evidence", "TEXT"),
            ("area_priority", "TEXT DEFAULT 'LOW'"),
            ("area_priority_rationale", "TEXT"),
            ("nearby_report_count", "INTEGER DEFAULT 0"),
            ("earthquake_info", "TEXT"),
            ("model_name", "TEXT"),
            ("provider_used", "TEXT"),
            ("error_message", "TEXT"),
        ]
        for col_name, col_type in columns_to_add:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE reports ADD COLUMN {col_name} {col_type}")

        # Safe backfill: ensure existing reports have 'not_done'
        conn.execute("UPDATE reports SET work_status = 'not_done' WHERE work_status IS NULL OR work_status = ''")

        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
        CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_reports_user_id ON reports(user_id);
        CREATE INDEX IF NOT EXISTS idx_report_images_report ON report_images(report_id);
        CREATE INDEX IF NOT EXISTS idx_reports_gps ON reports(latitude, longitude);
        CREATE INDEX IF NOT EXISTS idx_reports_priority ON reports(area_priority, safety_score);
        CREATE INDEX IF NOT EXISTS idx_reports_work_status ON reports(work_status);
        """)
        conn.commit()


def create_user(email: str, password_hash: str, role: str) -> str:
    """Create a new user with hashed password and role ('user' or 'engineer')."""
    if role not in ALLOWED_ROLES:
        raise ValueError(f"Invalid role '{role}'. Allowed roles: {ALLOWED_ROLES}")
    email_clean = email.strip().lower()
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, email_clean, password_hash, role, now),
        )
        conn.commit()
    return user_id


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve user record by user_id."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """Retrieve user record by email (case-insensitive)."""
    if not email:
        return None
    email_clean = email.strip().lower()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email_clean,)).fetchone()
        return dict(row) if row else None


def create_report(
    report_id: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    gps_accuracy: Optional[float] = None,
    user_id: Optional[str] = None,
) -> None:
    """Create a new report record with optional coordinates and owner user_id."""
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO reports (
                id, user_id, status, work_status, latitude, longitude, gps_accuracy,
                safety_score, recommended_action, area_priority, nearby_report_count,
                created_at, updated_at
            )
            VALUES (?, ?, 'pending', 'not_done', ?, ?, ?, 5, 'PROFESSIONAL_INSPECTION', 'LOW', 0, ?, ?)
            """,
            (report_id, user_id, latitude, longitude, gps_accuracy, now, now),
        )
        conn.commit()


def add_report_images(
    report_id: str,
    images: List[Dict[str, Any]],
) -> None:
    """Add multiple image records for a report."""
    now = utc_now_iso()
    with get_db() as conn:
        for img in images:
            conn.execute(
                """
                INSERT INTO report_images (report_id, filename, original_filename, file_size, mime_type, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    img["filename"],
                    img.get("original_filename", "photo.jpg"),
                    img.get("file_size", 0),
                    img.get("mime_type", "image/jpeg"),
                    now,
                ),
            )
        conn.commit()


def analyze_nearby_reports(
    report_id: str,
    latitude: Optional[float],
    longitude: Optional[float],
    safety_score: int,
    recommended_action: str,
    radius_meters: float = 1000.0,
    time_window_days: int = 14,
    eq_info: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, int, List[Dict[str, Any]]]:
    """
    Query existing reports within a geographic radius and time window.
    Compare number of nearby reports, recency, and similarity to calculate a separate area_priority.
    Avoids duplicate submissions from the same device/spot within short timeframe.
    """
    if latitude is None or longitude is None:
        return (
            "LOW" if safety_score > 3 else "HIGH",
            "No GPS coordinates available; area priority assigned based on individual visible severity.",
            0,
            [],
        )

    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=time_window_days)).isoformat()

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, latitude, longitude, summary, safety_score, recommended_action, created_at
            FROM reports
            WHERE id != ? AND latitude IS NOT NULL AND longitude IS NOT NULL AND created_at >= ?
            """,
            (report_id, cutoff_date),
        ).fetchall()

    nearby_list = []
    seen_locations = []

    for r in rows:
        dist = haversine_distance(latitude, longitude, r["latitude"], r["longitude"])
        if dist <= radius_meters:
            # Check for duplicate submission within 15 meters and 30 minutes
            is_dup = False
            for seen_lat, seen_lon in seen_locations:
                if haversine_distance(r["latitude"], r["longitude"], seen_lat, seen_lon) < 15.0:
                    is_dup = True
                    break

            if not is_dup:
                seen_locations.append((r["latitude"], r["longitude"]))
                nearby_list.append({
                    "id": r["id"],
                    "latitude": r["latitude"],
                    "longitude": r["longitude"],
                    "summary": r["summary"],
                    "safety_score": r["safety_score"],
                    "recommended_action": r["recommended_action"],
                    "created_at": r["created_at"],
                    "distance_meters": round(dist, 1),
                })

    nearby_count = len(nearby_list)
    serious_nearby = sum(
        1 for r in nearby_list if r["recommended_action"] in ("URGENT_ASSESSMENT", "PROFESSIONAL_INSPECTION") or (r["safety_score"] and r["safety_score"] <= 5)
    )

    # Check recent earthquake context
    has_recent_major_quake = False
    if eq_info and eq_info.get("found") and eq_info.get("closest"):
        closest_eq = eq_info["closest"]
        if closest_eq.get("magnitude", 0) >= 4.0 and closest_eq.get("distance_km", 999) <= 250:
            has_recent_major_quake = True

    # Calculate area_priority
    if recommended_action == "URGENT_ASSESSMENT" or safety_score <= 3 or serious_nearby >= 2:
        area_priority = "HIGH"
        rationale = (
            f"High area inspection priority: Immediate structural concerns or {serious_nearby} "
            f"nearby reports showing significant structural distress within {int(radius_meters)}m."
        )
    elif recommended_action == "PROFESSIONAL_INSPECTION" or nearby_count >= 2 or has_recent_major_quake:
        area_priority = "MEDIUM"
        reasons = []
        if nearby_count >= 2:
            reasons.append(f"{nearby_count} cluster reports in same general area")
        if recommended_action == "PROFESSIONAL_INSPECTION":
            reasons.append("visible engineering inspection indicators")
        if has_recent_major_quake:
            reasons.append("recent corroborating regional seismic event")
        rationale = f"Medium area inspection priority: {', '.join(reasons)}."
    else:
        area_priority = "LOW"
        rationale = (
            f"Low area inspection priority: Isolated report ({nearby_count} nearby) with no "
            "concerning recurring damage cluster."
        )

    return area_priority, rationale, nearby_count, nearby_list


def update_report_analysis(
    report_id: str,
    status: str,
    summary: str,
    safety_score: int,
    recommended_action: str,
    evidence: str,
    area_priority: str,
    area_priority_rationale: str,
    nearby_report_count: int,
    earthquake_info: Optional[Dict[str, Any]],
    model_name: str,
    provider_used: str,
    error_message: Optional[str] = None,
) -> None:
    """Update report with Gemma Phase 2 analysis, score, action, area priority, and earthquake info."""
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE reports
            SET status = ?,
                summary = ?,
                safety_score = ?,
                recommended_action = ?,
                evidence = ?,
                area_priority = ?,
                area_priority_rationale = ?,
                nearby_report_count = ?,
                earthquake_info = ?,
                model_name = ?,
                provider_used = ?,
                error_message = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                summary,
                safety_score,
                recommended_action,
                evidence,
                area_priority,
                area_priority_rationale,
                nearby_report_count,
                json.dumps(earthquake_info) if earthquake_info else None,
                model_name,
                provider_used,
                error_message,
                now,
                report_id,
            ),
        )
        conn.commit()


def update_report_status(
    report_id: str,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    """Update report analysis status."""
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            """
            UPDATE reports
            SET status = ?,
                error_message = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, error_message, now, report_id),
        )
        conn.commit()


def update_report_work_status(report_id: str, work_status: str) -> bool:
    """
    Update work status of an individual report without altering AI scores or priority.
    Allowed values: 'not_done', 'in_progress', 'done'.
    """
    if work_status not in ALLOWED_WORK_STATUSES:
        raise ValueError(f"Invalid work status '{work_status}'. Allowed values: {ALLOWED_WORK_STATUSES}")

    now = utc_now_iso()
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE reports
            SET work_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (work_status, now, report_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def bulk_update_work_status(report_ids: List[str], work_status: str) -> int:
    """
    Bulk update work status for multiple reports identified by their database IDs.
    Returns the count of successfully updated reports.
    """
    if work_status not in ALLOWED_WORK_STATUSES:
        raise ValueError(f"Invalid work status '{work_status}'. Allowed values: {ALLOWED_WORK_STATUSES}")

    if not report_ids:
        return 0

    clean_ids = list(dict.fromkeys(str(rid).strip() for rid in report_ids if rid))
    if not clean_ids:
        return 0

    now = utc_now_iso()
    placeholders = ",".join("?" for _ in clean_ids)
    with get_db() as conn:
        cursor = conn.execute(
            f"""
            UPDATE reports
            SET work_status = ?, updated_at = ?
            WHERE id IN ({placeholders})
            """,
            [work_status, now] + clean_ids,
        )
        conn.commit()
        return cursor.rowcount


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve report and images from SQLite without invoking external APIs."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT r.*, u.email as submitter_email
            FROM reports r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE r.id = ?
            """,
            (report_id,),
        ).fetchone()
        if not row:
            return None

        report = dict(row)
        report["work_status"] = report.get("work_status") or "not_done"

        try:
            report["earthquake_info"] = json.loads(report["earthquake_info"]) if report.get("earthquake_info") else None
        except Exception:
            report["earthquake_info"] = None

        img_rows = conn.execute(
            """
            SELECT * FROM report_images
            WHERE report_id = ?
            ORDER BY created_at ASC
            """,
            (report_id,),
        ).fetchall()
        report["images"] = [dict(img) for img in img_rows]

        # Fetch nearby reports for map rendering
        nearby = []
        if report.get("latitude") is not None and report.get("longitude") is not None:
            all_geo = conn.execute(
                """
                SELECT id, latitude, longitude, summary, safety_score, recommended_action, area_priority, work_status, created_at,
                       (SELECT filename FROM report_images WHERE report_id = reports.id LIMIT 1) as thumbnail
                FROM reports
                WHERE id != ? AND latitude IS NOT NULL AND longitude IS NOT NULL
                """,
                (report_id,),
            ).fetchall()
            for g in all_geo:
                dist = haversine_distance(report["latitude"], report["longitude"], g["latitude"], g["longitude"])
                if dist <= Config.CLUSTER_RADIUS_METERS:
                    g_dict = dict(g)
                    g_dict["work_status"] = g_dict.get("work_status") or "not_done"
                    g_dict["distance_meters"] = round(dist, 1)
                    nearby.append(g_dict)

        report["nearby_reports"] = nearby
        return report


def get_user_reports(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve reports submitted by a specific user."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT r.*,
                   (SELECT filename FROM report_images WHERE report_id = r.id ORDER BY id ASC LIMIT 1) as primary_image,
                   (SELECT COUNT(*) FROM report_images WHERE report_id = r.id) as image_count
            FROM reports r
            WHERE r.user_id = ?
            ORDER BY r.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

        result = []
        for row in rows:
            r = dict(row)
            r["work_status"] = r.get("work_status") or "not_done"
            try:
                r["earthquake_info"] = json.loads(r["earthquake_info"]) if r.get("earthquake_info") else None
            except Exception:
                r["earthquake_info"] = None
            result.append(r)
        return result


def get_engineer_incidents(view: str = "active", limit: int = 100) -> List[Dict[str, Any]]:
    """
    Retrieve reports partitioned by incident view:
    - 'active': reports with work_status in ('not_done', 'in_progress')
    - 'past': reports with work_status == 'done'
    """
    if view == "past":
        where_clause = "r.work_status = 'done'"
        order_clause = "r.updated_at DESC, r.created_at DESC"
    else:
        where_clause = "r.work_status IN ('not_done', 'in_progress') OR r.work_status IS NULL"
        order_clause = """
            CASE r.area_priority
                WHEN 'HIGH' THEN 1
                WHEN 'MEDIUM' THEN 2
                WHEN 'LOW' THEN 3
                ELSE 4
            END ASC,
            COALESCE(r.safety_score, 10) ASC,
            r.created_at DESC
        """

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT r.*,
                   u.email as submitter_email,
                   (SELECT filename FROM report_images WHERE report_id = r.id LIMIT 1) as primary_image,
                   (SELECT COUNT(*) FROM report_images WHERE report_id = r.id) as image_count
            FROM reports r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        result = []
        for row in rows:
            r = dict(row)
            r["work_status"] = r.get("work_status") or "not_done"
            try:
                r["earthquake_info"] = json.loads(r["earthquake_info"]) if r.get("earthquake_info") else None
            except Exception:
                r["earthquake_info"] = None

            # Fetch all image filenames for map popups
            imgs = conn.execute(
                "SELECT filename FROM report_images WHERE report_id = ?", (r["id"],)
            ).fetchall()
            r["all_images"] = [img["filename"] for img in imgs]
            result.append(r)
        return result


def get_incident_counts() -> Dict[str, int]:
    """Return counts of active and past incidents."""
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT 
                SUM(CASE WHEN work_status IN ('not_done', 'in_progress') OR work_status IS NULL THEN 1 ELSE 0 END) as active_count,
                SUM(CASE WHEN work_status = 'done' THEN 1 ELSE 0 END) as past_count
            FROM reports
            """
        ).fetchone()
        return {
            "active": int(row["active_count"] or 0) if row else 0,
            "past": int(row["past_count"] or 0) if row else 0,
        }


def get_all_reports_for_engineer(limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieve reports for engineer view (defaults to active incidents)."""
    return get_engineer_incidents(view="active", limit=limit)


def get_all_reports(limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve reports for history list."""
    return get_engineer_incidents(view="active", limit=limit)
