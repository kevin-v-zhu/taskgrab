# =============================
# app.py — Minimal Public Taskboard (Flask + SQLite + HTMX)
# =============================
# Run locally:
#   pip install flask==3.0.0 flask_sqlalchemy==3.1.1 sqlalchemy==2.0.23 python-dateutil==2.9.0.post0
#   python app.py
#   open http://127.0.0.1:5000
#
# Deploy to Render:
#   - Add render.yaml (see bottom of this file)
#   - Build Command: pip install -r requirements.txt
#   - Start Command: gunicorn app:app
#
# Files also needed for Render:
#   requirements.txt (see bottom)
#   render.yaml (see bottom)

from __future__ import annotations
from flask import Flask, request, redirect, url_for, render_template_string, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from datetime import datetime, date, timedelta
from dateutil import tz
import json
import os

app = Flask(__name__)

# Database configuration - supports both SQLite (local) and PostgreSQL (production/Supabase)
database_url = os.environ.get('DATABASE_URL', 'sqlite:///taskboard.db')
# Fix for PostgreSQL URL compatibility - use psycopg3 driver (works with Python 3.13)
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql+psycopg://', 1)
elif database_url.startswith('postgresql://') and '+' not in database_url.split('://')[0]:
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Email Configuration
app.config['MAIL_ENABLED'] = os.environ.get('MAIL_ENABLED', 'false').lower() == 'true'
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', '')

db = SQLAlchemy(app)

# -----------------------------
# Models
# -----------------------------
class Person(db.Model):
    __tablename__ = 'people'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False, unique=True)
    email = db.Column(db.String, nullable=True)
    # weekly_hours JSON mapping weekday index 0..6 -> float hours available that day
    weekly_hours = db.Column(db.Text, nullable=False, default='{}')
    # time_slots JSON mapping weekday index 0..6 -> list of {start: "HH:MM", end: "HH:MM"} slots
    time_slots = db.Column(db.Text, nullable=False, default='{}')

class Task(db.Model):
    __tablename__ = 'tasks'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String, nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    description = db.Column(db.Text, nullable=True)
    assignee_id = db.Column(db.Integer, db.ForeignKey('people.id'), nullable=True)
    estimated_hours = db.Column(db.Float, nullable=True, default=0.0)
    status = db.Column(db.String, nullable=False, default='assigned')  # assigned | in_progress | ready_for_review | complete
    up_for_grabs = db.Column(db.Boolean, nullable=False, default=True)
    # convenience cache of JSON scheduled blocks for quick display (kept in sync with ScheduledBlock rows)
    scheduled_json = db.Column(db.Text, nullable=False, default='[]')
    # New fields for enhanced task management
    assigner = db.Column(db.String, nullable=True)  # Name of person who assigned the task
    priority = db.Column(db.Integer, nullable=False, default=3)  # 1=Highest, 2=High, 3=Medium, 4=Low, 5=Lowest
    scheduling_flag = db.Column(db.String, nullable=True)  # 'red' (can't complete by due date) or 'orange' (spillover)
    scheduling_message = db.Column(db.String, nullable=True)  # Warning/info message for scheduling issues

    assignee = db.relationship('Person', backref='tasks')

class ScheduledBlock(db.Model):
    __tablename__ = 'scheduled_blocks'
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=False)
    person_id = db.Column(db.Integer, db.ForeignKey('people.id'), nullable=False)
    day = db.Column(db.Date, nullable=False)
    hours = db.Column(db.Float, nullable=False)

    task = db.relationship('Task', backref='blocks')
    person = db.relationship('Person', backref='blocks')

# -----------------------------
# DB init / seed
# -----------------------------
with app.app_context():
    # Auto-migrate: Add email column if it doesn't exist
    from sqlalchemy import inspect
    inspector = inspect(db.engine)

    # Check if people table exists and migrate
    if 'people' in inspector.get_table_names():
        columns = [col['name'] for col in inspector.get_columns('people')]

        if 'email' not in columns:
            app.logger.info("Running migration: adding email column to people table")
            with db.engine.connect() as conn:
                conn.execute(db.text("ALTER TABLE people ADD COLUMN email VARCHAR"))
                conn.commit()
            app.logger.info("Migration complete: email column added")

        if 'time_slots' not in columns:
            app.logger.info("Running migration: adding time_slots column to people table")
            with db.engine.connect() as conn:
                conn.execute(db.text("ALTER TABLE people ADD COLUMN time_slots TEXT DEFAULT '{}'"))
                conn.commit()
            app.logger.info("Migration complete: time_slots column added")

    # Check if tasks table exists and migrate
    if 'tasks' in inspector.get_table_names():
        task_columns = [col['name'] for col in inspector.get_columns('tasks')]

        if 'assigner' not in task_columns:
            app.logger.info("Running migration: adding assigner column to tasks table")
            with db.engine.connect() as conn:
                conn.execute(db.text("ALTER TABLE tasks ADD COLUMN assigner VARCHAR"))
                conn.commit()
            app.logger.info("Migration complete: assigner column added")

        if 'priority' not in task_columns:
            app.logger.info("Running migration: adding priority column to tasks table")
            with db.engine.connect() as conn:
                conn.execute(db.text("ALTER TABLE tasks ADD COLUMN priority INTEGER DEFAULT 3"))
                conn.commit()
            app.logger.info("Migration complete: priority column added")

        if 'scheduling_flag' not in task_columns:
            app.logger.info("Running migration: adding scheduling_flag column to tasks table")
            with db.engine.connect() as conn:
                conn.execute(db.text("ALTER TABLE tasks ADD COLUMN scheduling_flag VARCHAR"))
                conn.commit()
            app.logger.info("Migration complete: scheduling_flag column added")

        if 'scheduling_message' not in task_columns:
            app.logger.info("Running migration: adding scheduling_message column to tasks table")
            with db.engine.connect() as conn:
                conn.execute(db.text("ALTER TABLE tasks ADD COLUMN scheduling_message VARCHAR"))
                conn.commit()
            app.logger.info("Migration complete: scheduling_message column added")

    db.create_all()
    if Person.query.count() == 0:
        # Define team members with their schedules and emails

        # Day 0 = Monday

        # Real schedule data with specific time slots
        # Day indices: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Saturday, 6=Sunday
        team_members = [
            {
                "name": "Vidya",
                "email": "vidya22@sas.upenn.edu",
                "schedule": {"0": 0, "1": 3, "2": 0, "3": 0, "4": 3, "5": 0, "6": 0},
                "time_slots": {
                    "1": [{"start": "12:00", "end": "15:00"}],  # Tuesday 12-3pm
                    "4": [{"start": "11:00", "end": "14:00"}]   # Friday 11am-2pm
                }
            },
            {
                "name": "Ariel",
                "email": "fariel@sas.upenn.edu",
                "schedule": {"0": 4, "1": 0, "2": 0, "3": 0, "4": 3, "5": 0, "6": 0},
                "time_slots": {
                    "0": [{"start": "13:00", "end": "17:00"}],  # Monday 1-5pm
                    "4": [{"start": "14:00", "end": "17:00"}]   # Friday 2-5pm
                }
            },
            {
                "name": "Ray",
                "email": "ruitian@wharton.upenn.edu",
                "schedule": {"0": 0, "1": 0, "2": 1.75, "3": 0, "4": 4, "5": 0, "6": 0},
                "time_slots": {
                    "2": [{"start": "15:15", "end": "17:00"}],  # Wednesday 3:15-5pm
                    "4": [{"start": "10:00", "end": "14:00"}]   # Friday 10am-2pm
                }
            },
            {
                "name": "Viveka",
                "email": "vsinha@wharton.upenn.edu",
                "schedule": {"0": 2, "1": 0, "2": 2, "3": 2, "4": 4, "5": 0, "6": 0},
                "time_slots": {
                    "0": [{"start": "13:30", "end": "15:30"}],  # Monday 1:30-3:30pm
                    "2": [{"start": "13:30", "end": "15:30"}],  # Wednesday 1:30-3:30pm
                    "3": [{"start": "11:45", "end": "13:45"}],  # Thursday 11:45am-1:45pm
                    "4": [{"start": "13:00", "end": "17:00"}]   # Friday 1-5pm
                }
            },
            {
                "name": "Melinda",
                "email": "melimei@wharton.upenn.edu",
                "schedule": {"0": 0, "1": 3, "2": 0, "3": 5, "4": 2, "5": 0, "6": 0},
                "time_slots": {
                    "1": [{"start": "12:00", "end": "15:00"}],  # Tuesday 12-3pm
                    "3": [{"start": "10:00", "end": "15:00"}],  # Thursday 10am-3pm
                    "4": [{"start": "15:00", "end": "17:00"}]   # Friday 3-5pm
                }
            },
            {
                "name": "Test Lila",
                "email": "ldimasi@wharton.upenn.edu",
                "schedule": {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 0, "6": 0},
                "time_slots": {
                    "0": [{"start": "16:00", "end": "17:00"}],  # Monday 4-5pm
                    "1": [{"start": "16:00", "end": "17:00"}],  # Tuesday 4-5pm
                    "2": [{"start": "16:00", "end": "17:00"}],  # Wednesday 4-5pm
                    "3": [{"start": "16:00", "end": "17:00"}],  # Thursday 4-5pm
                    "4": [{"start": "16:00", "end": "17:00"}]   # Friday 4-5pm
                }
            },
            {
                "name": "Test Kevin",
                "email": "kvzhu@wharton.upenn.edu",
                "schedule": {"0": 1, "1": 1, "2": 1, "3": 1, "4": 1, "5": 0, "6": 0},
                "time_slots": {
                    "0": [{"start": "16:00", "end": "17:00"}],  # Monday 4-5pm
                    "1": [{"start": "16:00", "end": "17:00"}],  # Tuesday 4-5pm
                    "2": [{"start": "16:00", "end": "17:00"}],  # Wednesday 4-5pm
                    "3": [{"start": "16:00", "end": "17:00"}],  # Thursday 4-5pm
                    "4": [{"start": "16:00", "end": "17:00"}]   # Friday 4-5pm
                }
            }
        ]

        for member in team_members:
            db.session.add(Person(
                name=member["name"],
                email=member["email"],
                weekly_hours=json.dumps(member["schedule"]),
                time_slots=json.dumps(member.get("time_slots", {}))
            ))
        db.session.commit()

