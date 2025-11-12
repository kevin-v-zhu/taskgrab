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
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///taskboard.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# -----------------------------
# Models
# -----------------------------
class Person(db.Model):
    __tablename__ = 'people'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False, unique=True)
    # weekly_hours JSON mapping weekday index 0..6 -> float hours available that day
    weekly_hours = db.Column(db.Text, nullable=False, default='{}')

class Task(db.Model):
    __tablename__ = 'tasks'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String, nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    description = db.Column(db.Text, nullable=True)
    assignee_id = db.Column(db.Integer, db.ForeignKey('people.id'), nullable=True)
    estimated_hours = db.Column(db.Float, nullable=True, default=0.0)
    status = db.Column(db.String, nullable=False, default='in_progress')  # in_progress | completed | archived
    up_for_grabs = db.Column(db.Boolean, nullable=False, default=True)
    # convenience cache of JSON scheduled blocks for quick display (kept in sync with ScheduledBlock rows)
    scheduled_json = db.Column(db.Text, nullable=False, default='[]')

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
    db.create_all()
    if Person.query.count() == 0:
        
        vidya_schedule = {"0": 3, "1": 0, "2": 3, "3": 0, "4": 4, "5": 0, "6": 0}  # Mon..Sun mapping by index (Mon=0)
        ariel_schedule = {"0": 3, "1": 3, "2": 3, "3": 0, "4": 0, "5": 0, "6": 0}
        ray_schedule = {"0": 0, "1": 2, "2": 0, "3": 0, "4": 4, "5": 0, "6": 0}
        viveka_schedule = {"0": 0, "1": 2.5, "2": 0, "3": 3, "4": 3, "5": 0, "6": 0}
        melinda_schedule = {"0": 0, "1": 1.5, "2": 1, "3": 1.5, "4": 6, "5": 0, "6": 0}
        collin_schedule = {"0": 10, "1": 10, "2": 10, "3": 10, "4": 10, "5": 10, "6": 10}
        andrew_schedule = {"0": 4, "1": 0, "2": 8, "3": 0, "4": 8, "5": 0, "6": 0}

        for name in ["Vidya", "Ariel", "Ray", "Viveka", "Melinda", "Collin", "Andrew"]:
            db.session.add(Person(name=name, weekly_hours=json.dumps(locals()[f"{name.lower()}_schedule"])))
        db.session.commit()

# -----------------------------
# Utilities
# -----------------------------
WEEKDAY_LABELS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


def person_remaining_capacity_on_day(person_id: int, day: date) -> float:
    person = Person.query.get(person_id)
    if not person:
        return 0.0
    weekly = json.loads(person.weekly_hours or '{}')
    day_idx = (day.weekday())  # Monday=0
    day_hours = float(weekly.get(str(day_idx), 0))
    # sum existing blocks for this person on that day
    used = db.session.query(func.coalesce(func.sum(ScheduledBlock.hours), 0.0)).\
        filter(ScheduledBlock.person_id==person_id, ScheduledBlock.day==day).scalar() or 0.0
    return max(0.0, day_hours - float(used))


def auto_schedule_task(task: Task):
    """Fill ScheduledBlock rows for task based on assignee weekly_hours and estimated_hours.
    Overwrites any existing schedule for the task. Minimal, forward-fill algorithm.
    """
    # Clear existing blocks
    ScheduledBlock.query.filter_by(task_id=task.id).delete()

    if not task.assignee_id or not task.estimated_hours or task.estimated_hours <= 0:
        task.scheduled_json = '[]'
        db.session.commit()
        return

    remaining = float(task.estimated_hours)
    today = date.today()
    day_ptr = today
    blocks = []
    safety = 365  # prevent infinite loop

    while remaining > 0 and safety > 0:
        capacity = person_remaining_capacity_on_day(task.assignee_id, day_ptr)
        if capacity > 0:
            chunk = min(capacity, remaining)
            blk = ScheduledBlock(task_id=task.id, person_id=task.assignee_id, day=day_ptr, hours=chunk)
            db.session.add(blk)
            blocks.append({"date": day_ptr.isoformat(), "hours": round(chunk,2)})
            remaining -= chunk
        day_ptr += timedelta(days=1)
        safety -= 1

    task.scheduled_json = json.dumps(blocks)
    db.session.commit()


def task_css(task: Task) -> str:
    classes = ["task-card"]
    # up for grabs
    if task.up_for_grabs:
        classes.append("up-for-grabs")
    # overdue
    if task.due_date and task.status != 'completed' and date.today() > task.due_date:
        classes.append("overdue")
    # in progress default look handled by CSS .task-card
    return ' '.join(classes)

# -----------------------------
# Routes: UI pages
# -----------------------------
@app.route('/')
def index():
    tasks = Task.query.filter(Task.status != 'archived').order_by(Task.id.desc()).all()
    people = Person.query.order_by(Person.name).all()
    return render_template_string(INDEX_HTML, tasks=tasks, people=people, task_css=task_css, WEEKDAY_LABELS=WEEKDAY_LABELS, json=json, today=date.today())

