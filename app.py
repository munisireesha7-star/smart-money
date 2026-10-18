import csv
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from io import BytesIO, StringIO
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from flask_wtf.csrf import CSRFProtect
import psycopg
from werkzeug.security import check_password_hash, generate_password_hash


MAX_PHOTO_SIZE = 4 * 1024 * 1024
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SMART_MONEY_SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SMART_MONEY_COOKIE_SECURE", "").lower()
    in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=MAX_PHOTO_SIZE + 256 * 1024,
)
csrf = CSRFProtect(app)
DATABASE = Path(__file__).with_name("smart_money.db")
CASH_PHOTO_DIR = Path(app.instance_path) / "uploads" / "cash"
DATABASE_URL = os.environ.get("DATABASE_URL")

if not os.environ.get("SMART_MONEY_SECRET_KEY"):
    if os.environ.get("VERCEL"):
        raise RuntimeError(
            "SMART_MONEY_SECRET_KEY is required on Vercel. Configure a persistent secret in Project Settings."
        )
    app.logger.warning(
        "SMART_MONEY_SECRET_KEY is not set; sessions will not survive app restarts. "
        "Set a strong random secret in the environment for persistent deployments."
    )


class HybridRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)



def postgres_row_factory(cursor):
    if cursor.description is None:
        return lambda values: values

    names = [column.name for column in cursor.description]
    return lambda values: HybridRow(zip(names, values))



def using_postgres():
    return bool(app.config.get("DATABASE_URL") or DATABASE_URL)


def adapt_sql(statement):
    if not using_postgres():
        return statement
    statement = statement.replace(
        "INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY"
    )
    statement = re.sub(r"\bREAL\b", "DOUBLE PRECISION", statement, flags=re.IGNORECASE)
    statement = re.sub(r"\bBLOB\b", "BYTEA", statement, flags=re.IGNORECASE)
    statement = re.sub(
        r"\b([A-Za-z_]\w*)\s*=\s*\?\s+COLLATE NOCASE\b",
        r"LOWER(\1) = LOWER(?)",
        statement,
        flags=re.IGNORECASE,
    )
    statement = re.sub(r"\s+COLLATE NOCASE", "", statement, flags=re.IGNORECASE)
    statement = re.sub(r"\bLIKE\b", "ILIKE", statement, flags=re.IGNORECASE)
    statement = statement.replace("?", "%s")
    return statement


class DatabaseConnection:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, statement, parameters=()):
        return self.connection.execute(adapt_sql(statement), parameters)

    def executemany(self, statement, parameters):
        return self.connection.executemany(adapt_sql(statement), parameters)


@contextmanager
def connect_db():
    database_url = app.config.get("DATABASE_URL") or DATABASE_URL
    if database_url:
        connection = psycopg.connect(
            database_url,
            row_factory=postgres_row_factory,
            connect_timeout=10,
        )
    else:
        if os.environ.get("VERCEL"):
            raise RuntimeError(
                "DATABASE_URL is required on Vercel. Configure a persistent PostgreSQL database."
            )
        connection = sqlite3.connect(DATABASE)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
    connection = DatabaseConnection(connection)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@app.before_request