# -----------------------------
# Utilities
# -----------------------------
WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
WEEKDAY_FULL_LABELS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

# EST Timezone support
EST = tz.gettz('America/New_York')

def get_est_now() -> datetime:
    """Get current datetime in EST timezone."""
    return datetime.now(EST)

def get_est_today() -> date:
    """Get current date in EST timezone."""
    return get_est_now().date()

def get_week_dates(start_date: date = None) -> list[date]:
    """Get list of dates for a week starting from start_date (or current week if None).
    Returns Monday through Sunday of that week.
    """
    if start_date is None:
        start_date = get_est_today()
    # Find the Monday of this week
    monday = start_date - timedelta(days=start_date.weekday())
    return [monday + timedelta(days=i) for i in range(7)]

def get_two_weeks_dates(start_date: date = None) -> list[date]:
    """Get list of dates for current week and next week (14 days starting from Monday)."""
    if start_date is None:
        start_date = get_est_today()
    # Find the Monday of this week
    monday = start_date - timedelta(days=start_date.weekday())
    return [monday + timedelta(days=i) for i in range(14)]

def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None

def format_time_12h(time_str: str) -> str:
    """Convert 24-hour time string (HH:MM) to 12-hour AM/PM format."""
    try:
        t = datetime.strptime(time_str, '%H:%M')
        return t.strftime('%I:%M %p').lstrip('0').replace(' 0', ' ')
    except ValueError:
        return time_str  # Return as-is if parsing fails

def format_time_range_12h(start: str, end: str) -> str:
    """Format a time range in 12-hour AM/PM format."""
    return f"{format_time_12h(start)} - {format_time_12h(end)}"


# -----------------------------
# Email Service
# -----------------------------
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_email(to_addresses, subject, body_plain, body_html=None):
    """
    Send email with error handling. Returns True if successful, False otherwise.
    """
    if not app.config['MAIL_ENABLED']:
        app.logger.info(f"Email disabled. Would send to {to_addresses}: {subject}")
        return False

    if not app.config['MAIL_USERNAME'] or not app.config['MAIL_PASSWORD']:
        app.logger.warning("Email credentials not configured")
        return False

    # Normalize to list
    if isinstance(to_addresses, str):
        to_addresses = [to_addresses]

    # Filter out None and empty emails
    to_addresses = [email for email in to_addresses if email and email.strip()]

    if not to_addresses:
        app.logger.info("No valid email addresses to send to")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = app.config['MAIL_DEFAULT_SENDER']
        msg['To'] = ', '.join(to_addresses)
        msg['Subject'] = subject

        # Attach plain text
        msg.attach(MIMEText(body_plain, 'plain'))

        # Attach HTML if provided
        if body_html:
            msg.attach(MIMEText(body_html, 'html'))

        # Connect and send
        server = smtplib.SMTP(app.config['MAIL_SERVER'], app.config['MAIL_PORT'])
        server.starttls()
        server.login(app.config['MAIL_USERNAME'], app.config['MAIL_PASSWORD'])
        server.sendmail(app.config['MAIL_DEFAULT_SENDER'], to_addresses, msg.as_string())
        server.quit()

        app.logger.info(f"Email sent successfully to {to_addresses}: {subject}")
        return True

    except Exception as e:
        app.logger.error(f"Failed to send email to {to_addresses}: {str(e)}")
        return False


def send_task_assigned_email(task, person):
    """Send email when a task is assigned to a person."""
    if not person.email:
        return False

    subject = f"Task Assigned: {task.title}"

    due_text = f"Due: {task.due_date.strftime('%B %d, %Y')}" if task.due_date else "No due date"
    est_text = f"{task.estimated_hours:.2f} hours" if task.estimated_hours else "No estimate"

    body_plain = f"""Hi {person.name},

You have been assigned a new task:

Task: {task.title}
{due_text}
Estimated: {est_text}

{task.description or 'No description provided.'}

View full details at your taskboard.

---
Taskgrab Notification System
"""

    body_html = f"""
<html>
<body style="font-family: system-ui, -apple-system, sans-serif; line-height: 1.6; color: #333;">
    <h2 style="color: #2563eb;">Task Assigned to You</h2>
    <div style="background: #f9fafb; padding: 1rem; border-radius: 0.5rem; border-left: 4px solid #2563eb;">
        <h3 style="margin-top: 0;">{task.title}</h3>
        <p><strong>{due_text}</strong> · Estimated: {est_text}</p>
        {f'<p>{task.description}</p>' if task.description else ''}
    </div>
    <hr style="border: none; border-top: 1px solid #e5e5e5; margin: 2rem 0;">
    <p style="color: #666; font-size: 0.875rem;">Taskgrab Notification System</p>
</body>
</html>
"""

    return send_email(person.email, subject, body_plain, body_html)


def send_task_reassigned_email(task, old_person, new_person):
    """Send emails when task is reassigned from one person to another."""
    results = []

    # Notify old assignee (task removed)
    if old_person and old_person.email:
        subject = f"Task Unassigned: {task.title}"
        body_plain = f"""Hi {old_person.name},

The task "{task.title}" has been reassigned to {new_person.name if new_person else 'someone else'}.

---
Taskgrab Notification System
"""
        body_html = f"""
<html>
<body style="font-family: system-ui, -apple-system, sans-serif; line-height: 1.6; color: #333;">
    <h2 style="color: #666;">Task Reassigned</h2>
    <p>The task <strong>{task.title}</strong> has been reassigned to {new_person.name if new_person else 'someone else'}.</p>
    <p style="color: #666; font-size: 0.875rem;">Taskgrab Notification System</p>
</body>
</html>
"""
        results.append(send_email(old_person.email, subject, body_plain, body_html))

    # Notify new assignee (task assigned)
    if new_person:
        results.append(send_task_assigned_email(task, new_person))

    return any(results)


