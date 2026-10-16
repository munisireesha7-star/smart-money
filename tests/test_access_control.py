import csv
import re
import sqlite3
import struct
import tempfile
import unittest
import zlib
from datetime import datetime
from io import BytesIO
from pathlib import Path

import app as smart_money


def make_png():
    def chunk(name, data):
        payload = name + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.original_database = smart_money.DATABASE
        self.original_photo_dir = smart_money.CASH_PHOTO_DIR
        self.original_cookie_secure = smart_money.app.config["SESSION_COOKIE_SECURE"]
        smart_money.DATABASE = root / "test.sqlite"
        smart_money.CASH_PHOTO_DIR = root / "uploads"
        smart_money.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=True,
            SESSION_COOKIE_SECURE=False,
        )
        smart_money.initialize_database()
        self.alice = smart_money.app.test_client()
        self.bob = smart_money.app.test_client()

    def tearDown(self):
        smart_money.DATABASE = self.original_database
        smart_money.CASH_PHOTO_DIR = self.original_photo_dir
        smart_money.app.config["SESSION_COOKIE_SECURE"] = self.original_cookie_secure
        self.temp_dir.cleanup()

    def csrf_token(self, client, path):
        response = client.get(path)
        self.assertEqual(response.status_code, 200)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def register(self, client, username, password="a-unique-long-password-123"):
        token = self.csrf_token(client, "/register")
        response = client.post(
            "/register",
            data={"csrf_token": token, "username": username, "password": password},
        )
        self.assertEqual(response.status_code, 302)

    def test_users_can_only_access_their_own_financial_records_and_photos(self):
        self.assertEqual(self.alice.get("/records").status_code, 302)
        self.register(self.alice, "alice")
        self.register(self.bob, "bob")

        recorded_at = datetime.now().strftime("%Y-%m-%dT%H:%M")
        token = self.csrf_token(self.alice, "/records")
        created = self.alice.post(
            "/records",
            data={
                "csrf_token": token,
                "direction": "added",
                "amount": "100.00",
                "location": "Alice home safe",
                "transaction_time": recorded_at,
                "description": "Private record",
                "photo": (BytesIO(make_png()), "safe.png"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(created.status_code, 302)
        with smart_money.connect_db() as db:
            alice_user = db.execute(
                "SELECT id FROM users WHERE username = 'alice'"
            ).fetchone()
            cash = db.execute(
                "SELECT id, photo_filename, photo_data FROM cash_transactions WHERE user_id = ?",
                (alice_user["id"],),
            ).fetchone()
        self.assertIsNotNone(cash["photo_filename"])
        self.assertEqual(cash["photo_data"], make_png())

        alice_records = self.alice.get("/records")
        bob_records = self.bob.get("/records")
        self.assertIn(b"Alice home safe", alice_records.data)
        self.assertNotIn(b"Alice home safe", bob_records.data)
        photo_url = f"/records/photos/{cash['photo_filename']}"
        self.assertEqual(self.alice.get(photo_url).status_code, 200)
        self.assertEqual(self.bob.get(photo_url).status_code, 404)
        edit_response = self.bob.get(f"/records/{cash['id']}/edit")
        self.assertEqual(edit_response.status_code, 302)
        self.assertEqual(edit_response.headers["Location"], "/records")
        self.assertNotIn(b"Alice home safe", edit_response.data)

        bob_alerts = self.bob.get("/alerts")
        self.assertNotIn(b"Alice home safe", bob_alerts.data)
        bob_reports = self.bob.get(
            "/reports",
            query_string={"location": "Alice home safe"},
        )
        self.assertNotIn(b"100.00", bob_reports.data)
        bob_csv = self.bob.get(
            "/reports/cash.csv",
            query_string={
                "start_date": "2020-01-01",
                "end_date": "2030-01-01",
                "location": "Alice home safe",
            },
        )
        self.assertNotIn(b"Alice home safe", bob_csv.data)

        token = self.csrf_token(self.alice, "/alerts")
        check_response = self.alice.post(
            "/alerts/cash-checks",
            data={
                "csrf_token": token,
                "location": "Alice home safe",
                "actual_amount": "50.00",
                "counted_at": recorded_at,
                "ledger_complete": "yes",
            },
        )
        self.assertEqual(check_response.status_code, 302)
        with smart_money.connect_db() as db:
            check = db.execute(
                "SELECT id, status FROM cash_checks WHERE user_id = ?",
                (alice_user["id"],),
            ).fetchone()
            notification = db.execute(
                "SELECT id, read_at FROM app_notifications WHERE user_id = ?",
                (alice_user["id"],),
            ).fetchone()
        self.assertEqual(check["status"], "New")
        self.assertIsNotNone(notification)

        self.assertNotIn(b"Alice home safe", self.bob.get("/alerts").data)
        bob_token = self.csrf_token(self.bob, "/alerts")
        self.bob.post(
            f"/alerts/cash-checks/{check['id']}/status",
            data={"csrf_token": bob_token, "status": "Resolved"},
        )
        self.bob.post(
            f"/notifications/{notification['id']}/read",
            data={"csrf_token": bob_token},
        )
        with smart_money.connect_db() as db:
            unchanged_check = db.execute(
                "SELECT status FROM cash_checks WHERE id = ?",
                (check["id"],),
            ).fetchone()
            unchanged_notification = db.execute(
                "SELECT read_at FROM app_notifications WHERE id = ?",
                (notification["id"],),
            ).fetchone()
        self.assertEqual(unchanged_check["status"], "New")
        self.assertIsNone(unchanged_notification["read_at"])

        token = self.csrf_token(self.alice, "/records")
        self.alice.post(
            "/records",
            data={
                "csrf_token": token,
                "record_type": "budget",
                "title": "Alice private budget entry",
                "amount": "25",
                "category": "Other",
                "kind": "expense",
                "transaction_date": recorded_at[:10],
                "note": "",
            },
        )
        with smart_money.connect_db() as db:
            budget_entry = db.execute(
                "SELECT id FROM transactions WHERE user_id = ? "
                "AND title = 'Alice private budget entry'",
                (alice_user["id"],),
            ).fetchone()
        self.assertIsNotNone(budget_entry)
        self.assertNotIn(b"Alice private budget entry", self.bob.get("/records").data)
        token = self.csrf_token(self.bob, "/records")
        self.bob.post(
            f"/budget-records/{budget_entry['id']}/delete",
            data={"csrf_token": token},
        )
        with smart_money.connect_db() as db:
            self.assertIsNotNone(
                db.execute(
                    "SELECT id FROM transactions WHERE id = ? AND user_id = ?",
                    (budget_entry["id"], alice_user["id"]),
                ).fetchone()
            )

    def test_csrf_logout_and_user_scoped_settings(self):
        self.register(self.alice, "charlie")
        self.register(self.bob, "dana")

        self.assertEqual(
            self.alice.post(
                "/register",
                data={"username": "forgery", "password": "not-a-valid-request"},
            ).status_code,
            400,
        )
        self.assertEqual(self.alice.post("/notifications/test").status_code, 400)
        token = self.csrf_token(self.alice, "/settings")
        self.alice.post(
            "/settings",
            data={
                "csrf_token": token,
                "monthly_budget": "777",
                "alert_threshold": "88",
                "currency": "INR",
            },
        )
        with smart_money.connect_db() as db:
            alice_id = db.execute(
                "SELECT id FROM users WHERE username = 'charlie'"
            ).fetchone()["id"]
            bob_id = db.execute(
                "SELECT id FROM users WHERE username = 'dana'"
            ).fetchone()["id"]
            alice_settings = db.execute(
                "SELECT monthly_budget, alert_threshold, currency "
                "FROM user_settings WHERE user_id = ?",
                (alice_id,),
            ).fetchone()
            bob_settings = db.execute(
                "SELECT monthly_budget, alert_threshold, currency "
                "FROM user_settings WHERE user_id = ?",
                (bob_id,),
            ).fetchone()
        self.assertEqual(tuple(alice_settings), (777.0, 88.0, "INR"))
        self.assertEqual(tuple(bob_settings), (4000.0, 450.0, "USD"))

        token = self.csrf_token(self.alice, "/dashboard")
        logout = self.alice.post("/logout", data={"csrf_token": token})
        self.assertEqual(logout.status_code, 302)
        self.assertEqual(self.alice.get("/dashboard").status_code, 302)

        with self.alice.session_transaction() as session:
            self.assertNotIn("user_id", session)
        smart_money.app.config["SESSION_COOKIE_SECURE"] = True
        self.bob.get("/dashboard")
        cookie = self.bob.get_cookie("session")
        self.assertIsNotNone(cookie)
        self.assertTrue(cookie.http_only)
        self.assertTrue(cookie.secure)
        self.assertEqual(cookie.same_site, "Lax")

    def test_password_hashing_and_login(self):
        self.register(self.alice, "erin", "correct-horse-long-passphrase")
        with smart_money.connect_db() as db:
            password_hash = db.execute(
                "SELECT password_hash FROM users WHERE username = 'erin'"
            ).fetchone()["password_hash"]
        self.assertNotEqual(password_hash, "correct-horse-long-passphrase")
        self.assertTrue(
            smart_money.check_password_hash(
                password_hash, "correct-horse-long-passphrase"
            )
        )

        self.alice.post("/logout", data={"csrf_token": self.csrf_token(self.alice, "/dashboard")})
        login_token = self.csrf_token(self.alice, "/login")
        response = self.alice.post(
            "/login",
            data={
                "csrf_token": login_token,
                "username": "erin",
                "password": "correct-horse-long-passphrase",
            },
        )
        self.assertEqual(response.status_code, 302)
        dashboard = self.alice.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Good to see you, erin", dashboard.data)

    def test_cash_ledger_alerts_reports_and_csv_work_end_to_end(self):
        self.register(self.alice, "cashuser")

        for path in ("/", "/dashboard", "/records", "/alerts", "/reports", "/settings"):
            with self.subTest(path=path):
                response = self.alice.get(path)
                self.assertEqual(response.status_code, 200)

        recorded_at = datetime.now().replace(second=0, microsecond=0)
        timestamp = recorded_at.strftime("%Y-%m-%dT%H:%M")

        for direction, amount, description in (
            ("added", "200.00", "Cash put into safe"),
            ("removed", "50.00", "Cash taken from safe"),
        ):
            token = self.csrf_token(self.alice, "/records")
            response = self.alice.post(
                "/records",
                data={
                    "csrf_token": token,
                    "direction": direction,
                    "amount": amount,
                    "location": "Test safe",
                    "transaction_time": timestamp,
                    "description": description,
                },
            )
            self.assertEqual(response.status_code, 302)

        records = self.alice.get("/records")
        self.assertIn("₹ 150.00".encode(), records.data)
        self.assertIn(b"Cash put into safe", records.data)
        self.assertIn(b"Cash taken from safe", records.data)

        with smart_money.connect_db() as db:
            user_id = db.execute(
                "SELECT id FROM users WHERE username = 'cashuser'"
            ).fetchone()["id"]
            stored_balance = db.execute(
                "SELECT SUM(CASE WHEN direction = 'added' THEN amount_paise "
                "ELSE -amount_paise END) FROM cash_transactions WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        self.assertEqual(stored_balance, 15000)

        token = self.csrf_token(self.alice, "/alerts")
        response = self.alice.post(
            "/alerts/cash-checks",
            data={
                "csrf_token": token,
                "location": "Test safe",
                "actual_amount": "125.00",
                "counted_at": timestamp,
                "ledger_complete": "yes",
            },
        )
        self.assertEqual(response.status_code, 302)

        alerts = self.alice.get("/alerts")
        self.assertIn(b"Possible cash discrepancies", alerts.data)
        self.assertIn(b"New", alerts.data)
        self.assertIn(b"Test safe", alerts.data)
        self.assertIn("₹ 150.00".encode(), alerts.data)
        self.assertIn("₹ 125.00".encode(), alerts.data)
        self.assertIn("₹ 25.00".encode(), alerts.data)
        with smart_money.connect_db() as db:
            check = db.execute(
                "SELECT id, expected_paise, actual_paise, difference_paise, status "
                "FROM cash_checks WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        self.assertEqual(
            tuple(check),
            (check["id"], 15000, 12500, -2500, "New"),
        )

        query = {
            "start_date": recorded_at.date().isoformat(),
            "end_date": recorded_at.date().isoformat(),
            "location": "Test safe",
        }
        report = self.alice.get("/reports", query_string=query)
        self.assertEqual(report.status_code, 200)
        for expected in ("₹ 250.00", "₹ 200.00", "₹ 50.00", "₹ 150.00"):
            self.assertIn(expected.encode(), report.data)
        self.assertIn(b"Unresolved discrepancies", report.data)
        self.assertIn(b"<strong>1</strong>", report.data)
        self.assertIn(b"const cashChartData", report.data)
        self.assertIn(timestamp.replace("T", " ").encode(), report.data)

        export = self.alice.get("/reports/cash.csv", query_string=query)
        self.assertEqual(export.status_code, 200)
        self.assertTrue(export.mimetype.startswith("text/csv"))
        self.assertIn("attachment;".encode(), export.headers["Content-Disposition"].encode())
        csv_rows = list(csv.reader(export.data.decode("utf-8-sig").splitlines()))
        self.assertEqual(
            csv_rows[0],
            ["Type", "Amount (INR)", "Storage location", "Date and time", "Description"],
        )
        self.assertEqual(len(csv_rows), 3)
        self.assertEqual({row[1] for row in csv_rows[1:]}, {"200.00", "50.00"})
        self.assertEqual({row[2] for row in csv_rows[1:]}, {"Test safe"})

    def test_legacy_unowned_rows_stay_inaccessible_after_schema_migration(self):
        legacy_database = Path(self.temp_dir.name) / "legacy.sqlite"
        connection = sqlite3.connect(legacy_database)
        connection.executescript(
            """
            CREATE TABLE transactions (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
                amount REAL NOT NULL, kind TEXT NOT NULL, transaction_date TEXT NOT NULL,
                note TEXT DEFAULT ''
            );
            CREATE TABLE cash_transactions (
                id INTEGER PRIMARY KEY, direction TEXT NOT NULL, amount_paise INTEGER NOT NULL,
                location TEXT NOT NULL, transaction_time TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', photo_filename TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE cash_checks (
                id INTEGER PRIMARY KEY, location TEXT NOT NULL, expected_paise INTEGER NOT NULL,
                actual_paise INTEGER NOT NULL, difference_paise INTEGER NOT NULL,
                counted_at TEXT NOT NULL, ledger_latest_at TEXT NOT NULL,
                ledger_complete INTEGER NOT NULL, status TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE cash_check_status_history (
                id INTEGER PRIMARY KEY, check_id INTEGER NOT NULL,
                old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE app_notifications (
                id INTEGER PRIMARY KEY, notification_type TEXT NOT NULL, title TEXT NOT NULL,
                message TEXT NOT NULL, check_id INTEGER, created_at TEXT NOT NULL, read_at TEXT
            );
            CREATE TABLE notification_preferences (
                id INTEGER PRIMARY KEY, discrepancy_notifications INTEGER NOT NULL DEFAULT 1,
                status_notifications INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY, monthly_budget REAL NOT NULL DEFAULT 4000,
                alert_threshold REAL NOT NULL DEFAULT 450, currency TEXT NOT NULL DEFAULT 'USD'
            );
            INSERT INTO transactions
                (title, category, amount, kind, transaction_date, note)
                VALUES ('Legacy private entry', 'Other', 10, 'expense', '2026-01-01', '');
            INSERT INTO cash_transactions
                (direction, amount_paise, location, transaction_time)
                VALUES ('added', 12345, 'Legacy private safe', '2026-01-01T12:00');
            """
        )
        connection.commit()
        connection.close()

        smart_money.DATABASE = legacy_database
        smart_money.initialize_database()
        self.register(self.alice, "legacyuser")
        records = self.alice.get("/records")
        reports = self.alice.get("/reports")
        self.assertNotIn(b"Legacy private entry", records.data)
        self.assertNotIn(b"Legacy private safe", records.data)
        self.assertNotIn(b"Legacy private safe", reports.data)
        with smart_money.connect_db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM transactions WHERE user_id IS NULL"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM cash_transactions WHERE user_id IS NULL"
                ).fetchone()[0],
                1,
            )

    def test_postgres_sql_adapter_emits_supported_sql(self):
        previous_url = smart_money.app.config.get("DATABASE_URL")
        smart_money.app.config["DATABASE_URL"] = "postgresql://test"
        try:
            schema = smart_money.adapt_sql(
                "CREATE TABLE cash_transactions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, amount REAL, photo BLOB)"
            )
            self.assertIn("SERIAL PRIMARY KEY", schema)
            self.assertIn("DOUBLE PRECISION", schema)
            self.assertIn("BYTEA", schema)

            query = smart_money.adapt_sql(
                "SELECT * FROM transactions WHERE title LIKE ? "
                "AND username = ? COLLATE NOCASE"
            )
            self.assertIn("ILIKE %s", query)
            self.assertIn("LOWER(username) = LOWER(%s)", query)
            self.assertNotIn("NOCASE", query)
        finally:
            if previous_url is None:
                smart_money.app.config.pop("DATABASE_URL", None)
            else:
                smart_money.app.config["DATABASE_URL"] = previous_url


if __name__ == "__main__":
    unittest.main()
