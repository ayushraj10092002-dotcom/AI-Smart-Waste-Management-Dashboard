import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from app import app, limiter
from flask_migrate import upgrade as run_migrations
from models.database import DeviceCredential, Location, Organization, User, db
from models.database import CollectionTask, NotificationOutbox, OperationalAlert, SmartBin
from models.waste_model import train_and_predict
from services.analytics import DatasetError, calculate_analytics, load_waste_data
from services.demo_data import classify_waste, get_collection_plan, get_demo_bins
from services.recommendations import build_recommendation
from services.tenant_data import create_organization, get_organization_locations
from werkzeug.security import generate_password_hash
from sqlalchemy import inspect, select, text


class WasteAnalyticsTests(unittest.TestCase):
    def test_current_csv_analytics(self):
        result = calculate_analytics(load_waste_data())
        self.assertEqual(result["weekly_total_kg"], 905.0)
        self.assertEqual(result["highest_waste_day"], "Thursday")
        self.assertEqual(result["lowest_waste_day"], "Wednesday")
        self.assertNotIn("recycling_percentage", result)
        self.assertNotIn("waste_by_category", result)
        self.assertNotIn("waste_by_location", result)

    def test_invalid_csv_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_file = Path(directory) / "invalid.csv"
            for contents in [
                "Day,Other\nMonday,1\n",
                "Day,Waste_kg\nMonday,NaN\n",
                "Day,Waste_kg\nMonday,-3\n",
                "Day,Waste_kg\n",
            ]:
                csv_file.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaises(DatasetError):
                    load_waste_data(csv_file)

    def test_missing_csv_has_clear_error(self):
        missing_file = Path(tempfile.gettempdir()) / "waste-data-file-that-does-not-exist.csv"
        with self.assertRaisesRegex(DatasetError, "was not found"):
            load_waste_data(missing_file)

    def test_prediction_warning_and_metrics_gate(self):
        current = train_and_predict([120, 135, 110, 145, 130, 140, 125])
        self.assertEqual(current["status"], "limited_training_data")
        self.assertIsNone(current["mae_kg"])
        self.assertIsNone(current["rmse_kg"])

        larger = train_and_predict([100 + value for value in range(12)])
        self.assertEqual(larger["status"], "trained")
        self.assertIsNotNone(larger["mae_kg"])
        self.assertIsNotNone(larger["rmse_kg"])

    def test_recommendation_thresholds(self):
        base = {
            "daily_average_kg": 100.0,
            "maximum_kg": 125.0,
            "highest_waste_day": "Friday",
            "latest_waste_kg": 100.0,
            "latest_day": "Sunday",
        }
        self.assertEqual(build_recommendation(base)["priority"], "High")
        base["maximum_kg"] = 110.0
        base["latest_waste_kg"] = 80.0
        self.assertEqual(build_recommendation(base)["priority"], "Low")
        base["latest_waste_kg"] = 100.0
        self.assertEqual(build_recommendation(base)["priority"], "Medium")

    def test_classification_and_simulated_bin_states(self):
        self.assertEqual(classify_waste(" PAPER ")["label"], "Paper")
        self.assertIsNone(classify_waste("glass"))
        self.assertEqual({item["status"] for item in get_demo_bins()}, {"Normal", "Warning", "Critical"})

    def test_collection_planner_prioritizes_critical_bins_on_tie(self):
        bins = [
            {"id": "BIN-A", "location": "Site A", "fill_percent": 90, "status": "Warning", "priority": "WARNING"},
            {"id": "BIN-B", "location": "Site B", "fill_percent": 90, "status": "Critical", "priority": "CRITICAL"},
        ]
        plan = get_collection_plan(bins)
        self.assertEqual(plan[0]["bin_id"], "BIN-B")
        self.assertEqual(plan[1]["bin_id"], "BIN-A")

    def test_custom_location_note(self):
        with app.app_context():
            org = create_organization("Note Org", "note-org")
            loc = Location(organization_id=org.id, name="Custom Site", demo=False)
            db.session.add(loc)
            db.session.commit()
            locations = get_organization_locations(org.id)
            custom = next(item for item in locations if item["name"] == "Custom Site")
            self.assertEqual(custom["note"], "Location registered; no waste measurement available.")


class FlaskRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_directory = tempfile.TemporaryDirectory()
        app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            SECRET_KEY="test-only-secret-key-with-sufficient-entropy",
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{Path(cls.database_directory.name) / 'tests.sqlite'}",
        )
        limiter.enabled = False
        with app.app_context():
            db.engine.dispose()
            db.drop_all()
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        limiter.enabled = True
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.database_directory.cleanup()

    def setUp(self):
        with app.app_context():
            db.drop_all()
            db.create_all()
            self.organization = create_organization("Test Organization", "test-organization")
            self.user = User(
                organization_id=self.organization.id,
                email="admin@example.test",
                password_hash=generate_password_hash("CorrectHorseBatteryStaple1!"),
                role="Admin",
            )
            db.session.add(self.user)
            db.session.commit()
            self.user_id = self.user.id
            self.organization_id = self.organization.id
        self.client = app.test_client()

    def login(self, client=None, email="admin@example.test", password="CorrectHorseBatteryStaple1!"):
        client = client or self.client
        return client.post("/login", data={"email": email, "password": password})

    def test_authentication_root_and_health(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlparse(response.headers["Location"]).path, "/login")
        self.assertEqual(self.client.get("/health").json, {"status": "healthy"})
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.assertEqual(urlparse(self.client.get("/dashboard").headers["Location"]).path, "/login")
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(urlparse(self.client.get("/").headers["Location"]).path, "/dashboard")
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.client.post("/logout").status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)

    def test_registration_creates_hashed_admin_and_allows_login(self):
        page = self.client.get("/register")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Create Account", page.data)
        response = self.client.post("/register", data={
            "full_name": "New Owner",
            "organization_name": "New Organization",
            "email": "owner@example.test",
            "password": "ValidPassword123",
            "confirm_password": "ValidPassword123",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlparse(response.headers["Location"]).path, "/login")
        with app.app_context():
            user = db.session.scalar(select(User).where(User.email == "owner@example.test"))
            self.assertEqual(user.full_name, "New Owner")
            self.assertNotEqual(user.password_hash, "ValidPassword123")
            self.assertTrue(user.password_hash.startswith("scrypt:"))
            self.assertEqual(user.role, "Admin")
            self.assertEqual(user.organization.name, "New Organization")
        login_response = self.login(email="owner@example.test", password="ValidPassword123")
        self.assertEqual(login_response.status_code, 302)

    def test_registration_validates_passwords_and_duplicate_email(self):
        mismatch = self.client.post("/register", data={
            "full_name": "New Owner",
            "organization_name": "Mismatch Organization",
            "email": "new-owner@example.test",
            "password": "short",
            "confirm_password": "different",
        })
        self.assertEqual(mismatch.status_code, 200)
        self.assertIn(b"at least 12 characters", mismatch.data)
        self.assertIn(b"Passwords do not match", mismatch.data)

        duplicate = self.client.post("/register", data={
            "full_name": "Another Owner",
            "organization_name": "Unused Organization",
            "email": "admin@example.test",
            "password": "ValidPassword123",
            "confirm_password": "ValidPassword123",
        })
        self.assertEqual(duplicate.status_code, 200)
        self.assertIn(b"already has an account", duplicate.data)
        with app.app_context():
            self.assertIsNone(db.session.scalar(select(Organization).where(Organization.slug == "unused-organization")))

    def test_invalid_login_and_lockout(self):
        for _ in range(5):
            response = self.login(password="incorrect-password")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Email or password is incorrect", response.data)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)

    def test_all_operational_pages_and_navigation_targets_render(self):
        self.login()

        paths = ["/dashboard", "/analytics", "/locations", "/bins", "/collection-planner", "/prediction", "/classification", "/alerts", "/reports"]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                if path == "/dashboard":
                    for label in ["Dashboard", "Analytics", "Locations", "Smart Bins", "Collection Planner", "Prediction", "Classification", "Alerts", "Reports"]:
                        self.assertIn(label.encode(), response.data)
                    for stale_route in ["/recommendations", "/settings", "/about", "/login", "/logout"]:
                        self.assertNotIn(f'href="{stale_route}"'.encode(), response.data)
            favicon_response = self.client.get("/static/favicon.svg")
            self.assertEqual(favicon_response.status_code, 200)
            favicon_response.close()

    def test_removed_pages_return_404(self):
        self.login()
        self.assertEqual(self.client.get("/login").status_code, 302)
        self.assertEqual(self.client.get("/logout").status_code, 405)
        for path in ["/recommendations", "/settings", "/about"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_apis_return_expected_shapes(self):
        self.login()
        self.assertEqual(self.client.get("/api/waste").json["unit"], "kg")
        self.assertEqual(self.client.get("/api/analytics").json["record_count"], 7)
        prediction = self.client.get("/api/prediction").json
        self.assertEqual(prediction["status"], "limited_training_data")
        bins = self.client.get("/api/bins").json
        self.assertEqual(bins["source"], "simulated demo data")
        self.assertEqual(bins["count"], 6)

    def test_classification_api_and_form_validate_input(self):
        self.login()
        valid = self.client.post("/api/classify", json={"waste_type": "paper"})
        self.assertEqual(valid.status_code, 200)
        self.assertFalse(valid.json["image_model_available"])
        self.assertEqual(self.client.post("/api/classify", json={"waste_type": "glass"}).status_code, 400)
        self.assertEqual(self.client.post("/api/classify", json=["paper"]).status_code, 400)
        self.assertIn(b"Choose one of the listed waste categories", self.client.post("/classification", data={"waste_type": "glass"}).data)

    def test_missing_data_has_user_facing_error(self):
        self.login()
        with patch("app.get_active_data", side_effect=DatasetError("Test data error.")):
            self.assertEqual(self.client.get("/api/analytics").status_code, 503)
            self.assertIn(b"Test data error", self.client.get("/dashboard").data)

    def test_model_failure_has_safe_api_response(self):
        self.login()
        with patch("services.prediction.train_and_predict", side_effect=RuntimeError("internal detail")):
            with self.assertLogs("services.prediction", level="ERROR"):
                response = self.client.get("/api/prediction")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("internal detail", response.json["error"])

    def test_unknown_api_route_returns_json_404(self):
        self.login()
        response = self.client.get("/api/not-real")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json)

    def test_operational_routes_and_demo_values(self):
        self.login()
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.client.get("/locations").status_code, 200)
        self.assertEqual(self.client.get("/collection-planner").status_code, 200)
        self.assertEqual(self.client.get("/reports").status_code, 200)
        self.assertEqual(self.client.get("/alerts").status_code, 200)
        locations_page = self.client.get("/locations").data
        self.assertIn(b"Static demo location values", locations_page)
        self.assertNotIn(b"Share", locations_page)
        dashboard_page = self.client.get("/dashboard").data
        self.assertIn(b"Smart bin status", dashboard_page)
        self.assertIn(b"Rule-based", dashboard_page)

    def test_role_permissions_and_admin_pages(self):
        with app.app_context():
            student = User(
                organization_id=self.organization_id,
                email="student@example.test",
                password_hash=generate_password_hash("CorrectHorseBatteryStaple1!"),
                role="Student/User",
            )
            staff = User(
                organization_id=self.organization_id,
                email="staff@example.test",
                password_hash=generate_password_hash("CorrectHorseBatteryStaple1!"),
                role="Staff",
            )
            db.session.add_all([student, staff])
            db.session.commit()
        student_client = app.test_client()
        self.login(student_client, "student@example.test")
        self.assertEqual(student_client.get("/dashboard").status_code, 200)
        self.assertEqual(student_client.get("/classification").status_code, 200)
        self.assertEqual(student_client.get("/analytics").status_code, 403)
        self.assertEqual(student_client.get("/admin/users").status_code, 403)
        classification_page = student_client.get("/classification").data
        self.assertIn(b"Classification", classification_page)
        self.assertNotIn(b'href="/analytics"', classification_page)

        self.login()
        self.assertEqual(self.client.get("/admin/users").status_code, 200)
        self.assertEqual(self.client.get("/admin/audit").status_code, 200)

        staff_client = app.test_client()
        self.login(staff_client, "staff@example.test")
        staff_dashboard = staff_client.get("/dashboard")
        self.assertEqual(staff_dashboard.status_code, 200)
        self.assertIn(b"Assignment-scoped data", staff_dashboard.data)
        self.assertNotIn(b"Waste trend", staff_dashboard.data)

    def test_csrf_rejects_state_change_without_token(self):
        self.login()
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            response = self.client.post("/logout")
            self.assertEqual(response.status_code, 400)
            self.assertIn("expired", response.get_data(as_text=True).lower())
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_csv_import_validation_and_tenant_scoping(self):
        self.login()
        invalid_extension = self.client.post("/api/import-csv", data={"csv_file": (io.BytesIO(b"Day,Waste_kg\nA,1\n"), "data.txt")})
        self.assertEqual(invalid_extension.status_code, 400)
        path_filename = self.client.post("/api/import-csv", data={"csv_file": (io.BytesIO(b"Day,Waste_kg\nA,1\n"), "../data.csv")})
        self.assertEqual(path_filename.status_code, 400)
        duplicate_days = self.client.post("/api/import-csv", data={"csv_file": (io.BytesIO(b"Day,Waste_kg\nA,1\nA,2\n"), "data.csv")})
        self.assertEqual(duplicate_days.status_code, 422)
        imported = self.client.post("/api/import-csv", data={"csv_file": (io.BytesIO(b"Day,Waste_kg\nA,4\nB,6\n"), "data.csv")})
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json["rows_loaded"], 2)
        self.assertEqual(self.client.get("/api/analytics").json["weekly_total_kg"], 10)

        with app.app_context():
            other_organization = create_organization("Other Organization", "other-organization")
            other_user = User(
                organization_id=other_organization.id,
                email="other@example.test",
                password_hash=generate_password_hash("CorrectHorseBatteryStaple1!"),
                role="Admin",
            )
            db.session.add(other_user)
            db.session.commit()
        other_client = app.test_client()
        self.login(other_client, "other@example.test")
        self.assertEqual(other_client.get("/api/analytics").json["weekly_total_kg"], 905)
        exported = other_client.get("/api/v1/data/export.csv")
        self.assertEqual(exported.status_code, 200)
        self.assertIn(b"Monday,120", exported.data)
        self.assertNotIn(b"A,4", exported.data)

    def test_admin_resource_creation_and_cross_tenant_foreign_keys(self):
        self.login()
        response = self.client.post("/api/v1/locations", json={"name": "New site"})
        self.assertEqual(response.status_code, 201)
        location_id = response.json["id"]
        created_bin = self.client.post("/api/v1/bins", json={"bin_id": "NEW-01", "location_id": location_id})
        self.assertEqual(created_bin.status_code, 201)
        with app.app_context():
            second_org = create_organization("Second Org", "second-org")
            second_location = db.session.scalar(select(Location.id).where(Location.organization_id == second_org.id))
            second_admin = User(organization_id=second_org.id, email="second@example.test", password_hash=generate_password_hash("CorrectHorseBatteryStaple1!"), role="Admin")
            db.session.add(second_admin)
            db.session.commit()
        cross_org_bin = self.client.post("/api/v1/bins", json={"bin_id": "BAD-01", "location_id": second_location})
        self.assertEqual(cross_org_bin.status_code, 404)

    def test_device_telemetry_requires_tenant_token_and_valid_payload(self):
        with app.app_context():
            token = "test-device-token-with-high-entropy"
            credential = DeviceCredential(
                organization_id=self.organization_id,
                name="test fixture",
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
            )
            db.session.add(credential)
            db.session.commit()
        endpoint = "/api/v1/bins/BIN-001/telemetry"
        self.assertEqual(self.client.post(endpoint, json={"fill_level": 20}).status_code, 401)
        invalid = self.client.post(endpoint, json={"fill_level": 101}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(invalid.status_code, 422)
        valid = self.client.post(endpoint, json={"fill_level": 72, "battery_level": 81}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(valid.status_code, 202)
        self.assertEqual(valid.json["status"], "Warning")

    def test_preferences_change_timezone_and_display_units(self):
        self.login()
        response = self.client.patch("/api/v1/organization/preferences", json={
            "unit": "lbs",
            "unit_system": "imperial",
            "timezone": "America/New_York",
            "language": "hi",
            "date_format": "%m/%d/%Y",
            "currency": "USD",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["timezone"], "America/New_York")
        self.assertIn(b"lbs", self.client.get("/dashboard").data)
        invalid = self.client.patch("/api/v1/organization/preferences", json={"timezone": "Mars/Olympus"})
        self.assertEqual(invalid.status_code, 422)

    def test_assigned_task_and_alert_workflows_are_scoped(self):
        self.login()
        with app.app_context():
            staff = User(
                organization_id=self.organization_id,
                email="staff@example.test",
                password_hash=generate_password_hash("CorrectHorseBatteryStaple1!"),
                role="Staff",
            )
            db.session.add(staff)
            db.session.commit()
            staff_id = staff.id
            selected_bin = db.session.scalar(select(SmartBin).where(SmartBin.organization_id == self.organization_id))
            bin_id = selected_bin.bin_id
            location_id = selected_bin.location_id
        task_response = self.client.post("/api/v1/tasks", json={
            "bin_id": bin_id,
            "location_id": location_id,
            "assigned_user_id": staff_id,
            "priority": "High",
            "scheduled_at": "2026-10-08T10:00:00+05:30",
        })
        self.assertEqual(task_response.status_code, 201)
        task_id = task_response.json["id"]
        alert_response = self.client.post("/api/v1/alerts", json={
            "alert_type": "collection_overdue",
            "severity": "High",
            "source": "manual",
            "location_id": location_id,
            "assigned_to": staff_id,
        })
        self.assertEqual(alert_response.status_code, 201)
        alert_id = alert_response.json["id"]
        staff_client = app.test_client()
        self.login(staff_client, "staff@example.test")
        self.assertEqual(staff_client.get("/api/v1/tasks").json["tasks"][0]["id"], task_id)
        self.assertEqual(staff_client.get("/api/v1/alerts").json["alerts"][0]["id"], alert_id)
        self.assertEqual(staff_client.patch(f"/api/v1/tasks/{task_id}", json={"status": "Completed"}).status_code, 200)
        self.assertEqual(staff_client.patch(f"/api/v1/alerts/{alert_id}", json={"status": "Resolved"}).status_code, 200)
        with app.app_context():
            self.assertEqual(db.session.get(CollectionTask, task_id).status, "Completed")
            self.assertEqual(db.session.get(OperationalAlert, alert_id).status, "Resolved")
            self.assertEqual(db.session.scalar(select(db.func.count(NotificationOutbox.id))), 2)

    def test_oversized_csv_upload_is_rejected(self):
        self.login()
        large_csv = io.BytesIO(b"Day,Waste_kg\nA," + b"1" * (2 * 1024 * 1024))
        response = self.client.post("/api/import-csv", data={"csv_file": (large_csv, "large.csv")})
        self.assertEqual(response.status_code, 413)

    def test_classification_form_has_csrf_protection(self):
        self.login()
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            response_without_csrf = self.client.post("/classification", data={"waste_type": "paper"})
            self.assertEqual(response_without_csrf.status_code, 400)

            page_content = self.client.get("/classification").get_data(as_text=True)
            self.assertIn('name="csrf_token"', page_content)

            import re
            token_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page_content)
            self.assertIsNotNone(token_match)
            token = token_match.group(1)
            valid_post = self.client.post("/classification", data={"waste_type": "paper", "csrf_token": token})
            self.assertEqual(valid_post.status_code, 200)
            self.assertIn(b"Paper", valid_post.data)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_dashboard_and_reports_render_safely_with_empty_locations(self):
        self.login()
        with app.app_context():
            db.session.query(CollectionTask).filter(CollectionTask.organization_id == self.organization_id).delete()
            db.session.query(OperationalAlert).filter(OperationalAlert.organization_id == self.organization_id).delete()
            db.session.query(SmartBin).filter(SmartBin.organization_id == self.organization_id).delete()
            db.session.query(Location).filter(Location.organization_id == self.organization_id).delete()
            db.session.commit()
        dash_response = self.client.get("/dashboard")
        self.assertEqual(dash_response.status_code, 200)
        self.assertIn("—".encode("utf-8"), dash_response.data)

        reports_response = self.client.get("/reports")
        self.assertEqual(reports_response.status_code, 200)
        self.assertIn("—".encode("utf-8"), reports_response.data)

    def test_ip_rate_limiting_on_login_post(self):
        limiter.enabled = True
        limiter.reset()
        try:
            for _ in range(5):
                resp = self.login(password="bad-pass")
                self.assertEqual(resp.status_code, 200)
            blocked = self.login(password="bad-pass")
            self.assertEqual(blocked.status_code, 429)
            self.assertIn(b"Too many requests", blocked.data)
        finally:
            limiter.reset()
            limiter.enabled = False

    def test_ip_rate_limiting_on_telemetry_api(self):
        with app.app_context():
            token = "telemetry-rate-limit-token-key"
            credential = DeviceCredential(
                organization_id=self.organization_id,
                name="telemetry rate test",
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
            )
            db.session.add(credential)
            db.session.commit()
        endpoint = "/api/v1/bins/BIN-001/telemetry"
        headers = {"Authorization": f"Bearer {token}"}
        limiter.enabled = True
        limiter.reset()
        try:
            for _ in range(60):
                resp = self.client.post(endpoint, json={"fill_level": 40}, headers=headers)
                self.assertIn(resp.status_code, (202, 422))
            blocked = self.client.post(endpoint, json={"fill_level": 40}, headers=headers)
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("Too many requests", blocked.json.get("error", ""))
        finally:
            limiter.reset()
            limiter.enabled = False

    def test_spoofed_forwarded_headers_not_trusted_by_default(self):
        limiter.enabled = True
        limiter.reset()
        try:
            for i in range(5):
                resp = self.client.post("/login", data={"email": "admin@example.test", "password": "wrong"}, headers={"X-Forwarded-For": f"198.51.100.{i}"})
                self.assertEqual(resp.status_code, 200)
            blocked = self.client.post("/login", data={"email": "admin@example.test", "password": "wrong"}, headers={"X-Forwarded-For": "198.51.100.99"})
            self.assertEqual(blocked.status_code, 429)
        finally:
            limiter.reset()
            limiter.enabled = False

    def test_trusted_proxy_configuration_processes_forwarded_ip_when_configured(self):
        from werkzeug.middleware.proxy_fix import ProxyFix
        original_wsgi = app.wsgi_app
        app.wsgi_app = ProxyFix(original_wsgi, x_for=1)
        limiter.enabled = True
        limiter.reset()
        try:
            for _ in range(5):
                resp = self.client.post("/login", data={"email": "admin@example.test", "password": "wrong"}, headers={"X-Forwarded-For": "203.0.113.10"})
                self.assertEqual(resp.status_code, 200)
            blocked_ip1 = self.client.post("/login", data={"email": "admin@example.test", "password": "wrong"}, headers={"X-Forwarded-For": "203.0.113.10"})
            self.assertEqual(blocked_ip1.status_code, 429)

            allowed_ip2 = self.client.post("/login", data={"email": "admin@example.test", "password": "wrong"}, headers={"X-Forwarded-For": "203.0.113.20"})
            self.assertEqual(allowed_ip2.status_code, 200)
        finally:
            app.wsgi_app = original_wsgi
            limiter.reset()
            limiter.enabled = False


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.database_directory = tempfile.TemporaryDirectory()
        self.original_uri = app.config["SQLALCHEMY_DATABASE_URI"]
        app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{Path(self.database_directory.name) / 'migration.sqlite'}"
        with app.app_context():
            db.engine.dispose()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        app.config["SQLALCHEMY_DATABASE_URI"] = self.original_uri
        self.database_directory.cleanup()

    def test_upgrade_creates_fresh_schema_at_head(self):
        with app.app_context():
            run_migrations(directory=str(Path(app.root_path) / "migrations"))
            revision = db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            self.assertEqual(revision, "4d0f3d0a9c1")
            self.assertIsNotNone(db.session.execute(text("SELECT 1 FROM organizations")).first())
            self.assertIn("full_name", {column["name"] for column in inspect(db.engine).get_columns("users")})

    def test_upgrade_adopts_complete_legacy_schema_without_losing_data(self):
        with app.app_context():
            db.create_all()
            organization = Organization(name="Legacy Organization", slug="legacy-organization")
            db.session.add(organization)
            db.session.commit()
            organization_id = organization.id

            run_migrations(directory=str(Path(app.root_path) / "migrations"))

            revision = db.session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            self.assertEqual(revision, "4d0f3d0a9c1")
            preserved = db.session.get(Organization, organization_id)
            self.assertEqual(preserved.name, "Legacy Organization")
            self.assertIn("full_name", {column["name"] for column in inspect(db.engine).get_columns("users")})


if __name__ == "__main__":
    unittest.main()