def send_task_up_for_grabs_email(task):
    """Send email to everyone when task is marked 'up for grabs'."""
    all_people = Person.query.all()
    email_addresses = [p.email for p in all_people if p.email]

    if not email_addresses:
        return False

    subject = f"Task Up For Grabs: {task.title}"

    due_text = f"Due: {task.due_date.strftime('%B %d, %Y')}" if task.due_date else "No due date"
    est_text = f"{task.estimated_hours:.2f} hours" if task.estimated_hours else "No estimate"

    body_plain = f"""Team,

A task is now up for grabs:

Task: {task.title}
{due_text}
Estimated: {est_text}

{task.description or 'No description provided.'}

View and claim it on the taskboard.

---
Taskgrab Notification System
"""

    body_html = f"""
<html>
<body style="font-family: system-ui, -apple-system, sans-serif; line-height: 1.6; color: #333;">
    <h2 style="color: #f59e0b;">Task Up For Grabs</h2>
    <div style="background: #fff7cc; padding: 1rem; border-radius: 0.5rem; border-left: 4px solid #f59e0b;">
        <h3 style="margin-top: 0;">{task.title}</h3>
        <p><strong>{due_text}</strong> · Estimated: {est_text}</p>
        {f'<p>{task.description}</p>' if task.description else ''}
    </div>
    <hr style="border: none; border-top: 1px solid #e5e5e5; margin: 2rem 0;">
    <p style="color: #666; font-size: 0.875rem;">Taskgrab Notification System</p>
</body>
</html>
"""

    return send_email(email_addresses, subject, body_plain, body_html)


def send_task_completed_email(task):
    """Send email to assignee when task is marked complete."""
    if not task.assignee or not task.assignee.email:
        return False

    subject = f"Task Completed: {task.title}"

    body_plain = f"""Hi {task.assignee.name},

Congratulations! The task "{task.title}" has been marked as completed.

---
Taskgrab Notification System
"""

    body_html = f"""
<html>
<body style="font-family: system-ui, -apple-system, sans-serif; line-height: 1.6; color: #333;">
    <h2 style="color: #10b981;">Task Completed!</h2>
    <p>Congratulations! The task <strong>{task.title}</strong> has been marked as completed.</p>
    <p style="color: #666; font-size: 0.875rem;">Taskgrab Notification System</p>
</body>
</html>
"""

    return send_email(task.assignee.email, subject, body_plain, body_html)


def time_str_to_minutes(time_str: str) -> int:
    """Convert HH:MM time string to minutes since midnight."""
    try:
        parts = time_str.split(':')
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0

def get_available_hours_from_slots(time_slots: list, current_time_minutes: int = None) -> float:
    """Calculate total available hours from time slots, optionally filtering by current time.

    Args:
        time_slots: List of {"start": "HH:MM", "end": "HH:MM"} dicts
        current_time_minutes: If provided, only count time remaining after this time (minutes since midnight)

    Returns:
        Total available hours
    """
    total_minutes = 0
    for slot in time_slots:
        start_mins = time_str_to_minutes(slot.get('start', '00:00'))
        end_mins = time_str_to_minutes(slot.get('end', '00:00'))

        if current_time_minutes is not None:
            # Only count time after current time
            if end_mins <= current_time_minutes:
                # Slot has already passed
                continue
            if start_mins < current_time_minutes:
                # Slot started but hasn't ended - count remaining time
                start_mins = current_time_minutes

        slot_minutes = max(0, end_mins - start_mins)
        total_minutes += slot_minutes

    return total_minutes / 60.0

def person_remaining_capacity_on_day(person_id: int, day: date, exclude_task_id: int = None) -> float:
    """Get remaining capacity for a person on a specific day, optionally excluding a task's blocks.

    Takes into account the current time if the day is today - only counts hours that haven't passed yet.
    """
    person = Person.query.get(person_id)
    if not person:
        return 0.0

    day_idx = day.weekday()  # Monday=0

    # Get time slots for this day of the week
    time_slots_data = json.loads(person.time_slots or '{}')
    day_slots = time_slots_data.get(str(day_idx), [])

    if not day_slots:
        return 0.0

    # Check if this is today - if so, only count remaining hours
    est_now = get_est_now()
    est_today = est_now.date()

    if day == est_today:
        # Calculate current time in minutes since midnight
        current_time_minutes = est_now.hour * 60 + est_now.minute
        day_hours = get_available_hours_from_slots(day_slots, current_time_minutes)
    else:
        day_hours = get_available_hours_from_slots(day_slots)

    if day_hours <= 0:
        return 0.0

    # Subtract already scheduled blocks for this person on that day
    query = db.session.query(func.coalesce(func.sum(ScheduledBlock.hours), 0.0)).\
        filter(ScheduledBlock.person_id==person_id, ScheduledBlock.day==day)
    if exclude_task_id:
        query = query.filter(ScheduledBlock.task_id != exclude_task_id)
    used = query.scalar() or 0.0

    return max(0.0, day_hours - float(used))


def person_total_capacity_before_date(person_id: int, before_date: date, start_date: date = None) -> float:
    """Calculate total available capacity for a person from start_date until before_date (exclusive).

    Uses time-slot-aware capacity calculation that accounts for current time if start_date is today.
    """
    if start_date is None:
        start_date = get_est_today()
    if before_date <= start_date:
        return 0.0

    total = 0.0
    day_ptr = start_date
    while day_ptr < before_date:
        # Use the time-aware capacity function
        total += person_remaining_capacity_on_day(person_id, day_ptr)
        day_ptr += timedelta(days=1)
    return total


# Minimum chunk size for scheduling (in hours)
# Tasks won't be split into chunks smaller than this, except for the final remaining portion
MIN_SCHEDULE_CHUNK_HOURS = 1.0

def reschedule_all_tasks_for_person(person_id: int):
    """Reschedule all active tasks for a person based on priority and due date.
    This ensures higher priority tasks get scheduled first.
    """
    # Get all active tasks for this person, ordered by priority then due date
    tasks = Task.query.filter(
        Task.assignee_id == person_id,
        Task.status.notin_(['complete', 'completed', 'archived'])
    ).order_by(
        Task.priority.asc().nullslast(),
        Task.due_date.asc().nullslast()
    ).all()

    # Clear all existing blocks for this person's tasks
    for task in tasks:
        ScheduledBlock.query.filter_by(task_id=task.id).delete()
        task.scheduling_flag = None
        task.scheduling_message = None

    today = get_est_today()

    # Schedule each task in priority order
    for task in tasks:
        if not task.estimated_hours or task.estimated_hours <= 0:
            task.scheduled_json = '[]'
            continue

        remaining = float(task.estimated_hours)
        day_ptr = today
        blocks = []
        safety = 365
        passed_due_date = False
        hours_scheduled_before_due = 0.0

        while remaining > 0 and safety > 0:
            # Check capacity for this day (excluding current task since we cleared its blocks)
            capacity = person_remaining_capacity_on_day(person_id, day_ptr, exclude_task_id=task.id)

            # Enforce minimum chunk size:
            # - If capacity >= 1 hour, schedule (up to remaining hours)
            # - If capacity < 1 hour but remaining < 1 hour, schedule (final portion of task)
            # - If capacity < 1 hour and remaining >= 1 hour, skip this day (don't create tiny chunks)
            if capacity >= MIN_SCHEDULE_CHUNK_HOURS or (capacity > 0 and remaining < MIN_SCHEDULE_CHUNK_HOURS):
                chunk = min(capacity, remaining)
                blk = ScheduledBlock(task_id=task.id, person_id=person_id, day=day_ptr, hours=chunk)
                db.session.add(blk)
                blocks.append({"date": day_ptr.isoformat(), "hours": round(chunk, 2)})

                # Track hours scheduled before due date
                if task.due_date and day_ptr <= task.due_date:
                    hours_scheduled_before_due += chunk

                remaining -= chunk

            # Check if we've passed the due date
            if task.due_date and day_ptr > task.due_date and not passed_due_date:
                passed_due_date = True

            day_ptr += timedelta(days=1)
            safety -= 1

        task.scheduled_json = json.dumps(blocks)

        # Set flags based on scheduling result
        if task.due_date:
            if remaining > 0:
                # Couldn't complete task at all - RED flag
                task.scheduling_flag = 'red'
                task.scheduling_message = f"Cannot complete by due date. {remaining:.1f}h remaining. Please re-assign."
            elif passed_due_date:
                # Task spills over past due date - ORANGE flag
                spillover_hours = task.estimated_hours - hours_scheduled_before_due
                task.scheduling_flag = 'orange'
                task.scheduling_message = f"Re-assign remaining {spillover_hours:.1f} hours if not completed by due date."
            else:
                # Task can be completed on time
                task.scheduling_flag = None
                task.scheduling_message = None

    db.session.commit()


