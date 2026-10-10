import io
import os
import unittest
from pathlib import Path
from PIL import Image

# Use temporary test database
os.environ["DATABASE_NAME"] = "test_seismic_auth.db"
os.environ["GEMMA_MOCK_FALLBACK"] = "true"
os.environ["GEMMA_TEST_MODE"] = "true"

from app import app
import database
from services.gemma import (
    FIXED_SYSTEM_INSTRUCTION,
    FIXED_ANALYSIS_PROMPT,
    VALID_ACTIONS,
    sanitize_safety_score,
    sanitize_recommended_action,
    parse_model_json_response,
    run_offline_structural_screening,
)
from services.earthquake import get_recent_earthquakes


def create_dummy_image(format="JPEG", size=(200, 200), color=(180, 100, 80)) -> io.BytesIO:
    """Helper to create an in-memory test image."""
    img_byte_arr = io.BytesIO()
    image = Image.new("RGB", size, color=color)
    image.save(img_byte_arr, format=format)
    img_byte_arr.seek(0)
    return img_byte_arr


class FullAuthAndIncidentTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        database.init_db()

    def setUp(self):
        self.app = app
        self.app.config["TESTING"] = True
        self.app.config["WTF_CSRF_ENABLED"] = False
        self.client = self.app.test_client()

    def register(self, email, password, role="user"):
        return self.client.post(
            "/register",
            data={
                "email": email,
                "password": password,
                "confirm_password": password,
                "role": role,
            },
            follow_redirects=False,
        )

    def login(self, email, password):
        return self.client.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=False,
        )

    def logout(self):
        return self.client.get("/logout", follow_redirects=False)

    # -------------------------------------------------------------
    # 1. Registration & Validation Tests
    # -------------------------------------------------------------
    def test_registration_validation(self):
        """Verify registration validations: email format, password length, mismatch, duplicates."""
        # Invalid email
        res = self.client.post("/register", data={
            "email": "invalid-email", "password": "password123", "confirm_password": "password123", "role": "user"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn(b"valid email", res.data.lower())

        # Password too short (< 8 chars)
        res = self.client.post("/register", data={
            "email": "short@example.com", "password": "short", "confirm_password": "short", "role": "user"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn(b"at least 8 characters", res.data.lower())

        # Password mismatch
        res = self.client.post("/register", data={
            "email": "mismatch@example.com", "password": "password123", "confirm_password": "password456", "role": "user"
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn(b"do not match", res.data.lower())

        # Successful registration as User
        res_ok = self.client.post("/register", data={
            "email": "user1@example.com", "password": "password123", "confirm_password": "password123", "role": "user"
        }, follow_redirects=False)
        self.assertEqual(res_ok.status_code, 302)
        self.assertEqual(res_ok.headers["Location"], "/")

        # Duplicate email registration rejected
        self.logout()
        res_dup = self.client.post("/register", data={
            "email": "user1@example.com", "password": "password123", "confirm_password": "password123", "role": "user"
        })
        self.assertEqual(res_dup.status_code, 400)
        self.assertIn(b"already exists", res_dup.data.lower())

    def test_engineer_registration_redirect(self):
        """Verify registering as an Engineer redirects directly to /engineer dashboard."""
        self.logout()
        res = self.client.post("/register", data={
            "email": "engineer1@example.com", "password": "password123", "confirm_password": "password123", "role": "engineer"
        }, follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["Location"], "/engineer")

    # -------------------------------------------------------------
    # 2. Login & Logout Tests
    # -------------------------------------------------------------
    def test_login_and_logout_flow(self):
        """Verify successful login, incorrect password handling, and session logout."""
        self.logout()
        # Register user
        self.register("logintest@example.com", "password123", "user")
        self.logout()

        # Invalid password
        res_fail = self.login("logintest@example.com", "wrongpassword")
        self.assertEqual(res_fail.status_code, 401)
        self.assertIn(b"Invalid email or password", res_fail.data)

        # Non-existent user
        res_nouser = self.login("nonexistent@example.com", "password123")
        self.assertEqual(res_nouser.status_code, 401)

        # Successful user login -> redirects to /
        res_ok = self.login("logintest@example.com", "password123")
        self.assertEqual(res_ok.status_code, 302)
        self.assertEqual(res_ok.headers["Location"], "/")

        # Logout
        res_logout = self.logout()
        self.assertEqual(res_logout.status_code, 302)
        self.assertEqual(res_logout.headers["Location"], "/login")

    def test_unauthenticated_protected_routes_redirect(self):
        """Verify unauthenticated requests to protected routes redirect to /login."""
        self.logout()
        # Protected user routes
        res_index = self.client.get("/")
        self.assertEqual(res_index.status_code, 302)
        self.assertIn("/login", res_index.headers["Location"])

        res_reports = self.client.get("/reports")
        self.assertEqual(res_reports.status_code, 302)
        self.assertIn("/login", res_reports.headers["Location"])

        # Protected engineer route
        res_eng = self.client.get("/engineer")
        self.assertEqual(res_eng.status_code, 302)
        self.assertIn("/login", res_eng.headers["Location"])

    # -------------------------------------------------------------
    # 3. User Ownership & Report Isolation Tests
    # -------------------------------------------------------------
    def test_user_can_submit_and_view_only_own_reports(self):
        """Verify User A can submit and view only their own report; User B gets 403 on User A's report."""
        self.logout()
        # Register User A
        self.register("user_a@example.com", "password123", "user")
        self.login("user_a@example.com", "password123")

        # Submit report as User A
        img = create_dummy_image()
        data = {
            "images": [(img, "wall_user_a.jpg")],
            "latitude": "26.1510",
            "longitude": "91.7310",
        }
        res_submit = self.client.post("/submit", data=data, content_type="multipart/form-data", follow_redirects=False)
        self.assertEqual(res_submit.status_code, 302)
        rep_id_a = res_submit.headers["Location"].split("/")[-1]

        # Verify User A can view Report A
        res_view_a = self.client.get(f"/report/{rep_id_a}")
        self.assertEqual(res_view_a.status_code, 200)

        # Verify Report A appears in User A's /reports list
        res_list_a = self.client.get("/reports")
        self.assertEqual(res_list_a.status_code, 200)
        self.assertIn(rep_id_a.encode(), res_list_a.data)

        # Register and switch to User B
        self.logout()
        self.register("user_b@example.com", "password123", "user")
        self.login("user_b@example.com", "password123")

        # User B attempts to access User A's report -> 403 Forbidden!
        res_view_b = self.client.get(f"/report/{rep_id_a}")
        self.assertEqual(res_view_b.status_code, 403)

        # User B's /reports list does NOT contain User A's report
        res_list_b = self.client.get("/reports")
        self.assertEqual(res_list_b.status_code, 200)
        self.assertNotIn(rep_id_a.encode(), res_list_b.data)

    # -------------------------------------------------------------
    # 4. Role-Based Permissions & Engineer Dashboard Tests
    # -------------------------------------------------------------
    def test_user_cannot_access_engineer_dashboard_or_update_status(self):
        """Verify regular users cannot access /engineer (403) or update work status (403)."""
        self.logout()
        self.register("user_regular@example.com", "password123", "user")
        self.login("user_regular@example.com", "password123")

        # Try to access /engineer -> 403
        res_eng = self.client.get("/engineer")
        self.assertEqual(res_eng.status_code, 403)

        # Try to update status -> 403
        res_stat = self.client.post(
            "/report/dummy_id/status",
            json={"work_status": "done"},
            content_type="application/json",
        )
        self.assertEqual(res_stat.status_code, 403)

    def test_engineer_can_view_all_reports_and_update_status(self):
        """Verify an Engineer can view any report, view the engineer map, and update work status."""
        self.logout()
        # 1. User submits a report
        self.register("citizen_poster@example.com", "password123", "user")
        self.login("citizen_poster@example.com", "password123")
        res_submit = self.client.post(
            "/submit",
            data={"images": [(create_dummy_image(), "crack.jpg")], "latitude": "26.155", "longitude": "91.735"},
            content_type="multipart/form-data",
            follow_redirects=False,
        )
        rep_id = res_submit.headers["Location"].split("/")[-1]

        # 2. Switch to Engineer
        self.logout()
        self.register("field_engineer@example.com", "password123", "engineer")
        self.login("field_engineer@example.com", "password123")

        # Engineer can access /engineer
        res_eng = self.client.get("/engineer")
        self.assertEqual(res_eng.status_code, 200)
        self.assertIn(rep_id.encode(), res_eng.data)

        # Engineer can access individual report details regardless of owner
        res_view = self.client.get(f"/report/{rep_id}")
        self.assertEqual(res_view.status_code, 200)

        # Engineer can update status to in_progress
        res_stat = self.client.post(
            f"/report/{rep_id}/status",
            json={"work_status": "in_progress"},
            content_type="application/json",
        )
        self.assertEqual(res_stat.status_code, 200)
        self.assertEqual(res_stat.get_json()["work_status"], "in_progress")

        # Verify persistence in SQLite
        updated = database.get_report(rep_id)
        self.assertEqual(updated["work_status"], "in_progress")

    # -------------------------------------------------------------
    # 5. Two Incident Views & Status Transitions
    # -------------------------------------------------------------
    def test_two_incident_views_filtering_and_moving(self):
        """Verify Active view shows not_done & in_progress, Past shows done, and status change moves incidents."""
        self.logout()
        self.register("submitter@example.com", "password123", "user")
        self.login("submitter@example.com", "password123")

        # Create two reports
        res1 = self.client.post("/submit", data={"images": [(create_dummy_image(), "wall1.jpg")], "latitude": "26.15", "longitude": "91.73"}, content_type="multipart/form-data", follow_redirects=False)
        id1 = res1.headers["Location"].split("/")[-1]

        res2 = self.client.post("/submit", data={"images": [(create_dummy_image(), "wall2.jpg")], "latitude": "26.16", "longitude": "91.74"}, content_type="multipart/form-data", follow_redirects=False)
        id2 = res2.headers["Location"].split("/")[-1]

        # Login as Engineer
        self.logout()
        self.register("eval_engineer@example.com", "password123", "engineer")
        self.login("eval_engineer@example.com", "password123")

        # Initially both in active view
        res_active1 = self.client.get("/engineer?view=active")
        self.assertIn(id1.encode(), res_active1.data)
        self.assertIn(id2.encode(), res_active1.data)

        # Update id1 to done
        self.client.post(f"/report/{id1}/status", json={"work_status": "done"}, content_type="application/json")

        # Active view now only contains id2
        res_active2 = self.client.get("/engineer?view=active")
        self.assertNotIn(id1.encode(), res_active2.data)
        self.assertIn(id2.encode(), res_active2.data)

        # Past view contains id1
        res_past = self.client.get("/engineer?view=past")
        self.assertIn(id1.encode(), res_past.data)
        self.assertNotIn(id2.encode(), res_past.data)

        # Move id1 back to not_done
        self.client.post(f"/report/{id1}/status", json={"work_status": "not_done"}, content_type="application/json")
        res_active3 = self.client.get("/engineer?view=active")
        self.assertIn(id1.encode(), res_active3.data)

    def test_risk_marker_legend_present(self):
        """Verify plain-text risk color legend is present in engineer view."""
        self.logout()
        self.register("map_engineer@example.com", "password123", "engineer")
        self.login("map_engineer@example.com", "password123")

        res = self.client.get("/engineer")
        self.assertEqual(res.status_code, 200)
        self.assertIn(b"Red:", res.data)
        self.assertIn(b"Orange:", res.data)
        self.assertIn(b"Green:", res.data)
        self.assertIn(b"Gray:", res.data)
        self.assertIn(b"preliminary image-based concern", res.data.lower())


if __name__ == "__main__":
    unittest.main()
