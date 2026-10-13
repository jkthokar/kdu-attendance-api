
#!/usr/bin/env python3
# ================================================================
# api/app.py — KDU Smart Attendance System v4.0
# Flask REST API — deploy free on Render.com
# ================================================================

import os
import sqlite3
import hashlib
import secrets
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder='web', static_url_path='/web')
CORS(app)

# ── Config ────────────────────────────────────────────────────────
SECRET_KEY    = os.environ.get('SECRET_KEY', 'kdu-attendance-secret-change-me')
SYNC_TOKEN    = os.environ.get('SYNC_TOKEN', 'kdu-sync-token-change-me')
PROF_PASSWORD = os.environ.get('PROF_PASSWORD', 'prof1234')
DB_PATH       = os.environ.get('DB_PATH', 'attendance_cloud.db')

# ── DB helpers ────────────────────────────────────────────────────
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop('db', None)
    if db:
        db.close()

def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS students (
            student_id   TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            gmail        TEXT,
            enrolled_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS subjects (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            semester TEXT
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            subject     TEXT NOT NULL,
            semester    TEXT,
            started_at  TEXT,
            ended_at    TEXT,
            synced_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER NOT NULL,
            student_id  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT "Absent",
            confidence  REAL,
            marked_at   TEXT,
            UNIQUE(session_id, student_id)
        );
    ''')
    db.commit()
    db.close()

init_db()

# ── Auth helpers ──────────────────────────────────────────────────
def make_token(student_id):
    raw = f'{student_id}:{SECRET_KEY}'
    return hashlib.sha256(raw.encode()).hexdigest()[:32]

def require_sync_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-Sync-Token', '')
        if token != SYNC_TOKEN:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def require_student_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-Student-Token', '')
        sid   = request.headers.get('X-Student-ID', '')
        if not sid or token != make_token(sid):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def make_prof_token():
    raw = f'prof:{PROF_PASSWORD}:{SECRET_KEY}'
    return hashlib.sha256(raw.encode()).hexdigest()[:48]

def require_prof_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-Prof-Token', '')
        if token != make_prof_token():
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

# ── Public endpoints ──────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({
        'system': 'KDU Smart Attendance API v4.0',
        'university': 'Kyungdong University',
        'department': 'Artificial Intelligence',
        'group': 'Group 11 — Jay Kumar Tamang',
        'status': 'online'
    })

@app.route('/portal')
@app.route('/portal/')
def portal():
    """Serve the student web portal (web/index.html)."""
    return app.send_static_file('index.html')

@app.route('/api/login', methods=['POST'])
def login():
    """Student login — returns auth token."""
    data = request.get_json(force=True)
    sid  = str(data.get('student_id', '')).strip()
    if not sid:
        return jsonify({'error': 'student_id required'}), 400

    db  = get_db()
    row = db.execute('SELECT * FROM students WHERE student_id=?', (sid,)).fetchone()
    if not row:
        return jsonify({'error': 'Student ID not found'}), 404

    return jsonify({
        'token':      make_token(sid),
        'student_id': sid,
        'name':       row['name'],
        'gmail':      row['gmail'] or '',
    })

# ── Student endpoints (require student token) ─────────────────────

@app.route('/api/my/summary')
@require_student_token
def my_summary():
    """Per-subject attendance summary for logged-in student."""
    sid = request.headers.get('X-Student-ID')
    db  = get_db()

    rows = db.execute('''
        SELECT s.subject,
               COUNT(*)                                   AS total,
               SUM(CASE WHEN a.status="Present" THEN 1 ELSE 0 END) AS present,
               SUM(CASE WHEN a.status="Absent"  THEN 1 ELSE 0 END) AS absent
        FROM attendance a
        JOIN sessions s ON s.id = a.session_id
        WHERE a.student_id = ?
        GROUP BY s.subject
        ORDER BY s.subject
    ''', (sid,)).fetchall()

    result = []
    for r in rows:
        total   = r['total']
        present = r['present']
        pct     = round(present / total * 100, 1) if total else 0
        result.append({
            'subject': r['subject'],
            'total':   total,
            'present': present,
            'absent':  r['absent'],
            'rate':    pct,
            'status':  'Good' if pct >= 75 else ('Warning' if pct >= 50 else 'Danger'),
        })
    return jsonify(result)

@app.route('/api/my/sessions')
@require_student_token
def my_sessions():
    """Full session history for logged-in student."""
    sid     = request.headers.get('X-Student-ID')
    subject = request.args.get('subject', '')
    db      = get_db()

    sql = '''
        SELECT s.subject, s.semester, s.started_at,
               a.status, a.confidence, a.marked_at
        FROM attendance a
        JOIN sessions s ON s.id = a.session_id
        WHERE a.student_id = ?
    '''
    params = [sid]
    if subject:
        sql    += ' AND s.subject = ?'
        params.append(subject)
    sql += ' ORDER BY s.started_at DESC'

    rows = db.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/my/subjects')
@require_student_token
def my_subjects():
    """List of subjects the student has attendance records for."""
    sid = request.headers.get('X-Student-ID')
    db  = get_db()
    rows = db.execute('''
        SELECT DISTINCT s.subject
        FROM attendance a
        JOIN sessions s ON s.id = a.session_id
        WHERE a.student_id = ?
        ORDER BY s.subject
    ''', (sid,)).fetchall()
    return jsonify([r['subject'] for r in rows])

@app.route('/api/my/profile')
@require_student_token
def my_profile():
    sid = request.headers.get('X-Student-ID')
    db  = get_db()
    row = db.execute('SELECT * FROM students WHERE student_id=?', (sid,)).fetchone()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(dict(row))

# ── Sync endpoint (professor → cloud, requires sync token) ────────

@app.route('/api/sync', methods=['POST'])
@require_sync_token
def sync():
    """
    Receive a full sync payload from the professor's desktop app.
    Payload shape:
    {
      "students":   [ {student_id, name, gmail, enrolled_at}, ... ],
      "subjects":   [ {name, semester}, ... ],
      "sessions":   [ {id, subject, semester, started_at, ended_at}, ... ],
      "attendance": [ {session_id, student_id, status, confidence, marked_at}, ... ]
    }
    All tables are upserted (INSERT OR REPLACE).
    """
    data = request.get_json(force=True)
    db   = get_db()

    students   = data.get('students',   [])
    subjects   = data.get('subjects',   [])
    sessions   = data.get('sessions',   [])
    attendance = data.get('attendance', [])

    for s in students:
        db.execute('''
            INSERT OR REPLACE INTO students (student_id, name, gmail, enrolled_at)
            VALUES (?,?,?,?)
        ''', (s['student_id'], s['name'], s.get('gmail',''), s.get('enrolled_at','')))

    for s in subjects:
        db.execute('''
            INSERT OR IGNORE INTO subjects (name, semester)
            VALUES (?,?)
        ''', (s['name'], s.get('semester','')))

    synced_at = datetime.now().isoformat()
    for s in sessions:
        db.execute('''
            INSERT OR REPLACE INTO sessions (id, subject, semester, started_at, ended_at, synced_at)
            VALUES (?,?,?,?,?,?)
        ''', (s['id'], s['subject'], s.get('semester',''),
              s.get('started_at',''), s.get('ended_at',''), synced_at))

    for a in attendance:
        db.execute('''
            INSERT OR REPLACE INTO attendance
                (session_id, student_id, status, confidence, marked_at)
            VALUES (?,?,?,?,?)
        ''', (a['session_id'], a['student_id'], a.get('status','Absent'),
              a.get('confidence'), a.get('marked_at','')))

    db.commit()
    return jsonify({
        'ok': True,
        'synced_at': synced_at,
        'counts': {
            'students':   len(students),
            'subjects':   len(subjects),
            'sessions':   len(sessions),
            'attendance': len(attendance),
        }
    })

@app.route('/api/sync/session', methods=['POST'])
@require_sync_token
def sync_session():
    """
    Push a single session's data immediately after it ends.
    Lighter than full sync — just one session + its attendance rows.
    """
    data       = request.get_json(force=True)
    db         = get_db()
    session    = data.get('session', {})
    attendance = data.get('attendance', [])
    students   = data.get('students', [])

    # Upsert students (so new enrollees appear on web portal)
    for s in students:
        db.execute('''
            INSERT OR REPLACE INTO students (student_id, name, gmail, enrolled_at)
            VALUES (?,?,?,?)
        ''', (s['student_id'], s['name'], s.get('gmail',''), s.get('enrolled_at','')))

    synced_at = datetime.now().isoformat()
    db.execute('''
        INSERT OR REPLACE INTO sessions (id, subject, semester, started_at, ended_at, synced_at)
        VALUES (?,?,?,?,?,?)
    ''', (session['id'], session['subject'], session.get('semester',''),
          session.get('started_at',''), session.get('ended_at',''), synced_at))

    for a in attendance:
        db.execute('''
            INSERT OR REPLACE INTO attendance
                (session_id, student_id, status, confidence, marked_at)
            VALUES (?,?,?,?,?)
        ''', (session['id'], a['student_id'], a.get('status','Absent'),
              a.get('confidence'), a.get('marked_at','')))

    db.commit()
    return jsonify({'ok': True, 'synced_at': synced_at,
                    'session_id': session['id'],
                    'attendance_count': len(attendance)})

# ── Professor endpoints ───────────────────────────────────────────

@app.route('/api/prof/login', methods=['POST'])
def prof_login():
    """Professor login — returns a session token."""
    data = request.get_json(force=True)
    pwd  = str(data.get('password', '')).strip()
    if pwd != PROF_PASSWORD:
        return jsonify({'error': 'Incorrect password'}), 401
    return jsonify({'token': make_prof_token()})

@app.route('/api/prof/overview')
@require_prof_token
def prof_overview():
    """High-level stats for professor dashboard."""
    db = get_db()
    total_students = db.execute('SELECT COUNT(*) FROM students').fetchone()[0]
    total_sessions = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
    total_records  = db.execute('SELECT COUNT(*) FROM attendance').fetchone()[0]
    present_count  = db.execute(
        "SELECT COUNT(*) FROM attendance WHERE status='Present'"
    ).fetchone()[0]
    avg_rate = round(present_count / total_records * 100, 1) if total_records else 0

    subjects = db.execute('''
        SELECT s.subject,
               COUNT(DISTINCT s.id) AS sessions,
               COUNT(a.id) AS total_records,
               SUM(CASE WHEN a.status="Present" THEN 1 ELSE 0 END) AS present
        FROM sessions s
        LEFT JOIN attendance a ON a.session_id = s.id
        GROUP BY s.subject
        ORDER BY s.subject
    ''').fetchall()

    subj_list = []
    for r in subjects:
        total = r['total_records'] or 0
        pres  = r['present'] or 0
        subj_list.append({
            'subject':  r['subject'],
            'sessions': r['sessions'],
            'rate':     round(pres / total * 100, 1) if total else 0,
        })

    return jsonify({
        'total_students': total_students,
        'total_sessions': total_sessions,
        'total_records':  total_records,
        'avg_rate':       avg_rate,
        'subjects':       subj_list,
    })

@app.route('/api/prof/students')
@require_prof_token
def prof_students():
    """Per-student attendance summary, optionally filtered by subject."""
    subject = request.args.get('subject', '')
    db = get_db()

    sql = '''
        SELECT st.student_id, st.name,
               s.subject,
               COUNT(a.id) AS total,
               SUM(CASE WHEN a.status="Present" THEN 1 ELSE 0 END) AS present
        FROM students st
        LEFT JOIN attendance a ON a.student_id = st.student_id
        LEFT JOIN sessions s ON s.id = a.session_id
        {where}
        GROUP BY st.student_id, s.subject
        ORDER BY st.name, s.subject
    '''
    if subject:
        rows = db.execute(
            sql.format(where='WHERE s.subject = ?'), (subject,)
        ).fetchall()
    else:
        rows = db.execute(sql.format(where='')).fetchall()

    result = []
    for r in rows:
        total   = r['total'] or 0
        present = r['present'] or 0
        result.append({
            'student_id': r['student_id'],
            'name':       r['name'],
            'subject':    r['subject'],
            'total':      total,
            'present':    present,
            'absent':     total - present,
            'rate':       round(present / total * 100, 1) if total else 0,
        })
    return jsonify(result)

@app.route('/api/prof/sessions')
@require_prof_token
def prof_sessions():
    """All sessions with present/absent counts, optionally filtered by subject."""
    subject = request.args.get('subject', '')
    db = get_db()

    sql = '''
        SELECT s.id, s.subject, s.semester, s.started_at,
               COUNT(a.id) AS total,
               SUM(CASE WHEN a.status="Present" THEN 1 ELSE 0 END) AS present
        FROM sessions s
        LEFT JOIN attendance a ON a.session_id = s.id
        {where}
        GROUP BY s.id
        ORDER BY s.started_at DESC
    '''
    if subject:
        rows = db.execute(
            sql.format(where='WHERE s.subject = ?'), (subject,)
        ).fetchall()
    else:
        rows = db.execute(sql.format(where='')).fetchall()

    result = []
    for r in rows:
        total   = r['total'] or 0
        present = r['present'] or 0
        result.append({
            'id':         r['id'],
            'subject':    r['subject'],
            'semester':   r['semester'] or '',
            'started_at': r['started_at'] or '',
            'total':      total,
            'present':    present,
            'absent':     total - present,
            'rate':       round(present / total * 100, 1) if total else 0,
        })
    return jsonify(result)

@app.route('/api/prof/subjects')
@require_prof_token
def prof_subjects():
    """List all subjects that have sessions."""
    db = get_db()
    rows = db.execute(
        'SELECT DISTINCT subject FROM sessions ORDER BY subject'
    ).fetchall()
    return jsonify([r['subject'] for r in rows])

# ── Health check ──────────────────────────────────────────────────

@app.route('/api/health')
def health():
    db  = get_db()
    cnt = db.execute('SELECT COUNT(*) FROM students').fetchone()[0]
    return jsonify({'status': 'ok', 'students': cnt,
                    'time': datetime.now().isoformat()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