def auto_schedule_task(task: Task):
    """Fill ScheduledBlock rows for task based on assignee weekly_hours and estimated_hours.
    Triggers a full reschedule for the assignee to respect priority ordering.
    """
    if not task.assignee_id:
        ScheduledBlock.query.filter_by(task_id=task.id).delete()
        task.scheduled_json = '[]'
        task.scheduling_flag = None
        task.scheduling_message = None
        db.session.commit()
        return

    # Reschedule all tasks for this person to respect priority order
    reschedule_all_tasks_for_person(task.assignee_id)


def find_best_assignee(estimated_hours: float, due_date: date = None) -> int | None:
    """Find the best available RA to assign a task to.
    Returns the person_id of the RA with the most available capacity who can complete the task.
    If no one can complete the task entirely, returns the person who can do the most.
    """
    if not estimated_hours or estimated_hours <= 0:
        return None

    people = Person.query.all()
    if not people:
        return None

    today = get_est_today()
    best_person_id = None
    best_capacity = 0.0
    best_can_complete = False

    for person in people:
        # Calculate capacity before due date (or total capacity if no due date)
        if due_date:
            capacity = person_total_capacity_before_date(person.id, due_date, today)
            can_complete = capacity >= estimated_hours
        else:
            # No due date - calculate capacity for next 30 days
            future_date = today + timedelta(days=30)
            capacity = person_total_capacity_before_date(person.id, future_date, today)
            can_complete = capacity >= estimated_hours

        # Prefer someone who can complete the task
        if can_complete and not best_can_complete:
            best_person_id = person.id
            best_capacity = capacity
            best_can_complete = True
        elif can_complete == best_can_complete:
            # If both can or both can't complete, prefer the one with more capacity
            if capacity > best_capacity:
                best_person_id = person.id
                best_capacity = capacity

    return best_person_id


def task_css(task: Task) -> str:
    classes = ["task-card"]
    # up for grabs
    if task.up_for_grabs:
        classes.append("up-for-grabs")
    # overdue
    if task.due_date and task.status not in ('complete', 'completed') and get_est_today() > task.due_date:
        classes.append("overdue")
    # status class
    status = task.status or 'assigned'
    classes.append(f"status-{status.replace('_', '-')}")
    # scheduling flag class
    if task.scheduling_flag:
        classes.append(f"flag-{task.scheduling_flag}")
    return ' '.join(classes)

# -----------------------------
# Routes: UI pages
# -----------------------------
@app.route('/')
def index():
    tasks = Task.query.filter(Task.status != 'archived').order_by(Task.id.desc()).all()
    people = Person.query.order_by(Person.name).all()

    # Get current EST date/time and two weeks of dates
    est_now = get_est_now()
    est_today = est_now.date()
    est_datetime = est_now.strftime('%a, %b %d, %Y %I:%M %p')
    two_weeks = get_two_weeks_dates(est_today)

    # Prepare JSON for JavaScript
    people_json = json.dumps([{
        'id': p.id,
        'name': p.name,
        'time_slots': json.loads(p.time_slots or '{}')
    } for p in people])

    tasks_json = json.dumps([{
        'id': t.id,
        'title': t.title,
        'assignee_id': t.assignee_id,
        'status': t.status,
        'scheduled_json': t.scheduled_json,
        'scheduling_flag': t.scheduling_flag
    } for t in tasks])

    two_weeks_dates_json = json.dumps([d.isoformat() for d in two_weeks])

    return render_template_string(INDEX_HTML, tasks=tasks, people=people, task_css=task_css,
                                  WEEKDAY_LABELS=WEEKDAY_LABELS, json=json, today=est_today,
                                  people_json=people_json, tasks_json=tasks_json,
                                  two_weeks_dates_json=two_weeks_dates_json,
                                  est_today=est_today.isoformat(),
                                  est_datetime=est_datetime,
                                  format_time_12h=format_time_12h)

@app.route('/archive')
def archive():
    # Show tasks with status 'complete' (new) or 'completed' (legacy) for backward compatibility
    tasks = Task.query.filter(Task.status.in_(['complete', 'completed'])).order_by(Task.id.desc()).all()
    return render_template_string(ARCHIVE_HTML, tasks=tasks, task_css=task_css)

@app.route('/task/<int:task_id>')
def task_detail(task_id: int):
    task = Task.query.get_or_404(task_id)
    people = Person.query.order_by(Person.name).all()
    blocks = ScheduledBlock.query.filter_by(task_id=task.id).order_by(ScheduledBlock.day.asc()).all()
    return render_template_string(TASK_HTML, task=task, people=people, blocks=blocks, WEEKDAY_LABELS=WEEKDAY_LABELS, json=json)

# -----------------------------
# Routes: APIs (HTMX-friendly)
# -----------------------------
@app.post('/api/tasks')
def api_create_task():
    data = request.form or request.json or {}
    title = (data.get('title') or '').strip()
    if not title:
        abort(400, description='Title required')
    due_date = parse_date(data.get('due_date'))
    description = data.get('description') or ''
    estimated_hours = float(data.get('estimated_hours') or 0)
    priority = int(data.get('priority') or 3)
    assigner = (data.get('assigner') or '').strip() or None

    assignee_raw = data.get('assignee_id')
    up_for_grabs = True
    assignee_id = None
    auto_assign_requested = str(assignee_raw) == '-2'

    if auto_assign_requested:
        # Auto-assign to best available RA
        assignee_id = find_best_assignee(estimated_hours, due_date)
        if assignee_id:
            up_for_grabs = False
    elif assignee_raw and str(assignee_raw) != '-1':
        person = Person.query.get(int(assignee_raw))
        if person:
            assignee_id = person.id
            up_for_grabs = False

    task = Task(
        title=title,
        due_date=due_date,
        description=description,
        assignee_id=assignee_id,
        estimated_hours=estimated_hours,
        up_for_grabs=up_for_grabs,
        status='assigned' if assignee_id else 'assigned',
        priority=priority,
        assigner=assigner
    )
    db.session.add(task)
    db.session.commit()

    # Auto-schedule if assigned
    if assignee_id:
        auto_schedule_task(task)
        # Send assignment email
        person = Person.query.get(assignee_id)
        if person:
            send_task_assigned_email(task, person)

    return redirect(url_for('index'))