@app.route('/archive')
def archive():
    tasks = Task.query.filter(Task.status=='completed').order_by(Task.id.desc()).all()
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

    assignee_raw = data.get('assignee_id')
    up_for_grabs = True
    assignee_id = None
    if assignee_raw and str(assignee_raw) != '-1':
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
        status='in_progress'
    )
    db.session.add(task)
    db.session.commit()

    if assignee_id:
        auto_schedule_task(task)

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

    if 'assignee_id' in data:
        raw = data.get('assignee_id')
        if raw and str(raw) != '-1':
            person = Person.query.get(int(raw))
            if person:
                task.assignee_id = person.id
                task.up_for_grabs = False
        else:
            task.assignee_id = None
            task.up_for_grabs = True

    db.session.commit()

    # optional reschedule toggle
    if (data.get('reschedule') == '1') and task.assignee_id:
        auto_schedule_task(task)

    return redirect(url_for('task_detail', task_id=task.id))

@app.post('/api/tasks/<int:task_id>/complete')
def api_complete_task(task_id: int):
    task = Task.query.get_or_404(task_id)
    task.status = 'completed'
    db.session.commit()
    return redirect(url_for('archive'))

@app.post('/api/tasks/<int:task_id>/delete')
def api_delete_task(task_id: int):
    task = Task.query.get_or_404(task_id)
    ScheduledBlock.query.filter_by(task_id=task.id).delete()
    db.session.delete(task)
    db.session.commit()
    return redirect(url_for('index'))

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
  <div class="controls">
    <button onclick="document.getElementById('newTask').toggleAttribute('hidden')">＋ New Task</button>
    <a class="btn" href="{{ url_for('archive') }}">Archive</a>
  </div>
</header>
<div class="container">
  <form id="newTask" method="post" action="{{ url_for('api_create_task') }}" hidden>
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
          {% for p in people %}<option value="{{p.id}}">{{p.name}}</option>{% endfor %}
        </select>
      </div>
      <div>
        <label>Estimated hours</label>
        <input name="estimated_hours" type="number" min="0" step="0.25" placeholder="e.g., 6">
      </div>
      <div style="grid-column:1/-1">
        <label>Description</label>
        <textarea name="description" rows="2" placeholder="Short context"></textarea>
      </div>
    </div>
    <div style="margin-top:.5rem; display:flex; gap:.5rem;">
      <button type="submit">Create</button>
      <button type="button" class="secondary" onclick="document.getElementById('newTask').hidden=true">Cancel</button>
    </div>
  </form>

  <div class="grid">
    {% for t in tasks if t.status != 'completed' %}
    <div class="{{ task_css(t) }}">
      <div class="task-head">
        <div class="task-title">{{ t.title }}</div>
        <a class="badge" href="{{ url_for('task_detail', task_id=t.id) }}">Open</a>
      </div>
      <div class="task-meta">
        {% if t.due_date %}Due: {{ t.due_date.strftime('%b %d, %Y') }}{% else %}No due date{% endif %} ·
        {% if t.up_for_grabs %}<strong>Up for grabs</strong>{% else %}Assignee: {{ t.assignee.name }}{% endif %}
        {% if t.estimated_hours %} · Est: {{ '%.2f'|format(t.estimated_hours) }}h{% endif %}
      </div>
      {% if t.description %}<div style="margin-top:.4rem;">{{ t.description }}</div>{% endif %}
      {% set sched = json.loads(t.scheduled_json) %}
      {% if sched %}
      <div class="small" style="margin-top:.4rem;">Schedule: 
        {% for b in sched %}{{ b.date }} ({{ b.hours }}h){% if not loop.last %}, {% endif %}{% endfor %}
      </div>
      {% endif %}
      <form method="post" action="{{ url_for('api_complete_task', task_id=t.id) }}" style="margin-top:.6rem; display:flex; gap:.5rem;">
        <button>Mark complete</button>
        <form method="post" action="{{ url_for('api_delete_task', task_id=t.id) }}">
          <button class="secondary" formaction="{{ url_for('api_delete_task', task_id=t.id) }}">Delete</button>
        </form>
      </form>
    </div>
    {% endfor %}
  </div>
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
  <title>Archive</title>
  <style>{{ BASE_CSS }}</style>
</head>
<body>
<header>
  <h1>Archive</h1>
  <div class="controls">
    <a class="btn" href="{{ url_for('index') }}">← Back to board</a>
  </div>
</header>
<div class="container">
  <div class="grid">
    {% for t in tasks %}
    <div class="task-card">
      <div class="task-head">
        <div class="task-title">{{ t.title }}</div>
      </div>
      <div class="task-meta">Completed · {% if t.due_date %}Due was {{ t.due_date.strftime('%b %d, %Y') }}{% else %}No due date{% endif %}</div>
      {% if t.description %}<div style="margin-top:.4rem;">{{ t.description }}</div>{% endif %}
    </div>
    {% else %}
    <p class="small">No completed tasks yet.</p>
    {% endfor %}
  </div>
</div>
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
  <form method="post" action="{{ url_for('api_update_task', task_id=task.id) }}">
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
          {% for p in people %}<option value="{{p.id}}" {% if task.assignee_id==p.id %}selected{% endif %}>{{p.name}}</option>{% endfor %}
        </select>
        <div class="small">Change assignee and tick “Reschedule” to auto-fill based on working hours.</div>
      </div>
      <div>
        <label>Estimated hours</label>
        <input name="estimated_hours" type="number" min="0" step="0.25" value="{{ '%.2f'|format(task.estimated_hours or 0) }}">
      </div>
      <div style="grid-column:1/-1">
        <label>Description</label>
        <textarea name="description" rows="3">{{ task.description or '' }}</textarea>
      </div>
      <div style="display:flex; align-items:center; gap:.5rem;">
        <input type="checkbox" id="reschedule" name="reschedule" value="1">
        <label for="reschedule">Reschedule automatically now</label>
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
