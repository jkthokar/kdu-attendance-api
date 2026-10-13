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
SECRET_KEY   = os.environ.get('SECRET_KEY', 'kdu-attendance-secret-change-me')
SYNC_TOKEN   = os.environ.get('SYNC_TOKEN', 'kdu-sync-token-change-me')
DB_PATH      = os.environ.get('DB_PATH', 'attendance_cloud.db')

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
    return app.send_static_file('index.html')

@app.route('/api/login', methods=['POST'])
def login():
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

# ── Student endpoints ─────────────────────────────────────────────

@app.route('/api/my/summary')
@require_student_token
def my_summary():
    sid = request.headers.get('X-Student-ID')
    db  = get_db()
    rows = db.execute('''
        SELECT s.subject,
               COUNT(*) AS total,
               SUM(CASE WHEN a.status="Present" THEN 1 ELSE 0 END) AS present,
               SUM(CASE WHEN a.status="Absent"  THEN 1 ELSE 0 END) AS absent
        FROM attendance a
        JOIN sessions s ON s.id = a.session_id
        WHERE a.student_id = ?
        GROUP BY s.subject ORDER BY s.subject
    ''', (sid,)).fetchall()
    result = []
    for r in rows:
        total   = r['total']
        present = r['present']
        pct     = round(present / total * 100, 1) if total else 0
        result.append({
            'subject': r['subject'], 'total': total,
            'present': present, 'absent': r['absent'], 'rate': pct,
            'status': 'Good' if pct >= 75 else ('Warning' if pct >= 50 else 'Danger'),
        })
    return jsonify(result)

@app.route('/api/my/sessions')
@require_student_token
def my_sessions():
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
        sql += ' AND s.subject = ?'
        params.append(subject)
    sql += ' ORDER BY s.started_at DESC'
    rows = db.execute(sql, params).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/my/subjects')
@require_student_token
def my_subjects():
    sid = request.headers.get('X-Student-ID')
    db  = get_db()
    rows = db.execute('''
        SELECT DISTINCT s.subject FROM attendance a
        JOIN sessions s ON s.id = a.session_id
        WHERE a.student_id = ? ORDER BY s.subject
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

# ── Sync endpoints ────────────────────────────────────────────────

@app.route('/api/sync', methods=['POST'])
@require_sync_token
def sync():
    data = request.get_json(force=True)
    db   = get_db()
    students   = data.get('students',   [])
    subjects   = data.get('subjects',   [])
    sessions   = data.get('sessions',   [])
    attendance = data.get('attendance', [])
    for s in students:
        db.execute('INSERT OR REPLACE INTO students (student_id,name,gmail,enrolled_at) VALUES (?,?,?,?)',
                   (s['student_id'],s['name'],s.get('gmail',''),s.get('enrolled_at','')))
    for s in subjects:
        db.execute('INSERT OR IGNORE INTO subjects (name,semester) VALUES (?,?)',
                   (s['name'],s.get('semester','')))
    synced_at = datetime.now().isoformat()
    for s in sessions:
        db.execute('INSERT OR REPLACE INTO sessions (id,subject,semester,started_at,ended_at,synced_at) VALUES (?,?,?,?,?,?)',
                   (s['id'],s['subject'],s.get('semester',''),s.get('started_at',''),s.get('ended_at',''),synced_at))
    for a in attendance:
        db.execute('INSERT OR REPLACE INTO attendance (session_id,student_id,status,confidence,marked_at) VALUES (?,?,?,?,?)',
                   (a['session_id'],a['student_id'],a.get('status','Absent'),a.get('confidence'),a.get('marked_at','')))
    db.commit()
    return jsonify({'ok':True,'synced_at':synced_at,'counts':{'students':len(students),'subjects':len(subjects),'sessions':len(sessions),'attendance':len(attendance)}})

@app.route('/api/sync/session', methods=['POST'])
@require_sync_token
def sync_session():
    data       = request.get_json(force=True)
    db         = get_db()
    session    = data.get('session', {})
    attendance = data.get('attendance', [])
    students   = data.get('students', [])
    for s in students:
        db.execute('INSERT OR REPLACE INTO students (student_id,name,gmail,enrolled_at) VALUES (?,?,?,?)',
                   (s['student_id'],s['name'],s.get('gmail',''),s.get('enrolled_at','')))
    synced_at = datetime.now().isoformat()
    db.execute('INSERT OR REPLACE INTO sessions (id,subject,semester,started_at,ended_at,synced_at) VALUES (?,?,?,?,?,?)',
               (session['id'],session['subject'],session.get('semester',''),session.get('started_at',''),session.get('ended_at',''),synced_at))
    for a in attendance:
        db.execute('INSERT OR REPLACE INTO attendance (session_id,student_id,status,confidence,marked_at) VALUES (?,?,?,?,?)',
                   (session['id'],a['student_id'],a.get('status','Absent'),a.get('confidence'),a.get('marked_at','')))
    db.commit()
    return jsonify({'ok':True,'synced_at':synced_at,'session_id':session['id'],'attendance_count':len(attendance)})

# ── Health check ──────────────────────────────────────────────────

@app.route('/api/health')
def health():
    db  = get_db()
    cnt = db.execute('SELECT COUNT(*) FROM students').fetchone()[0]
    return jsonify({'status':'ok','students':cnt,'time':datetime.now().isoformat()})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