@app.post('/api/tasks/<int:task_id>/update')
def api_update_task(task_id: int):
    task = Task.query.get_or_404(task_id)
    data = request.form or request.json or {}

    if 'title' in data:
        task.title = (data.get('title') or '').strip() or task.title
    if 'due_date' in data:
        task.due_date = parse_date(data.get('due_date'))
    if 'description' in data:
        task.description = data.get('description') or ''
    if 'estimated_hours' in data:
        try:
            task.estimated_hours = float(data.get('estimated_hours') or 0)
        except ValueError:
            task.estimated_hours = 0

    # Handle new fields
    if 'status' in data:
        task.status = data.get('status') or 'assigned'
    if 'priority' in data:
        try:
            task.priority = int(data.get('priority') or 3)
        except ValueError:
            task.priority = 3
    if 'assigner' in data:
        task.assigner = (data.get('assigner') or '').strip() or None

    old_assignee_id = task.assignee_id
    old_assignee = Person.query.get(old_assignee_id) if old_assignee_id else None
    new_assignee = None
    assignee_changed = False

    if 'assignee_id' in data:
        raw = data.get('assignee_id')
        auto_assign_requested = str(raw) == '-2'

        if auto_assign_requested:
            # Auto-assign to best available RA
            new_id = find_best_assignee(task.estimated_hours, task.due_date)
            if new_id:
                task.assignee_id = new_id
                task.up_for_grabs = False
                new_assignee = Person.query.get(new_id)
            else:
                task.assignee_id = None
                task.up_for_grabs = True
        elif raw and str(raw) != '-1':
            person = Person.query.get(int(raw))
            if person:
                task.assignee_id = person.id
                task.up_for_grabs = False
                new_assignee = person
        else:
            task.assignee_id = None
            task.up_for_grabs = True

        assignee_changed = old_assignee_id != task.assignee_id

    db.session.commit()

    # Auto-schedule if reschedule checkbox is checked or assignee changed
    reschedule = data.get('reschedule') == '1' or data.get('reschedule') == 'true'
    if reschedule or assignee_changed:
        if task.assignee_id:
            auto_schedule_task(task)

    # Send email notifications for assignee changes
    if assignee_changed:
        if task.up_for_grabs:
            # Task set to "up for grabs" - notify everyone
            send_task_up_for_grabs_email(task)
        elif old_assignee_id and task.assignee_id:
            # Reassigned from one person to another
            send_task_reassigned_email(task, old_assignee, new_assignee)
        elif not old_assignee_id and task.assignee_id:
            # Newly assigned (was up for grabs, now has assignee)
            send_task_assigned_email(task, new_assignee)

    # If status changed to complete, redirect to archive
    if task.status == 'complete':
        return redirect(url_for('archive'))

    return redirect(url_for('task_detail', task_id=task.id))

@app.post('/api/tasks/<int:task_id>/complete')
def api_complete_task(task_id: int):
    task = Task.query.get_or_404(task_id)
    task.status = 'complete'
    task.scheduling_flag = None
    task.scheduling_message = None
    db.session.commit()
    # Send completion email
    send_task_completed_email(task)
    return redirect(url_for('archive'))

@app.post('/api/tasks/<int:task_id>/uncomplete')
def api_uncomplete_task(task_id: int):
    """Undo task completion - restore task to assigned status."""
    task = Task.query.get_or_404(task_id)
    task.status = 'assigned'
    db.session.commit()
    # Re-schedule if task has an assignee
    if task.assignee_id:
        auto_schedule_task(task)
    return redirect(url_for('index'))

@app.post('/api/tasks/<int:task_id>/delete')
def api_delete_task(task_id: int):
    task = Task.query.get_or_404(task_id)
    ScheduledBlock.query.filter_by(task_id=task.id).delete()
    db.session.delete(task)
    db.session.commit()
    return redirect(url_for('index'))

@app.get('/api/people/<int:person_id>/schedule')
def api_person_schedule(person_id: int):
    """Get a person's availability and assigned tasks as JSON."""
    person = Person.query.get_or_404(person_id)
    time_slots = json.loads(person.time_slots or '{}')
    weekly_hours = json.loads(person.weekly_hours or '{}')

    # Get all active tasks for this person
    tasks = Task.query.filter(
        Task.assignee_id == person_id,
        Task.status.notin_(['complete', 'completed', 'archived'])
    ).order_by(Task.priority.asc().nullslast(), Task.due_date.asc().nullslast()).all()

    tasks_data = [{
        'id': t.id,
        'title': t.title,
        'due_date': t.due_date.isoformat() if t.due_date else None,
        'estimated_hours': t.estimated_hours,
        'priority': t.priority,
        'status': t.status,
        'scheduled_json': json.loads(t.scheduled_json or '[]'),
        'scheduling_flag': t.scheduling_flag,
        'scheduling_message': t.scheduling_message
    } for t in tasks]

    return jsonify({
        'id': person.id,
        'name': person.name,
        'email': person.email,
        'time_slots': time_slots,
        'weekly_hours': weekly_hours,
        'tasks': tasks_data
    })

@app.post('/api/task/<int:task_id>/blocks')
def api_update_blocks(task_id: int):
    """Manual schedule editing via simple rows. Replaces all blocks for the task."""
    task = Task.query.get_or_404(task_id)
    # Expected arrays: date[], hours[]
    dates = request.form.getlist('block_date')
    hours_list = request.form.getlist('block_hours')

    ScheduledBlock.query.filter_by(task_id=task.id).delete()

    blocks = []
    for d, h in zip(dates, hours_list):
        d_parsed = parse_date(d)
        try:
            h_val = float(h)
        except ValueError:
            h_val = 0
        if d_parsed and h_val > 0 and task.assignee_id:
            blk = ScheduledBlock(task_id=task.id, person_id=task.assignee_id, day=d_parsed, hours=h_val)
            db.session.add(blk)
            blocks.append({"date": d_parsed.isoformat(), "hours": round(h_val,2)})

    task.scheduled_json = json.dumps(blocks)
    db.session.commit()

    return redirect(url_for('task_detail', task_id=task.id))

# -----------------------------
# TEMPLATES (inline for single-file simplicity)
# -----------------------------
BASE_CSS = """
:root { --fg:#111; --bg:#fafafa; --muted:#666; --card:#fff; --border:#e5e5e5; --accent:#2563eb; }
*{ box-sizing:border-box; }
body{ margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background:var(--bg); color:var(--fg);} 
header{ display:flex; gap:.5rem; align-items:center; padding:1rem; border-bottom:1px solid var(--border); background:#fff; position:sticky; top:0; z-index:10;}
header h1{ font-size:1.1rem; margin:0; font-weight:600;}
.container{ max-width:1100px; margin:0 auto; padding:1rem;}
.controls{ display:flex; gap:.5rem; align-items:center; flex-wrap:wrap;}
button, .btn{ background:var(--accent); color:#fff; border:none; padding:.5rem .75rem; border-radius:.5rem; cursor:pointer; font-weight:600; }
button.secondary{ background:#e5e7eb; color:#111; }
button.link{ background:transparent; color:var(--accent); padding:0; }
input, select, textarea{ width:100%; padding:.5rem .6rem; border:1px solid var(--border); border-radius:.5rem; background:#fff; color:#111; }
label{ font-size:.85rem; color:var(--muted);} 
.grid{ display:grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap:1rem; margin-top:1rem;}
.task-card{ background:var(--card); border:1px solid var(--border); border-radius:.75rem; padding:0.75rem; box-shadow: 0 1px 0 rgba(0,0,0,.03);}
.task-card.overdue{ border-color: #fca5a5; box-shadow: 0 0 0 2px #fee2e2 inset; }
.task-card.up-for-grabs{ background: #fff7cc; }
/* Status colors */
.task-card.status-assigned{ border-left: 4px solid #3b82f6; }
.task-card.status-in-progress{ border-left: 4px solid #f59e0b; }
.task-card.status-ready-for-review{ border-left: 4px solid #8b5cf6; }
.task-card.status-complete{ border-left: 4px solid #10b981; }
/* Scheduling flags */
.task-card.flag-red{ background-color: #fee2e2; }
.task-card.flag-orange{ background-color: #ffedd5; }
.scheduling-message{ font-size:.75rem; color:#dc2626; font-weight:600; margin-top:.25rem; }
.scheduling-message.orange{ color:#ea580c; }
/* Priority badges */
.priority-badge{ display:inline-block; padding:.1rem .4rem; border-radius:.4rem; font-size:.7rem; font-weight:600; margin-left:.25rem; }
.priority-1{ background:#fee2e2; color:#dc2626; }
.priority-2{ background:#ffedd5; color:#ea580c; }
.priority-3{ background:#fef3c7; color:#d97706; }
.priority-4{ background:#dbeafe; color:#2563eb; }
.priority-5{ background:#e0e7ff; color:#4f46e5; }
.task-head{ display:flex; justify-content:space-between; align-items:center; gap:.5rem;}
.task-title{ font-weight:700; font-size:1rem;}
.task-meta{ font-size:.8rem; color:var(--muted); margin-top:.25rem;}
.badge{ display:inline-block; padding:.1rem .4rem; background:#eef2ff; color:#3730a3; border-radius:.4rem; font-size:.75rem; }
.form-grid{ display:grid; grid-template-columns:1fr 1fr; gap:.75rem; }
.form-grid-3{ display:grid; grid-template-columns: 1fr 1fr 1fr; gap:.75rem; }
hr{ border:none; border-top:1px solid var(--border); margin:1rem 0; }
.table{ width:100%; border-collapse: collapse; }
.table th, .table td{ border-bottom:1px solid var(--border); padding:.4rem; text-align:left; font-size:.9rem; }
.small{ font-size:.8rem; color:var(--muted); }
"""

INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Taskboard</title>
  <style>{{ BASE_CSS }}</style>
</head>
<body>
<header>
  <h1>Taskboard</h1>
  <div id="live-clock" style="font-size:0.8rem; color:#666; margin-left:1rem;">{{ est_datetime }} EST</div>
  <div class="controls" style="margin-left:auto;">
    <button onclick="document.getElementById('newTask').toggleAttribute('hidden')">New Task</button>
    <button onclick="window.location.href='{{ url_for('archive') }}'">Completed Tasks</button>
  </div>
</header>
<div class="container">
  <form id="newTask" method="post" action="{{ url_for('api_create_task') }}" hidden style="background:#fff; border:2px solid #2563eb; border-radius:0.75rem; padding:1.25rem; margin-bottom:1.5rem; box-shadow:0 4px 12px rgba(37,99,235,0.15);">
    <h2 style="margin:0 0 1rem 0; font-size:1.1rem; color:#2563eb;">Create New Task</h2>
    <div class="form-grid">
      <div>
        <label>Title</label>
        <input name="title" required placeholder="e.g., Draft IRB protocol">
      </div>
      <div>
        <label>Due date</label>
        <input type="date" name="due_date" value="{{ today.isoformat() }}">
      </div>
      <div>
        <label>Assignee</label>
        <select name="assignee_id">
          <option value="-1">Up for grabs</option>
          <option value="-2">Auto-assign</option>
          {% for p in people %}<option value="{{p.id}}">{{p.name}}</option>{% endfor %}
        </select>
      </div>
      <div>
        <label>Estimated hours</label>
        <input name="estimated_hours" type="number" min="0" step="0.25" placeholder="e.g., 6">
      </div>
      <div>
        <label>Priority</label>
        <select name="priority">
          <option value="1">1 - Highest</option>
          <option value="2">2 - High</option>
          <option value="3" selected>3 - Medium</option>
          <option value="4">4 - Low</option>
          <option value="5">5 - Lowest</option>
        </select>
      </div>
      <div>
        <label>Assigned by</label>
        <input name="assigner" placeholder="Your name">
      </div>
      <div style="grid-column:1/-1">
        <label>Description</label>
        <textarea name="description" rows="2" placeholder="Short context"></textarea>
      </div>
    </div>
    <div style="margin-top:.75rem; display:flex; gap:.5rem;">
      <button type="submit">Create Task</button>
      <button type="button" class="secondary" onclick="document.getElementById('newTask').hidden=true">Cancel</button>
    </div>
  </form>

  <div class="grid">
    {% for t in tasks if t.status != 'complete' %}
    <div class="{{ task_css(t) }}">
      <div class="task-head">
        <div class="task-title">
          {{ t.title }}
          <span class="priority-badge priority-{{ t.priority or 3 }}">P{{ t.priority or 3 }}</span>
        </div>
        <a class="badge" href="{{ url_for('task_detail', task_id=t.id) }}">Open</a>
      </div>
      <div class="task-meta">
        {% if t.due_date %}Due: {{ t.due_date.strftime('%b %d, %Y') }}{% else %}No due date{% endif %} ·
        {% if t.up_for_grabs %}<strong>Up for grabs</strong>{% else %}Assignee: {{ t.assignee.name }}{% endif %}
        {% if t.estimated_hours %} · Est: {{ '%.2f'|format(t.estimated_hours) }}h{% endif %}
        {% if t.assigner %} · By: {{ t.assigner }}{% endif %}
      </div>
      <div class="task-meta">Status: {{ t.status|replace('_', ' ')|title }}</div>
      {% if t.scheduling_message %}
      <div class="scheduling-message {{ 'orange' if t.scheduling_flag == 'orange' else '' }}">⚠️ {{ t.scheduling_message }}</div>
      {% endif %}
      {% if t.description %}<div class="task-description" style="margin-top:.4rem;">{{ t.description }}</div>{% endif %}
      <form method="post" action="{{ url_for('api_complete_task', task_id=t.id) }}" style="margin-top:.6rem; display:flex; gap:.5rem;">
        <button>Mark complete</button>
        <form method="post" action="{{ url_for('api_delete_task', task_id=t.id) }}">
          <button class="secondary" formaction="{{ url_for('api_delete_task', task_id=t.id) }}">Delete</button>
        </form>
      </form>
    </div>
    {% endfor %}
  </div>

  <hr style="margin-top:2rem;">

  <h2>Weekly Schedule</h2>
  <div class="schedule-grid" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:1rem; margin-top:1rem;">
    {% for day_idx in range(5) %}
    <div class="schedule-day">
      <h3 style="margin:0 0 0.5rem 0; font-size:1rem; color:#2563eb;">{{ WEEKDAY_LABELS[day_idx] }}</h3>
      <ul style="list-style:none; padding:0; margin:0; font-size:0.9rem;">
        {% for p in people %}
          {% set slots = json.loads(p.time_slots or '{}').get(day_idx|string, []) %}
          {% for slot in slots %}
            <li>{{ p.name }}: {{ format_time_12h(slot.start) }} - {{ format_time_12h(slot.end) }}</li>
          {% endfor %}
        {% endfor %}
      </ul>
    </div>
    {% endfor %}
  </div>

  <hr style="margin-top:2rem;">

  <h2>Individual RA Schedule (2 Weeks)</h2>
  <div style="margin-top:1rem;">
    <select id="ra-selector" onchange="showRASchedule(this.value)" style="margin-bottom:1rem;">
      <option value="">Select an RA to view their schedule...</option>
      {% for p in people %}
      <option value="{{ p.id }}">{{ p.name }}</option>
      {% endfor %}
    </select>
    <div id="ra-schedule-view"></div>
  </div>

  <script>
    const peopleData = {{ people_json|safe }};
    const tasksData = {{ tasks_json|safe }};
    const twoWeeksDates = {{ two_weeks_dates_json|safe }};
    const estToday = '{{ est_today }}';
    const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

    function formatDate(dateStr) {
      const d = new Date(dateStr + 'T12:00:00');
      const month = d.toLocaleString('en-US', { month: 'short' });
      const day = d.getDate();
      return month + ' ' + day;
    }

    function formatTime12h(timeStr) {
      // Convert HH:MM to 12-hour AM/PM format
      const [hours, minutes] = timeStr.split(':').map(Number);
      const period = hours >= 12 ? 'PM' : 'AM';
      const hour12 = hours % 12 || 12;
      return hour12 + ':' + (minutes < 10 ? '0' : '') + minutes + ' ' + period;
    }

    function formatTimeRange(start, end) {
      return formatTime12h(start) + ' - ' + formatTime12h(end);
    }

    function showRASchedule(personId) {
      const container = document.getElementById('ra-schedule-view');
      if (!personId) {
        container.innerHTML = '';
        return;
      }

      const person = peopleData.find(p => p.id == personId);
      if (!person) {
        container.innerHTML = '<p>Person not found</p>';
        return;
      }

      const slots = person.time_slots || {};
      const personTasks = tasksData.filter(t => t.assignee_id == personId && t.status !== 'complete' && t.status !== 'completed');

      // Split into two weeks (first 7 days = week 1, next 7 days = week 2)
      const week1Dates = twoWeeksDates.slice(0, 7);
      const week2Dates = twoWeeksDates.slice(7, 14);

      let html = '';

      // Week 1 - Current Week
      html += '<h3 style="margin:1rem 0 0.5rem 0; font-size:1rem; color:#333;">This Week</h3>';
      html += '<div class="schedule-grid" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:0.75rem; margin-bottom:1.5rem;">';

      week1Dates.forEach((dateStr, idx) => {
        // Only show Monday-Friday (idx 0-4)
        if (idx > 4) return;

        const d = new Date(dateStr + 'T12:00:00');
        const dayIdx = d.getDay(); // 0=Sun, 1=Mon, etc.
        const weekdayIdx = (dayIdx === 0) ? 6 : dayIdx - 1; // Convert to 0=Mon format
        const daySlots = slots[weekdayIdx.toString()] || [];
        const isToday = dateStr === estToday;

        html += '<div class="schedule-day" style="background:' + (isToday ? '#dbeafe' : '#f9fafb') + '; padding:0.75rem; border-radius:0.5rem;' + (isToday ? ' border:2px solid #3b82f6;' : '') + '">';
        html += '<h4 style="margin:0 0 0.25rem 0; font-size:0.9rem; color:#2563eb;">' + WEEKDAYS[dayIdx] + '</h4>';
        html += '<div style="font-size:0.8rem; color:#666; margin-bottom:0.4rem;">' + formatDate(dateStr) + (isToday ? ' <strong>(Today)</strong>' : '') + '</div>';

        if (daySlots.length > 0) {
          html += '<div style="font-size:0.75rem; color:#666; margin-bottom:0.3rem;">Available: ';
          html += daySlots.map(s => formatTimeRange(s.start, s.end)).join(', ');
          html += '</div>';
        } else {
          html += '<div style="font-size:0.75rem; color:#999;">Not available</div>';
        }

        // Find tasks scheduled for this specific date
        const dayTasks = [];
        personTasks.forEach(t => {
          const schedule = JSON.parse(t.scheduled_json || '[]');
          const block = schedule.find(b => b.date === dateStr);
          if (block) {
            dayTasks.push({ task: t, hours: block.hours });
          }
        });

        if (dayTasks.length > 0) {
          html += '<div style="margin-top:0.4rem; font-size:0.75rem;"><strong>Tasks:</strong></div>';
          html += '<ul style="list-style:none; padding:0; margin:0.2rem 0 0 0; font-size:0.75rem;">';
          dayTasks.forEach(item => {
            const flagColor = item.task.scheduling_flag === 'red' ? '#fee2e2' : (item.task.scheduling_flag === 'orange' ? '#ffedd5' : '');
            html += '<li style="padding:0.15rem 0;' + (flagColor ? ' background:' + flagColor + '; padding:0.15rem 0.25rem; border-radius:0.25rem;' : '') + '">• ' + item.task.title + ' (' + item.hours + 'h)</li>';
          });
          html += '</ul>';
        }

        html += '</div>';
      });

      html += '</div>';

      // Week 2 - Next Week
      html += '<h3 style="margin:1rem 0 0.5rem 0; font-size:1rem; color:#333;">Next Week</h3>';
      html += '<div class="schedule-grid" style="display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:0.75rem;">';

      week2Dates.forEach((dateStr, idx) => {
        // Only show Monday-Friday (idx 0-4)
        if (idx > 4) return;

        const d = new Date(dateStr + 'T12:00:00');
        const dayIdx = d.getDay();
        const weekdayIdx = (dayIdx === 0) ? 6 : dayIdx - 1;
        const daySlots = slots[weekdayIdx.toString()] || [];

        html += '<div class="schedule-day" style="background:#f9fafb; padding:0.75rem; border-radius:0.5rem;">';
        html += '<h4 style="margin:0 0 0.25rem 0; font-size:0.9rem; color:#2563eb;">' + WEEKDAYS[dayIdx] + '</h4>';
        html += '<div style="font-size:0.8rem; color:#666; margin-bottom:0.4rem;">' + formatDate(dateStr) + '</div>';

        if (daySlots.length > 0) {
          html += '<div style="font-size:0.75rem; color:#666; margin-bottom:0.3rem;">Available: ';
          html += daySlots.map(s => formatTimeRange(s.start, s.end)).join(', ');
          html += '</div>';
        } else {
          html += '<div style="font-size:0.75rem; color:#999;">Not available</div>';
        }

        // Find tasks scheduled for this specific date
        const dayTasks = [];
        personTasks.forEach(t => {
          const schedule = JSON.parse(t.scheduled_json || '[]');
          const block = schedule.find(b => b.date === dateStr);
          if (block) {
            dayTasks.push({ task: t, hours: block.hours });
          }
        });

        if (dayTasks.length > 0) {
          html += '<div style="margin-top:0.4rem; font-size:0.75rem;"><strong>Tasks:</strong></div>';
          html += '<ul style="list-style:none; padding:0; margin:0.2rem 0 0 0; font-size:0.75rem;">';
          dayTasks.forEach(item => {
            const flagColor = item.task.scheduling_flag === 'red' ? '#fee2e2' : (item.task.scheduling_flag === 'orange' ? '#ffedd5' : '');
            html += '<li style="padding:0.15rem 0;' + (flagColor ? ' background:' + flagColor + '; padding:0.15rem 0.25rem; border-radius:0.25rem;' : '') + '">• ' + item.task.title + ' (' + item.hours + 'h)</li>';
          });
          html += '</ul>';
        }

        html += '</div>';
      });

      html += '</div>';
      container.innerHTML = html;
    }

    // Live clock update
    function updateClock() {
      const clockEl = document.getElementById('live-clock');
      if (clockEl) {
        const now = new Date();
        const options = {
          weekday: 'short',
          month: 'short',
          day: 'numeric',
          year: 'numeric',
          hour: 'numeric',
          minute: '2-digit',
          hour12: true,
          timeZone: 'America/New_York'
        };
        const formatted = now.toLocaleString('en-US', options) + ' EST';
        clockEl.textContent = formatted;
      }
    }
    // Update clock every second
    setInterval(updateClock, 1000);
    // Initial update
    updateClock();

    // Convert URLs in text to clickable links
    function linkifyText(text) {
      const urlPattern = /(https?:\/\/[^\s<]+)/g;
      return text.replace(urlPattern, '<a href="$1" target="_blank" rel="noopener noreferrer" style="color:#2563eb; text-decoration:underline;">$1</a>');
    }

    // Apply linkify to all task descriptions
    function linkifyDescriptions() {
      const descriptions = document.querySelectorAll('.task-description');
      descriptions.forEach(el => {
        if (!el.dataset.linkified) {
          el.innerHTML = linkifyText(el.textContent);
          el.dataset.linkified = 'true';
        }
      });
    }

    // Run on page load
    document.addEventListener('DOMContentLoaded', linkifyDescriptions);
    linkifyDescriptions();
  </script>