def load_authenticated_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        with connect_db() as db:
            g.user = db.execute(
                "SELECT id, username FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if g.user is None:
            session.clear()
    if request.endpoint not in ("login", "register", "static"):
        if g.user is None:
            return redirect(url_for("login"))


def initialize_database():
    with connect_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                kind TEXT NOT NULL CHECK(kind IN ('expense', 'income')),
                transaction_date TEXT NOT NULL,
                note TEXT DEFAULT '',
                user_id INTEGER REFERENCES users(id)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS cash_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL CHECK(direction IN ('added', 'removed')),
                amount_paise BIGINT NOT NULL CHECK(amount_paise > 0),
                location TEXT NOT NULL,
                transaction_time TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                photo_filename TEXT,
                photo_data BLOB,
                user_id INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS cash_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location TEXT NOT NULL,
                expected_paise BIGINT NOT NULL,
                actual_paise BIGINT NOT NULL,
                difference_paise BIGINT NOT NULL,
                counted_at TEXT NOT NULL,
                ledger_latest_at TEXT NOT NULL,
                ledger_complete INTEGER NOT NULL CHECK(ledger_complete IN (0, 1)),
                status TEXT CHECK(status IN ('New', 'Under Review', 'Resolved')),
                user_id INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS cash_check_status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_id INTEGER NOT NULL REFERENCES cash_checks(id) ON DELETE CASCADE,
                old_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id),
                changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_preferences (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                discrepancy_notifications INTEGER NOT NULL DEFAULT 1
                    CHECK(discrepancy_notifications IN (0, 1)),
                status_notifications INTEGER NOT NULL DEFAULT 1
                    CHECK(status_notifications IN (0, 1))
            )
            """
        )
        db.execute(
            "INSERT INTO notification_preferences (id) VALUES (1) "
            "ON CONFLICT (id) DO NOTHING"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_type TEXT NOT NULL
                    CHECK(notification_type IN ('discrepancy', 'status', 'test')),
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                check_id INTEGER,
                created_at TEXT NOT NULL,
                read_at TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                monthly_budget REAL NOT NULL DEFAULT 4000,
                alert_threshold REAL NOT NULL DEFAULT 450,
                currency TEXT NOT NULL DEFAULT 'USD'
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_notification_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                discrepancy_notifications INTEGER NOT NULL DEFAULT 1
                    CHECK(discrepancy_notifications IN (0, 1)),
                status_notifications INTEGER NOT NULL DEFAULT 1
                    CHECK(status_notifications IN (0, 1))
            )
            """
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_idx "
            "ON users (LOWER(username))"
        )
        for table in (
            "transactions",
            "cash_transactions",
            "cash_checks",
            "cash_check_status_history",
            "app_notifications",
        ):
            if using_postgres():
                columns = {
                    column["column_name"]
                    for column in db.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = ?",
                        (table,),
                    ).fetchall()
                }
            else:
                columns = {
                    column["name"]
                    for column in db.execute(f"PRAGMA table_info({table})").fetchall()
                }
            if "user_id" not in columns:
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN user_id INTEGER REFERENCES users(id)"
                )
            if table == "cash_transactions" and "photo_data" not in columns:
                db.execute(
                    "ALTER TABLE cash_transactions ADD COLUMN photo_data "
                    + ("BYTEA" if using_postgres() else "BLOB")
                )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                monthly_budget REAL NOT NULL DEFAULT 4000,
                alert_threshold REAL NOT NULL DEFAULT 450,
                currency TEXT NOT NULL DEFAULT 'USD'
            )
            """
        )
        db.execute(
            "INSERT INTO settings (id, monthly_budget, alert_threshold, currency) "
            "VALUES (1, 4000, 450, 'USD') ON CONFLICT (id) DO NOTHING"
        )


def get_settings():
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (g.user["id"],)
        ).fetchone()


def get_month_totals():
    month = date.today().strftime("%Y-%m")
    with connect_db() as db:
        rows = db.execute(
            "SELECT kind, COALESCE(SUM(amount), 0) AS total FROM transactions "
            "WHERE user_id = ? AND substr(transaction_date, 1, 7) = ? GROUP BY kind",
            (g.user["id"], month),
        ).fetchall()
    totals = {row["kind"]: row["total"] for row in rows}
    return totals.get("income", 0), totals.get("expense", 0)


def get_alerts():
    settings = get_settings()
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM transactions WHERE kind = 'expense' AND amount >= ? "
            "AND user_id = ? ORDER BY transaction_date DESC, id DESC",
            (settings["alert_threshold"], g.user["id"]),
        ).fetchall()


def get_open_cash_discrepancy_count():
    with connect_db() as db:
        return db.execute(
            "SELECT COUNT(*) FROM cash_checks WHERE user_id = ? "
            "AND status IN ('New', 'Under Review')",
            (g.user["id"],),
        ).fetchone()[0]


def get_notification_preferences():
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM user_notification_preferences WHERE user_id = ?",
            (g.user["id"],),
        ).fetchone()


def get_unread_notification_count():
    with connect_db() as db:
        return db.execute(
            "SELECT COUNT(*) FROM app_notifications WHERE user_id = ? AND read_at IS NULL",
            (g.user["id"],),
        ).fetchone()[0]


def create_notification(db, notification_type, title, message, check_id=None):
    db.execute(
        "INSERT INTO app_notifications "
        "(notification_type, title, message, check_id, user_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            notification_type,
            title,
            message,
            check_id,
            g.user["id"],
            datetime.now().isoformat(timespec="seconds"),
        ),
    )


def money(value):
    return f"{value:,.2f}"


def parse_cash_form(form, files):
    direction = form.get("direction", "").strip()
    location = form.get("location", "").strip()
    transaction_time = form.get("transaction_time", "").strip()
    description = form.get("description", "").strip()
    try:
        amount = Decimal(form.get("amount", "").strip())
        if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999999.99"):
            raise ValueError
        if amount.quantize(Decimal("0.01")) != amount:
            raise ValueError
        amount_paise = int(amount * 100)
        if Decimal(amount_paise) != amount * 100:
            raise ValueError
        datetime.strptime(transaction_time, "%Y-%m-%dT%H:%M")
        if direction not in ("added", "removed") or not location or len(location) > 80:
            raise ValueError
        if len(description) > 500:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise ValueError(
            "Enter a valid amount (up to two decimal places), location, and date and time."
        ) from None

    photo = files.get("photo")
    photo_data = None
    photo_extension = None
    if photo and photo.filename:
        photo_data = photo.stream.read(MAX_PHOTO_SIZE + 1)
        if len(photo_data) > MAX_PHOTO_SIZE:
            raise ValueError("The photo must be 4 MB or smaller.")
        if photo_data.startswith(b"\xff\xd8\xff"):
            photo_extension = ".jpg"
        elif photo_data.startswith(b"\x89PNG\r\n\x1a\n"):
            photo_extension = ".png"
        elif len(photo_data) >= 12 and photo_data[:4] == b"RIFF" and photo_data[8:12] == b"WEBP":
            photo_extension = ".webp"
        else:
            raise ValueError("Choose a valid JPG, PNG, or WebP photo.")

    return {
        "direction": direction,
        "amount_paise": amount_paise,
        "location": location,
        "transaction_time": transaction_time,
        "description": description,
        "photo_data": photo_data,
        "photo_extension": photo_extension,
    }


def parse_cash_count(form):
    location = form.get("location", "").strip()
    counted_at = form.get("counted_at", "").strip()
    try:
        actual_amount = Decimal(form.get("actual_amount", "").strip())
    except InvalidOperation:
        raise ValueError(
            "Enter a valid actual amount (zero or more, up to two decimal places), location, and past date and time."
        ) from None
    if (
        not actual_amount.is_finite()
        or actual_amount < 0
        or actual_amount > Decimal("999999999999.99")
        or actual_amount.quantize(Decimal("0.01")) != actual_amount
        or not location
        or len(location) > 80
    ):
        raise ValueError(
            "Enter a valid actual amount (zero or more, up to two decimal places), location, and past date and time."
        )
    try:
        counted_time = datetime.strptime(counted_at, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValueError(
            "Enter a valid actual amount (zero or more, up to two decimal places), location, and past date and time."
        ) from None
    if counted_time > datetime.now():
        raise ValueError("The count time cannot be in the future.")
    if form.get("ledger_complete") != "yes":
        raise ValueError(
            "Confirm that all cash added and removed at this location has been recorded before comparing."
        )
    return location, int(actual_amount * 100), counted_at, counted_time


def new_cash_photo_filename(extension):
    return f"{uuid4().hex}{extension}"


def remove_cash_photo(filename):
    if filename and Path(filename).name == filename:
        path = CASH_PHOTO_DIR / filename
        if path.is_file():
            path.unlink()


@app.context_processor
def inject_shared_data():
    if g.user is None:
        return {
            "settings": {"monthly_budget": 0, "alert_threshold": 0, "currency": "INR"},
            "currency": "INR",
            "month_income": 0,
            "month_expenses": 0,
            "alert_count": 0,
            "cash_discrepancy_count": 0,
            "unread_notification_count": 0,
            "money": money,
        }
    settings = get_settings()
    income, expenses = get_month_totals()
    return {
        "settings": settings,
        "currency": settings["currency"],
        "month_income": income,
        "month_expenses": expenses,
        "alert_count": len(get_alerts()),
        "cash_discrepancy_count": get_open_cash_discrepancy_count(),
        "unread_notification_count": get_unread_notification_count(),
        "money": money,
    }


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
            flash("Choose a username with 3–40 letters, numbers, dots, dashes, or underscores.", "error")
            return render_template("auth.html", mode="register")
        if len(password) < 12 or len(password) > 128:
            flash("Choose a password between 12 and 128 characters.", "error")
            return render_template("auth.html", mode="register")
        try:
            with connect_db() as db:
                cursor = db.execute(
                    "INSERT INTO users (username, password_hash) VALUES (?, ?) RETURNING id",
                    (username, generate_password_hash(password)),
                )
                user_id = cursor.fetchone()["id"]
                db.execute(
                    "INSERT INTO user_settings (user_id) VALUES (?)", (user_id,)
                )
                db.execute(
                    "INSERT INTO user_notification_preferences (user_id) VALUES (?)",
                    (user_id,),
                )
        except (sqlite3.IntegrityError, psycopg.errors.UniqueViolation):
            flash("That username is already in use. Please choose another.", "error")
            return render_template("auth.html", mode="register")
        session.clear()
        session["user_id"] = user_id
        session.permanent = True
        flash("Your account is ready. Add your first cash record to get started.", "success")
        return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if len(password) > 128:
            flash("Username or password is incorrect.", "error")
            return render_template("auth.html", mode="login")
        with connect_db() as db:
            user = db.execute(
                "SELECT id, username, password_hash FROM users "
                "WHERE LOWER(username) = LOWER(?)",
                (username,),
            ).fetchone()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Username or password is incorrect.", "error")
            return render_template("auth.html", mode="login")
        session.clear()
        session["user_id"] = user["id"]
        session.permanent = True
        return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="login")


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/")
def home():
    return render_template("home.html", page="home")


@app.route("/dashboard")
def dashboard():
    income, expenses = get_month_totals()
    settings = get_settings()
    with connect_db() as db:
        recent = db.execute(
            "SELECT * FROM transactions WHERE user_id = ? "
            "ORDER BY transaction_date DESC, id DESC LIMIT 5",
            (g.user["id"],),
        ).fetchall()
        category_totals = db.execute(
            "SELECT category, SUM(amount) AS total FROM transactions "
            "WHERE user_id = ? AND kind = 'expense' AND substr(transaction_date, 1, 7) = ? "
            "GROUP BY category ORDER BY total DESC LIMIT 5",
            (g.user["id"], date.today().strftime("%Y-%m")),
        ).fetchall()
    return render_template(
        "dashboard.html",
        page="dashboard",
        income=income,
        expenses=expenses,
        budget=settings["monthly_budget"],
        recent=recent,
        category_totals=category_totals,
        alerts=get_alerts(),
    )


@app.route("/records", methods=["GET", "POST"])
def records():
    if request.method == "POST":
        if request.form.get("record_type") == "budget":
            return add_budget_record()
        try:
            record = parse_cash_form(request.form, request.files)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("records"))
        photo_filename = None
        if record["photo_data"]:
            photo_filename = new_cash_photo_filename(record["photo_extension"])
        with connect_db() as db:
            db.execute(
                "INSERT INTO cash_transactions "
                "(direction, amount_paise, location, transaction_time, description, "
                "photo_filename, photo_data, user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["direction"],
                    record["amount_paise"],
                    record["location"],
                    record["transaction_time"],
                    record["description"],
                    photo_filename,
                    record["photo_data"],
                    g.user["id"],
                ),
            )
        flash("Your cash transaction was saved.", "success")
        return redirect(url_for("records"))

    search = request.args.get("search", "").strip()
    direction_filter = request.args.get("direction", "")
    query = "SELECT * FROM cash_transactions WHERE user_id = ?"
    parameters = [g.user["id"]]
    if search:
        query += " AND (location LIKE ? OR description LIKE ?)"
        parameters.extend([f"%{search}%"] * 2)
    if direction_filter in ("added", "removed"):
        query += " AND direction = ?"
        parameters.append(direction_filter)
    query += " ORDER BY transaction_time DESC, id DESC"
    budget_search = request.args.get("budget_search", "").strip()
    budget_kind = request.args.get("budget_kind", "")
    budget_query = "SELECT * FROM transactions WHERE user_id = ?"
    budget_parameters = [g.user["id"]]
    if budget_search:
        budget_query += " AND (title LIKE ? OR category LIKE ? OR note LIKE ?)"
        budget_parameters.extend([f"%{budget_search}%"] * 3)
    if budget_kind in ("expense", "income"):
        budget_query += " AND kind = ?"
        budget_parameters.append(budget_kind)
    budget_query += " ORDER BY transaction_date DESC, id DESC"
    with connect_db() as db:
        cash_rows = db.execute(query, parameters).fetchall()
        budget_rows = db.execute(budget_query, budget_parameters).fetchall()
        balance = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction = 'added' "
            "THEN amount_paise ELSE -amount_paise END), 0) FROM cash_transactions WHERE user_id = ?",
            (g.user["id"],),
        ).fetchone()[0]
        location_balances = db.execute(
            "SELECT location, COALESCE(SUM(CASE WHEN direction = 'added' "
            "THEN amount_paise ELSE -amount_paise END), 0) AS balance_paise "
            "FROM cash_transactions WHERE user_id = ? GROUP BY location ORDER BY location COLLATE NOCASE",
            (g.user["id"],),
        ).fetchall()
    return render_template(
        "records.html",
        page="records",
        cash_transactions=cash_rows,
        cash_balance=balance,
        location_balances=location_balances,
        search=search,
        direction_filter=direction_filter,
        budget_transactions=budget_rows,
        budget_search=budget_search,
        budget_kind=budget_kind,
        today=datetime.now().strftime("%Y-%m-%dT%H:%M"),
    )


def add_budget_record():
    title = request.form.get("title", "").strip()
    category = request.form.get("category", "").strip()
    kind = request.form.get("kind", "").strip()
    note = request.form.get("note", "").strip()
    transaction_date = request.form.get("transaction_date", "")
    try:
        amount = float(request.form.get("amount", ""))
        datetime.strptime(transaction_date, "%Y-%m-%d")
        if (
            not title
            or len(title) > 100
            or not category
            or len(category) > 80
            or len(note) > 500
            or kind not in ("expense", "income")
            or not math.isfinite(amount)
            or amount <= 0
            or amount > 999999999999.99
        ):
            raise ValueError
    except ValueError:
        flash("Please enter a title, category, valid date, and amount greater than zero.", "error")
        return redirect(url_for("records"))
    with connect_db() as db:
        db.execute(
            "INSERT INTO transactions "
            "(title, category, amount, kind, transaction_date, note, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, category, amount, kind, transaction_date, note, g.user["id"]),
        )
    flash("Your money record was added.", "success")
    return redirect(url_for("records"))


@app.post("/budget-records/<int:transaction_id>/delete")
def delete_budget_record(transaction_id):
    with connect_db() as db:
        cursor = db.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?",
            (transaction_id, g.user["id"]),
        )
    if cursor.rowcount:
        flash("Money record deleted.", "success")
    else:
        flash("That record could not be found.", "error")
    return redirect(url_for("records"))


@app.route("/records/<int:transaction_id>/edit", methods=["GET", "POST"])
def edit_cash_record(transaction_id):
    with connect_db() as db:
        transaction = db.execute(
            "SELECT * FROM cash_transactions WHERE id = ? AND user_id = ?",
            (transaction_id, g.user["id"]),
        ).fetchone()
    if transaction is None:
        flash("That cash transaction could not be found.", "error")
        return redirect(url_for("records"))

    if request.method == "POST":
        try:
            record = parse_cash_form(request.form, request.files)
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("edit_cash_record", transaction_id=transaction_id))

        photo_filename = transaction["photo_filename"]
        photo_data = transaction["photo_data"]
        old_photo = None
        if record["photo_data"]:
            photo_filename = new_cash_photo_filename(record["photo_extension"])
            photo_data = record["photo_data"]
            old_photo = transaction["photo_filename"]
        elif request.form.get("remove_photo") == "yes":
            photo_filename = None
            photo_data = None
            old_photo = transaction["photo_filename"]

        with connect_db() as db:
            db.execute(
                "UPDATE cash_transactions SET direction = ?, amount_paise = ?, location = ?, "
                "transaction_time = ?, description = ?, photo_filename = ?, photo_data = ? "
                "WHERE id = ? AND user_id = ?",
                (
                    record["direction"],
                    record["amount_paise"],
                    record["location"],
                    record["transaction_time"],
                    record["description"],
                    photo_filename,
                    photo_data,
                    transaction_id,
                    g.user["id"],
                ),
            )
        if old_photo and old_photo != photo_filename:
            remove_cash_photo(old_photo)
        flash("Your cash transaction was updated.", "success")
        return redirect(url_for("records"))

    return render_template(
        "edit_record.html",
        page="records",
        transaction=transaction,
        amount=f"{transaction['amount_paise'] / 100:.2f}",
        photo_url=(
            url_for("cash_photo", filename=transaction["photo_filename"])
            if transaction["photo_filename"]
            else None
        ),
    )


@app.post("/records/<int:transaction_id>/delete")
def delete_cash_record(transaction_id):
    with connect_db() as db:
        transaction = db.execute(
            "SELECT photo_filename, photo_data FROM cash_transactions "
            "WHERE id = ? AND user_id = ?",
            (transaction_id, g.user["id"]),
        ).fetchone()
        cursor = db.execute(
            "DELETE FROM cash_transactions WHERE id = ? AND user_id = ?",
            (transaction_id, g.user["id"]),
        )
    if cursor.rowcount:
        if transaction["photo_filename"] and transaction["photo_data"] is None:
            remove_cash_photo(transaction["photo_filename"])
        flash("Cash transaction deleted. Your balance has been recalculated.", "success")
    else:
        flash("That cash transaction could not be found.", "error")
    return redirect(url_for("records"))


@app.get("/records/photos/<filename>")
def cash_photo(filename):
    with connect_db() as db:
        photo = db.execute(
            "SELECT photo_filename, photo_data FROM cash_transactions "
            "WHERE photo_filename = ? AND user_id = ?",
            (filename, g.user["id"]),
        ).fetchone()
    if not photo or Path(filename).name != filename:
        return "", 404
    mimetype = {
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(Path(filename).suffix.lower())
    if not mimetype:
        return "", 404
    image_bytes = photo["photo_data"]
    if image_bytes is None:
        photo_path = CASH_PHOTO_DIR / filename
        if not photo_path.is_file():
            return "", 404
        image_bytes = photo_path.read_bytes()
    response = send_file(
        BytesIO(image_bytes),
        mimetype=mimetype,
        download_name=filename,
        conditional=False,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/alerts")
def alerts():
    spending_alerts = get_alerts()
    with connect_db() as db:
        locations = db.execute(
            "SELECT location, MAX(transaction_time) AS latest_at FROM cash_transactions "
            "WHERE user_id = ? GROUP BY location ORDER BY location COLLATE NOCASE",
            (g.user["id"],),
        ).fetchall()
        checks = db.execute(
            "SELECT * FROM cash_checks WHERE user_id = ? ORDER BY counted_at DESC, id DESC LIMIT 100",
            (g.user["id"],),
        ).fetchall()
        notifications = db.execute(
            "SELECT * FROM app_notifications WHERE user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 100",
            (g.user["id"],),
        ).fetchall()

    now = datetime.now()
    check_rows = []
    cash_alerts = []
    for check in checks:
        latest = datetime.strptime(check["ledger_latest_at"], "%Y-%m-%dT%H:%M")
        counted = datetime.strptime(check["counted_at"], "%Y-%m-%dT%H:%M")
        ledger_outdated = latest < counted - timedelta(days=30)
        later_entries_exist = any(
            location["location"] == check["location"]
            and location["latest_at"] > check["counted_at"]
            for location in locations
        )
        check_details = {
            **dict(check),
            "ledger_age_days": max((counted.date() - latest.date()).days, 0),
            "ledger_outdated": ledger_outdated,
            "later_entries_exist": later_entries_exist,
        }
        check_rows.append(
            {
                "record": check_details,
            }
        )
        if check["difference_paise"] and check["status"] is not None:
            cash_alerts.append(check_details)
    return render_template(
        "alerts.html",
        page="alerts",
        alerts=spending_alerts,
        cash_locations=locations,
        cash_checks=check_rows,
        cash_discrepancies=cash_alerts,
        count_now=now.strftime("%Y-%m-%dT%H:%M"),
        notifications=notifications,
        unread_notification_count=get_unread_notification_count(),
        notification_preferences=get_notification_preferences(),
    )


@app.post("/alerts/cash-checks")
def create_cash_check():
    try:
        location, actual_paise, counted_at, counted_time = parse_cash_count(request.form)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("alerts"))

    with connect_db() as db:
        ledger = db.execute(
            "SELECT COUNT(*) AS entry_count, "
            "COALESCE(SUM(CASE WHEN direction = 'added' THEN amount_paise ELSE -amount_paise END), 0) "
            "AS expected_paise, MAX(transaction_time) AS latest_at "
            "FROM cash_transactions WHERE user_id = ? AND location = ? AND transaction_time <= ?",
            (g.user["id"], location, counted_at),
        ).fetchone()
        location_exists = db.execute(
            "SELECT 1 FROM cash_transactions WHERE user_id = ? AND location = ? LIMIT 1",
            (g.user["id"], location),
        ).fetchone()
        if not location_exists:
            flash("This location has no cash records yet, so there is no expected balance to compare.", "error")
            return redirect(url_for("alerts"))
        if not ledger["entry_count"]:
            flash(
                "There are no cash records for this location on or before the count time. "
                "Add the missing earlier records or choose a later count time.",
                "error",
            )
            return redirect(url_for("alerts"))
        expected_paise = ledger["expected_paise"]
        difference_paise = actual_paise - expected_paise
        status = "New" if difference_paise else None
        check_cursor = db.execute(
            "INSERT INTO cash_checks "
            "(location, expected_paise, actual_paise, difference_paise, counted_at, "
            "ledger_latest_at, ledger_complete, status, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) RETURNING id",
            (
                location,
                expected_paise,
                actual_paise,
                difference_paise,
                counted_at,
                ledger["latest_at"],
                status,
                g.user["id"],
            ),
        )
        if status:
            check_id = check_cursor.fetchone()["id"]
            db.execute(
                "INSERT INTO cash_check_status_history (check_id, old_status, new_status, user_id) "
                "VALUES (?, ?, ?, ?)",
                (check_id, "New", "New", g.user["id"]),
            )
            preferences = db.execute(
                "SELECT discrepancy_notifications FROM user_notification_preferences WHERE user_id = ?",
                (g.user["id"],),
            ).fetchone()
            if preferences["discrepancy_notifications"]:
                create_notification(
                    db,
                    "discrepancy",
                    "Possible cash discrepancy",
                    f"A manual count at {location} differed from its recorded expected balance. "
                    "Review the amounts and cash records; this comparison does not establish a cause.",
                    check_id,
                )

    if difference_paise:
        flash(
            "A possible cash discrepancy was recorded for review. It is a comparison of your "
            "entered cash records and manual count, not evidence of theft or responsibility.",
            "success",
        )
    else:
        flash(
            "Your manually entered cash count matches the recorded expected balance. "
            "The check was saved; no discrepancy alert was created.",
            "success",
        )
    return redirect(url_for("alerts"))


@app.post("/alerts/cash-checks/<int:check_id>/status")
def update_cash_check_status(check_id):
    new_status = request.form.get("status", "")
    if new_status not in ("New", "Under Review", "Resolved"):
        flash("Choose a valid discrepancy status.", "error")
        return redirect(url_for("alerts"))
    with connect_db() as db:
        check = db.execute(
            "SELECT status, location FROM cash_checks "
            "WHERE id = ? AND user_id = ? AND difference_paise != 0",
            (check_id, g.user["id"]),
        ).fetchone()
        if not check:
            flash("That discrepancy alert could not be found.", "error")
            return redirect(url_for("alerts"))
        if check["status"] != new_status:
            db.execute(
                "UPDATE cash_checks SET status = ? WHERE id = ? AND user_id = ?",
                (new_status, check_id, g.user["id"]),
            )
            db.execute(
                "INSERT INTO cash_check_status_history "
                "(check_id, old_status, new_status, user_id) VALUES (?, ?, ?, ?)",
                (check_id, check["status"], new_status, g.user["id"]),
            )
            preferences = db.execute(
                "SELECT status_notifications FROM user_notification_preferences WHERE user_id = ?",
                (g.user["id"],),
            ).fetchone()
            if preferences["status_notifications"]:
                create_notification(
                    db,
                    "status",
                    "Discrepancy review status updated",
                    f"The review status for the possible cash discrepancy at {check['location']} "
                    f"was changed from {check['status']} to {new_status}.",
                    check_id,
                )
    flash("Discrepancy alert status updated.", "success")
    return redirect(url_for("alerts"))


@app.post("/notifications/<int:notification_id>/read")
def mark_notification_read(notification_id):
    with connect_db() as db:
        cursor = db.execute(
            "UPDATE app_notifications SET read_at = ? "
            "WHERE id = ? AND user_id = ? AND read_at IS NULL",
            (datetime.now().isoformat(timespec="seconds"), notification_id, g.user["id"]),
        )
    if not cursor.rowcount:
        flash("That unread notification could not be found.", "error")
    else:
        flash("Notification marked as read.", "success")
    return redirect(url_for("alerts"))


@app.post("/notifications/read-all")
def mark_all_notifications_read():
    with connect_db() as db:
        db.execute(
            "UPDATE app_notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
            (datetime.now().isoformat(timespec="seconds"), g.user["id"]),
        )
    flash("All notifications marked as read.", "success")
    return redirect(url_for("alerts"))


@app.post("/notifications/test")
def send_test_notification():
    with connect_db() as db:
        create_notification(
            db,
            "test",
            "Test notification",
            "Your in-app notifications are working. This is a test message, not a cash discrepancy.",
        )
    flash("Test notification added to your in-app Alerts inbox.", "success")
    return redirect(url_for("alerts"))


def get_cash_report_filters():
    today = date.today()
    default_start = today - timedelta(days=29)
    start_value = request.args.get("start_date", default_start.isoformat())
    end_value = request.args.get("end_date", today.isoformat())
    location = request.args.get("location", "").strip()
    try:
        start_date = date.fromisoformat(start_value)
        end_date = date.fromisoformat(end_value)
        if start_date.isoformat() != start_value or end_date.isoformat() != end_value:
            raise ValueError
        if start_date > end_date:
            raise ValueError
        if (end_date - start_date).days > 365:
            raise ValueError
        if len(location) > 80:
            raise ValueError
    except ValueError:
        raise ValueError(
            "Choose valid dates with the start on or before the end, within a one-year range."
        ) from None
    return start_date, end_date, location


def get_cash_report_data(start_date, end_date, location):
    filters = ["user_id = ?", "transaction_time >= ?", "transaction_time < ?"]
    parameters = [
        g.user["id"],
        f"{start_date.isoformat()}T00:00",
        f"{(end_date + timedelta(days=1)).isoformat()}T00:00",
    ]
    if location:
        filters.append("location = ?")
        parameters.append(location)
    where_clause = " AND ".join(filters)

    check_filters = [
        "user_id = ?",
        "counted_at >= ?",
        "counted_at < ?",
        "status IN ('New', 'Under Review')",
    ]
    check_parameters = [
        g.user["id"],
        f"{start_date.isoformat()}T00:00",
        f"{(end_date + timedelta(days=1)).isoformat()}T00:00",
    ]
    if location:
        check_filters.append("location = ?")
        check_parameters.append(location)
    check_where_clause = " AND ".join(check_filters)

    balance_filters = ["user_id = ?", "transaction_time < ?"]
    balance_parameters = [
        g.user["id"],
        (end_date + timedelta(days=1)).isoformat() + "T00:00",
    ]
    if location:
        balance_filters.append("location = ?")
        balance_parameters.append(location)
    balance_where_clause = " AND ".join(balance_filters)

    with connect_db() as db:
        totals = db.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS total_recorded_paise, "
            "COALESCE(SUM(CASE WHEN direction = 'added' THEN amount_paise ELSE 0 END), 0) "
            "AS total_added_paise, "
            "COALESCE(SUM(CASE WHEN direction = 'removed' THEN amount_paise ELSE 0 END), 0) "
            "AS total_removed_paise, "
            "COALESCE(SUM(CASE WHEN direction = 'added' THEN amount_paise ELSE -amount_paise END), 0) "
            "AS expected_balance_paise "
            f"FROM cash_transactions WHERE {where_clause}",
            parameters,
        ).fetchone()
        expected_balance_paise = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN direction = 'added' "
            "THEN amount_paise ELSE -amount_paise END), 0) "
            f"FROM cash_transactions WHERE {balance_where_clause}",
            balance_parameters,
        ).fetchone()[0]
        unresolved_count = db.execute(
            f"SELECT COUNT(*) FROM cash_checks WHERE {check_where_clause}",
            check_parameters,
        ).fetchone()[0]
        daily_rows = db.execute(
            "SELECT substr(transaction_time, 1, 10) AS day, direction, SUM(amount_paise) AS total "
            f"FROM cash_transactions WHERE {where_clause} "
            "GROUP BY day, direction ORDER BY day",
            parameters,
        ).fetchall()
        monthly_rows = db.execute(
            "SELECT substr(transaction_time, 1, 7) AS month, direction, SUM(amount_paise) AS total "
            f"FROM cash_transactions WHERE {where_clause} "
            "GROUP BY month, direction ORDER BY month",
            parameters,
        ).fetchall()
        locations = db.execute(
            "SELECT DISTINCT location FROM cash_transactions "
            "WHERE user_id = ? ORDER BY location COLLATE NOCASE",
            (g.user["id"],),
        ).fetchall()
        transactions = db.execute(
            "SELECT direction, amount_paise, location, transaction_time, description "
            f"FROM cash_transactions WHERE {where_clause} "
            "ORDER BY transaction_time DESC, id DESC",
            parameters,
        ).fetchall()

    day_count = (end_date - start_date).days + 1
    daily = {
        (start_date + timedelta(days=offset)).isoformat(): {"added": 0, "removed": 0}
        for offset in range(day_count)
    }
    for row in daily_rows:
        daily[row["day"]][row["direction"]] = row["total"]
    monthly = {}
    month_cursor = start_date.replace(day=1)
    while month_cursor <= end_date:
        monthly[month_cursor.strftime("%Y-%m")] = {"added": 0, "removed": 0}
        if month_cursor.month == 12:
            month_cursor = month_cursor.replace(year=month_cursor.year + 1, month=1)
        else:
            month_cursor = month_cursor.replace(month=month_cursor.month + 1)
    for row in monthly_rows:
        monthly.setdefault(row["month"], {"added": 0, "removed": 0})
        monthly[row["month"]][row["direction"]] = row["total"]

    return {
        "totals": {
            **dict(totals),
            "expected_balance_paise": expected_balance_paise,
        },
        "unresolved_count": unresolved_count,
        "daily_labels": list(daily.keys()),
        "daily_added": [row["added"] / 100 for row in daily.values()],
        "daily_removed": [row["removed"] / 100 for row in daily.values()],
        "monthly_labels": list(monthly.keys()),
        "monthly_added": [row["added"] / 100 for row in monthly.values()],
        "monthly_removed": [row["removed"] / 100 for row in monthly.values()],
        "locations": [row["location"] for row in locations],
        "transactions": transactions,
    }


def csv_text_cell(value):
    text = str(value or "")
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


@app.route("/reports")
def reports():
    try:
        start_date, end_date, cash_location = get_cash_report_filters()
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("reports"))
    cash_report = get_cash_report_data(start_date, end_date, cash_location)
    month_keys = []
    today = date.today()
    for offset in range(5, -1, -1):
        month_number = today.month - offset
        year = today.year
        while month_number <= 0:
            month_number += 12
            year -= 1
        month_keys.append(f"{year:04d}-{month_number:02d}")
    with connect_db() as db:
        rows = db.execute(
            "SELECT substr(transaction_date, 1, 7) AS month, kind, SUM(amount) AS total "
            "FROM transactions WHERE user_id = ? AND substr(transaction_date, 1, 7) >= ? "
            "GROUP BY month, kind",
            (g.user["id"], month_keys[0]),
        ).fetchall()
        category_rows = db.execute(
            "SELECT category, SUM(amount) AS total FROM transactions "
            "WHERE user_id = ? AND kind = 'expense' AND substr(transaction_date, 1, 7) = ? "
            "GROUP BY category ORDER BY total DESC",
            (g.user["id"], today.strftime("%Y-%m")),
        ).fetchall()
    monthly = {month: {"income": 0, "expense": 0} for month in month_keys}
    for row in rows:
        if row["month"] in monthly:
            monthly[row["month"]][row["kind"]] = row["total"]
    max_spend = max([month["expense"] for month in monthly.values()] + [1])
    return render_template(
        "reports.html",
        page="reports",
        monthly=monthly,
        month_labels=[datetime.strptime(key, "%Y-%m").strftime("%b") for key in month_keys],
        max_spend=max_spend,
        category_rows=category_rows,
        cash_report=cash_report,
        cash_start_date=start_date.isoformat(),
        cash_end_date=end_date.isoformat(),
        cash_location=cash_location,
    )


@app.get("/reports/cash.csv")
def export_cash_report_csv():
    try:
        start_date, end_date, location = get_cash_report_filters()
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("reports"))
    report = get_cash_report_data(start_date, end_date, location)
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        ["Type", "Amount (INR)", "Storage location", "Date and time", "Description"]
    )
    for transaction in report["transactions"]:
        writer.writerow(
            [
                "Money added" if transaction["direction"] == "added" else "Money removed",
                f"{transaction['amount_paise'] / 100:.2f}",
                csv_text_cell(transaction["location"]),
                transaction["transaction_time"],
                csv_text_cell(transaction["description"]),
            ]
        )
    csv_data = ("\ufeff" + output.getvalue()).encode("utf-8")
    return app.response_class(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=smart-money-cash-history.csv"},
    )


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        try:
            budget = float(request.form.get("monthly_budget", ""))
            threshold = float(request.form.get("alert_threshold", ""))
            currency_code = request.form.get("currency", "")
            if (
                not math.isfinite(budget)
                or not math.isfinite(threshold)
                or budget <= 0
                or threshold <= 0
                or budget > 999999999999.99
                or threshold > 999999999999.99
                or currency_code not in ("USD", "EUR", "GBP", "INR")
            ):
                raise ValueError
        except ValueError:
            flash("Choose a currency and enter amounts greater than zero.", "error")
            return redirect(url_for("settings_page"))
        with connect_db() as db:
            db.execute(
                "UPDATE user_settings SET monthly_budget = ?, alert_threshold = ?, currency = ? "
                "WHERE user_id = ?",
                (budget, threshold, currency_code, g.user["id"]),
            )
        flash("Your settings have been saved.", "success")
        return redirect(url_for("settings_page"))
    return render_template(
        "settings.html",
        page="settings",
        notification_preferences=get_notification_preferences(),
    )


@app.post("/settings/notifications")
def save_notification_preferences():
    if request.form.get("discrepancy_notifications") not in (None, "on") or request.form.get(
        "status_notifications"
    ) not in (None, "on"):
        flash("The notification preference values are invalid.", "error")
        return redirect(url_for("settings_page"))
    discrepancy_notifications = int(
        request.form.get("discrepancy_notifications") == "on"
    )
    status_notifications = int(request.form.get("status_notifications") == "on")
    with connect_db() as db:
        db.execute(
            "UPDATE user_notification_preferences SET discrepancy_notifications = ?, "
            "status_notifications = ? WHERE user_id = ?",
            (discrepancy_notifications, status_notifications, g.user["id"]),
        )
    flash("Your in-app notification preferences have been saved.", "success")
    return redirect(url_for("settings_page"))


initialize_database()


if __name__ == "__main__":
    app.run(
        debug=os.environ.get("SMART_MONEY_DEBUG", "").lower() in ("1", "true", "yes")
    )