</div>
</body>
</html>
""".replace("{{ BASE_CSS }}", "" + BASE_CSS)

ARCHIVE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Completed Tasks</title>
  <style>{{ BASE_CSS }}</style>
</head>
<body>
<header>
  <h1>Completed Tasks</h1>
  <div class="controls" style="margin-left:auto;">
    <a class="btn" href="{{ url_for('index') }}">Back to board</a>
  </div>
</header>
<div class="container">
  <div class="grid">
    {% for t in tasks %}
    <div class="task-card status-complete">
      <div class="task-head">
        <div class="task-title">{{ t.title }}</div>
      </div>
      <div class="task-meta">Completed · {% if t.due_date %}Due was {{ t.due_date.strftime('%b %d, %Y') }}{% else %}No due date{% endif %}</div>
      {% if t.assignee %}<div class="task-meta">Assignee: {{ t.assignee.name }}</div>{% endif %}
      {% if t.assigner %}<div class="task-meta">Assigned by: {{ t.assigner }}</div>{% endif %}
      {% if t.estimated_hours %}<div class="task-meta">Estimated: {{ '%.2f'|format(t.estimated_hours) }}h</div>{% endif %}
      {% if t.description %}<div class="task-description" style="margin-top:.4rem;">{{ t.description }}</div>{% endif %}
      <form method="post" action="{{ url_for('api_uncomplete_task', task_id=t.id) }}" style="margin-top:.6rem;">
        <button class="secondary">Undo Complete</button>
      </form>
    </div>
    {% else %}
    <p class="small">No completed tasks yet.</p>
    {% endfor %}
  </div>
</div>
<script>
  // Convert URLs in text to clickable links
  function linkifyText(text) {
    const urlPattern = /(https?:\/\/[^\s<]+)/g;
    return text.replace(urlPattern, '<a href="$1" target="_blank" rel="noopener noreferrer" style="color:#2563eb; text-decoration:underline;">$1</a>');
  }

  // Apply linkify to all task descriptions
  function linkifyDescriptions() {
    const descriptions = document.querySelectorAll('.task-description');
    descriptions.forEach(el => {
      if (!el.dataset.linkified) {
        el.innerHTML = linkifyText(el.textContent);
        el.dataset.linkified = 'true';
      }
    });
  }

  // Run on page load
  document.addEventListener('DOMContentLoaded', linkifyDescriptions);
  linkifyDescriptions();
</script>
</body>
</html>
""".replace("{{ BASE_CSS }}", "" + BASE_CSS)

TASK_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Task · {{ task.title }}</title>
  <style>{{ BASE_CSS }}</style>
</head>
<body>
<header>
  <h1>Task</h1>
  <div class="controls">
    <a class="btn" href="{{ url_for('index') }}">← Back</a>
  </div>
</header>
<div class="container">
  {% if task.scheduling_message %}
  <div class="scheduling-message {{ 'orange' if task.scheduling_flag == 'orange' else '' }}" style="padding:0.75rem; margin-bottom:1rem; border-radius:0.5rem; background:{{ '#ffedd5' if task.scheduling_flag == 'orange' else '#fee2e2' }};">
    ⚠️ {{ task.scheduling_message }}
  </div>
  {% endif %}

  <form method="post" action="{{ url_for('api_update_task', task_id=task.id) }}" style="background:#fff; border:2px solid #2563eb; border-radius:0.75rem; padding:1.25rem; margin-bottom:1.5rem; box-shadow:0 4px 12px rgba(37,99,235,0.15);">
    <h2 style="margin:0 0 1rem 0; font-size:1.1rem; color:#2563eb;">Edit Task</h2>
    <div class="form-grid">
      <div>
        <label>Title</label>
        <input name="title" value="{{ task.title }}" required>
      </div>
      <div>
        <label>Due date</label>
        <input type="date" name="due_date" value="{{ task.due_date.isoformat() if task.due_date else '' }}">
      </div>
      <div>
        <label>Assignee</label>
        <select name="assignee_id">
          <option value="-1" {% if task.up_for_grabs %}selected{% endif %}>Up for grabs</option>
          <option value="-2">Auto-assign</option>
          {% for p in people %}<option value="{{p.id}}" {% if task.assignee_id==p.id %}selected{% endif %}>{{p.name}}</option>{% endfor %}
        </select>
        <div class="small">Change assignee and tick "Reschedule" to auto-fill based on working hours.</div>
      </div>
      <div>
        <label>Estimated hours</label>
        <input name="estimated_hours" type="number" min="0" step="0.25" value="{{ '%.2f'|format(task.estimated_hours or 0) }}">
      </div>
      <div>
        <label>Status</label>
        <select name="status">
          <option value="assigned" {% if task.status == 'assigned' %}selected{% endif %}>Assigned</option>
          <option value="in_progress" {% if task.status == 'in_progress' %}selected{% endif %}>In Progress</option>
          <option value="ready_for_review" {% if task.status == 'ready_for_review' %}selected{% endif %}>Ready for Review</option>
          <option value="complete" {% if task.status in ['complete', 'completed'] %}selected{% endif %}>Complete</option>
        </select>
      </div>
      <div>
        <label>Priority</label>
        <select name="priority">
          <option value="1" {% if task.priority == 1 %}selected{% endif %}>1 - Highest</option>
          <option value="2" {% if task.priority == 2 %}selected{% endif %}>2 - High</option>
          <option value="3" {% if (task.priority or 3) == 3 %}selected{% endif %}>3 - Medium</option>
          <option value="4" {% if task.priority == 4 %}selected{% endif %}>4 - Low</option>
          <option value="5" {% if task.priority == 5 %}selected{% endif %}>5 - Lowest</option>
        </select>
      </div>
      <div>
        <label>Assigned by</label>
        <input name="assigner" value="{{ task.assigner or '' }}" placeholder="Name of assigner">
      </div>
      <div style="display:flex; align-items:center; gap:.5rem;">
        <input type="checkbox" id="reschedule" name="reschedule" value="1">
        <label for="reschedule">Reschedule automatically now</label>
      </div>
      <div style="grid-column:1/-1">
        <label>Description</label>
        <textarea name="description" rows="3">{{ task.description or '' }}</textarea>
      </div>
    </div>
    <div style="margin-top:.75rem; display:flex; gap:.5rem;">
      <button type="submit">Save</button>
      <form method="post" action="{{ url_for('api_complete_task', task_id=task.id) }}">
        <button class="secondary" formaction="{{ url_for('api_complete_task', task_id=task.id) }}">Mark complete</button>
      </form>
    </div>
  </form>

  <hr>

  <h3>Schedule</h3>
  <p class="small">Automatic plan uses the assignee’s weekly working hours. You can edit the blocks below. Saving replaces the schedule.</p>
  <form method="post" action="{{ url_for('api_update_blocks', task_id=task.id) }}">
    <table class="table">
      <thead>
        <tr><th>Date</th><th>Hours</th></tr>
      </thead>
      <tbody id="blocks-body">
        {% for b in blocks %}
        <tr>
          <td><input type="date" name="block_date" value="{{ b.day.isoformat() }}"></td>
          <td><input type="number" name="block_hours" min="0" step="0.25" value="{{ '%.2f'|format(b.hours) }}"></td>
        </tr>
        {% endfor %}
        {% if not blocks %}
        <tr>
          <td><input type="date" name="block_date" value=""></td>
          <td><input type="number" name="block_hours" min="0" step="0.25" value=""></td>
        </tr>
        {% endif %}
      </tbody>
    </table>
    <div style="margin:.5rem 0; display:flex; gap:.5rem;">
      <button type="button" class="secondary" onclick="addRow()">Add row</button>
      <button type="submit">Save schedule</button>
    </div>
  </form>

  {% if task.assignee %}
  <hr>
  <h3>{{ task.assignee.name }} · Weekly working hours</h3>
  {% set weekly = json.loads(task.assignee.weekly_hours or '{}') %}
  <table class="table">
    <thead><tr><th>Day</th><th>Hours</th></tr></thead>
    <tbody>
      {% for i in range(0,7) %}
      <tr><td>{{ WEEKDAY_LABELS[i] }}</td><td>{{ weekly.get(i|string, 0) }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

</div>
<script>
function addRow(){
  const tbody = document.getElementById('blocks-body');
  const tr = document.createElement('tr');
  tr.innerHTML = `<td><input type="date" name="block_date"></td>
                  <td><input type="number" name="block_hours" min="0" step="0.25"></td>`;
  tbody.appendChild(tr);
}
</script>
</body>
</html>
""".replace("{{ BASE_CSS }}", "" + BASE_CSS)

# -----------------------------
# Entrypoint
# -----------------------------
if __name__ == '__main__':
    app.run(debug=True)

# =============================
# requirements.txt (place this in a separate file when deploying)
# =============================
# flask==3.0.0
# flask_sqlalchemy==3.1.1
# sqlalchemy==2.0.23
# gunicorn==22.0.0
# python-dateutil==2.9.0.post0

# =============================
# render.yaml — one-click deploy on Render
# =============================
# services:
#   - type: web
#     name: minimal-taskboard
#     runtime: python
#     region: oregon
#     plan: starter
#     buildCommand: pip install -r requirements.txt
#     startCommand: gunicorn app:app
#     envVars:
#       - key: DATABASE_URL
#         sync: false  # Let Render provide managed Postgres or set a value; falls back to SQLite if unset
