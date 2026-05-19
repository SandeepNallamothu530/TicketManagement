# app.py - Main Flask application entry point (FIXED & IMPROVED)
import os
import sqlite3
import smtplib
import hashlib
import secrets
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory, abort, Response

# Configuration
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / 'static' / 'uploads'
DB_PATH = BASE_DIR / 'tickets.db'
SECRET_KEY = secrets.token_hex(16)
SESSION_TYPE = 'filesystem'

# Ensure upload directory exists
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['UPLOAD_FOLDER'] = str(UPLOAD_FOLDER)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf', 'txt', 'doc', 'docx', 'xlsx', 'zip'}

# Email configuration (Gmail) - Set these as environment variables
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_USER = os.environ.get('GMAIL_USER', '')  # Set environment variable
EMAIL_PASS = os.environ.get('GMAIL_APP_PASSWORD', '')  # Use App Password for Gmail
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', '')

# Database initialization
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Tickets table
    c.execute('''CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_number TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        email TEXT NOT NULL,
        location TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL,
        subcategory TEXT NOT NULL DEFAULT '',
        priority TEXT NOT NULL,
        subject TEXT NOT NULL,
        description TEXT NOT NULL,
        attachment_filename TEXT,
        attachment_original_name TEXT,
        status TEXT DEFAULT 'Open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Basic migration for existing DBs (add columns if missing)
    c.execute("PRAGMA table_info(tickets)")
    existing_cols = {row[1] for row in c.fetchall()}

    if "location" not in existing_cols:
        c.execute("ALTER TABLE tickets ADD COLUMN location TEXT NOT NULL DEFAULT ''")
    if "subcategory" not in existing_cols:
        c.execute("ALTER TABLE tickets ADD COLUMN subcategory TEXT NOT NULL DEFAULT ''")
    
    # Comments table for audit trail
    c.execute('''CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        comment TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticket_id) REFERENCES tickets (id)
    )''')
    
    # Status history table for audit
    c.execute('''CREATE TABLE IF NOT EXISTS status_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id INTEGER NOT NULL,
        old_status TEXT,
        new_status TEXT NOT NULL,
        changed_by TEXT NOT NULL,
        changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (ticket_id) REFERENCES tickets (id)
    )''')
    
    # Check if admin exists in a simple users table (for future expansion)
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'admin'
    )''')
    
    # Insert default admin if not exists
    admin_hash = hashlib.sha256('admin123'.encode()).hexdigest()
    c.execute("SELECT id FROM users WHERE username = 'admin'")
    if not c.fetchone():
        c.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                  ('admin', admin_hash, 'admin'))
    
    conn.commit()
    conn.close()

init_db()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def generate_ticket_number():
    """Generate unique ticket number: NTT-YYYYMMDD-XXXX"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now().strftime('%Y%m%d')
    c.execute("SELECT COUNT(*) FROM tickets WHERE ticket_number LIKE ?", (f'NTT-{today}-%',))
    count = c.fetchone()[0] + 1
    conn.close()
    return f"NTT-{today}-{count:04d}"

def send_email_notification(ticket_data, attachment_path=None):
    """Send email notifications to admin and user"""
    if not EMAIL_USER or not EMAIL_PASS:
        print("Email not configured. Skipping email notification.")
        return False
    
    try:
        # Email to user (acknowledgment)
        user_msg = MIMEMultipart()
        user_msg['From'] = EMAIL_USER
        user_msg['To'] = ticket_data['email']
        user_msg['Subject'] = f"[NTT Data] Ticket #{ticket_data['ticket_number']} - Acknowledgment"
        
        user_body = f"""
        <html>
        <body style="font-family: 'Segoe UI', Arial, sans-serif;">
            <div style="max-width: 600px; margin: 0 auto; background: #f8f9fa;">
                <div style="background: linear-gradient(135deg, #004d40 0%, #00695c 100%); padding: 20px; text-align: center;">
                    <h2 style="color: white; margin: 0;">NTT Data Service Desk</h2>
                </div>
                <div style="padding: 30px;">
                    <h3 style="color: #004d40;">Ticket Acknowledgment</h3>
                    <p>Dear {ticket_data['name']},</p>
                    <p>Your ticket has been successfully submitted and is being processed.</p>
                    <div style="background: #e8f5e9; padding: 15px; border-radius: 8px; margin: 15px 0;">
                        <p style="margin: 5px 0;"><strong>Ticket Number:</strong> {ticket_data['ticket_number']}</p>
                        <p style="margin: 5px 0;"><strong>Subject:</strong> {ticket_data['subject']}</p>
                        <p style="margin: 5px 0;"><strong>Priority:</strong> {ticket_data['priority']}</p>
                        <p style="margin: 5px 0;"><strong>Location:</strong> {ticket_data.get('location', '')}</p>
                    </div>
                    <p>We will update you on the status shortly.</p>
                    <hr style="margin: 20px 0;">
                    <p style="font-size: 12px; color: #666;">This is an automated message. Please do not reply.</p>
                </div>
            </div>
        </body>
        </html>
        """
        user_msg.attach(MIMEText(user_body, 'html'))
        
        # Email to admin
        if ADMIN_EMAIL:
            admin_msg = MIMEMultipart()
            admin_msg['From'] = EMAIL_USER
            admin_msg['To'] = ADMIN_EMAIL
            admin_msg['Subject'] = f"[NTT Data] New Ticket #{ticket_data['ticket_number']} - {ticket_data['priority']} Priority"
            
            admin_body = f"""
            <html>
            <body style="font-family: 'Segoe UI', Arial, sans-serif;">
                <div style="max-width: 600px; margin: 0 auto;">
                    <div style="background: linear-gradient(135deg, #004d40 0%, #00695c 100%); padding: 20px; text-align: center;">
                        <h2 style="color: white; margin: 0;">New Ticket Submitted</h2>
                    </div>
                    <div style="padding: 30px;">
                        <p><strong>Ticket Number:</strong> {ticket_data['ticket_number']}</p>
                        <p><strong>Submitted By:</strong> {ticket_data['name']} ({ticket_data['email']})</p>
                        <p><strong>Location:</strong> {ticket_data.get('location', '')}</p>
                        <p><strong>Priority:</strong> {ticket_data['priority']}</p>
                        <p><strong>Subject:</strong> {ticket_data['subject']}</p>
                        <p><strong>Description:</strong><br>{ticket_data['description']}</p>
                        <p><a href="http://localhost:5000/admin/ticket/{ticket_data['id']}" style="background: #004d40; color: white; padding: 10px 15px; text-decoration: none; border-radius: 5px;">View Ticket</a></p>
                    </div>
                </div>
            </body>
            </html>
            """
            admin_msg.attach(MIMEText(admin_body, 'html'))
            
            # Attach file to admin email if provided
            if attachment_path and os.path.exists(attachment_path):
                with open(attachment_path, 'rb') as f:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename={os.path.basename(attachment_path)}')
                    admin_msg.attach(part)
        
        # Send emails
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(user_msg)
            if ADMIN_EMAIL:
                server.send_message(admin_msg)
        
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            flash('Please login to access the admin panel', 'error')
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function

# Routes
@app.route('/templates/logos/<path:filename>')
def templates_logos(filename):
    """Serve logo assets stored under templates/logos (requested by user)."""
    logos_dir = BASE_DIR / 'templates' / 'logos'
    return send_from_directory(logos_dir, filename)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/submit-ticket', methods=['POST'])
def submit_ticket():
    if request.method == 'POST':
        # Form validation
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        location = request.form.get('location', '').strip()
        category = request.form.get('category', '').strip()
        subcategory = request.form.get('subcategory', '').strip()
        priority = request.form.get('priority')
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        
        # Validation
        errors = []
        if not name or len(name) < 2:
            errors.append('Name is required and must be at least 2 characters')
        if not email or '@' not in email or '.' not in email:
            errors.append('Valid email is required')
        if not location:
            errors.append('Location is required')
        if not category:
            errors.append('Category is required')
        if not subcategory:
            errors.append('Sub-category is required')
        if not priority:
            errors.append('Priority is required')
        if not subject or len(subject) < 3:
            errors.append('Subject is required and must be at least 3 characters')
        if not description or len(description) < 10:
            errors.append('Description is required and must be at least 10 characters')
        
        if errors:
            flash(' | '.join(errors), 'error')
            return redirect(url_for('index'))
        
        # Handle file upload
        attachment_filename = None
        attachment_original_name = None
        file = request.files.get('attachment')
        if file and file.filename:
            if allowed_file(file.filename):
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                safe_filename = f"{timestamp}_{secrets.token_hex(4)}_{file.filename}"
                filepath = UPLOAD_FOLDER / safe_filename
                file.save(filepath)
                attachment_filename = safe_filename
                attachment_original_name = file.filename
            else:
                flash('File type not allowed', 'error')
                return redirect(url_for('index'))
        
        # Create ticket
        ticket_number = generate_ticket_number()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT INTO tickets (ticket_number, name, email, location, category, subcategory, priority, subject, description,
                     attachment_filename, attachment_original_name, status)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (ticket_number, name, email, location, category, subcategory, priority, subject, description,
                   attachment_filename, attachment_original_name, 'Open'))
        ticket_id = c.lastrowid
        conn.commit()
        conn.close()
        
        # Send email notifications
        ticket_data = {
            'id': ticket_id,
            'ticket_number': ticket_number,
            'name': name,
            'email': email,
            'location': location,
            'category': category,
            'subcategory': subcategory,
            'priority': priority,
            'subject': subject,
            'description': description
        }
        attachment_path = UPLOAD_FOLDER / attachment_filename if attachment_filename else None
        send_email_notification(ticket_data, attachment_path)
        
        flash(f'Ticket #{ticket_number} submitted successfully! A confirmation email has been sent.', 'success')
        return redirect(url_for('index'))

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('logged_in'):
        return redirect(url_for('admin_dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, username, role FROM users WHERE username = ? AND password_hash = ?", 
                  (username, password_hash))
        user = c.fetchone()
        conn.close()
        
        if user:
            session['logged_in'] = True
            session['username'] = username
            session['role'] = user[2]
            flash('Login successful!', 'success')
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid credentials', 'error')
    
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('admin_login'))

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    return render_template('admin_dashboard.html')

@app.route('/admin/api/tickets')
@login_required
def api_tickets():
    """API endpoint for tickets with filtering, pagination and aggregates for charts"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    status_filter = request.args.get('status', '')
    category_filter = request.args.get('category', '')
    priority_filter = request.args.get('priority', '')
    search = request.args.get('search', '')

    offset = (page - 1) * per_page

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Build query for tickets (with filters)
    base_where = "WHERE 1=1"
    filter_clauses = []
    filter_params = []

    if status_filter:
        filter_clauses.append("status = ?")
        filter_params.append(status_filter)
    if category_filter:
        filter_clauses.append("category = ?")
        filter_params.append(category_filter)
    if priority_filter:
        filter_clauses.append("priority = ?")
        filter_params.append(priority_filter)
    if search:
        filter_clauses.append("(ticket_number LIKE ? OR name LIKE ? OR email LIKE ? OR subject LIKE ?)")
        search_term = f"%{search}%"
        filter_params.extend([search_term, search_term, search_term, search_term])

    if filter_clauses:
        base_where += " AND " + " AND ".join(filter_clauses)

    query = f"SELECT * FROM tickets {base_where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params = list(filter_params) + [per_page, offset]

    c.execute(query, params)
    tickets = [dict(row) for row in c.fetchall()]

    # Count for pagination
    count_query = f"SELECT COUNT(*) FROM tickets {base_where}"
    c.execute(count_query, filter_params)
    total = c.fetchone()[0]

    # Global statistics (without filters for headline cards)
    c.execute("SELECT COUNT(*) as total FROM tickets")
    total_tickets = c.fetchone()[0]
    c.execute("SELECT COUNT(*) as open FROM tickets WHERE status IN ('Open', 'In Progress')")
    open_tickets = c.fetchone()[0]
    c.execute("SELECT COUNT(*) as resolved FROM tickets WHERE status = 'Resolved'")
    resolved_tickets = c.fetchone()[0]

    # Aggregates for charts (respecting current filters)
    aggregates = {
        "tickets_over_time": [],
        "by_status": {},
        "by_category": {},
        "by_priority": {},
        "avg_resolution_by_status": {},
        "by_location": {},
        "by_weekday": {},
    }

    # Tickets over time (daily counts)
    c.execute(
        f"""
        SELECT DATE(created_at) as day, COUNT(*) as cnt
        FROM tickets
        {base_where}
        GROUP BY DATE(created_at)
        ORDER BY DATE(created_at)
        """,
        filter_params
    )
    for row in c.fetchall():
        aggregates["tickets_over_time"].append({
            "date": row[0],
            "count": row[1]
        })

    # By status
    c.execute(
        f"SELECT status, COUNT(*) as cnt FROM tickets {base_where} GROUP BY status",
        filter_params
    )
    for row in c.fetchall():
        aggregates["by_status"][row[0]] = row[1]

    # By category
    c.execute(
        f"SELECT category, COUNT(*) as cnt FROM tickets {base_where} GROUP BY category",
        filter_params
    )
    for row in c.fetchall():
        aggregates["by_category"][row[0]] = row[1]

    # By priority
    c.execute(
        f"SELECT priority, COUNT(*) as cnt FROM tickets {base_where} GROUP BY priority",
        filter_params
    )
    for row in c.fetchall():
        aggregates["by_priority"][row[0]] = row[1]

    # By location
    c.execute(
        f"SELECT location, COUNT(*) as cnt FROM tickets {base_where} GROUP BY location",
        filter_params
    )
    for row in c.fetchall():
        aggregates["by_location"][row[0] or ""] = row[1]

    # By weekday (0=Sunday..6=Saturday)
    c.execute(
        f"""
        SELECT STRFTIME('%%w', created_at) as wd, COUNT(*) as cnt
        FROM tickets
        {base_where}
        GROUP BY STRFTIME('%%w', created_at)
        ORDER BY CAST(STRFTIME('%%w', created_at) as INTEGER)
        """,
        filter_params
    )
    for row in c.fetchall():
        aggregates["by_weekday"][row[0]] = row[1]

    conn.close()

    return jsonify({
        'tickets': tickets,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page if total > 0 else 1,
        'stats': {
            'total': total_tickets,
            'open': open_tickets,
            'resolved': resolved_tickets
        },
        'aggregates': aggregates
    })

@app.route('/admin/ticket/<int:ticket_id>')
@login_required
def admin_ticket_detail(ticket_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
    ticket = c.fetchone()
    if not ticket:
        flash('Ticket not found', 'error')
        return redirect(url_for('admin_dashboard'))
    
    c.execute("SELECT * FROM comments WHERE ticket_id = ? ORDER BY created_at ASC", (ticket_id,))
    comments = [dict(row) for row in c.fetchall()]
    
    c.execute("SELECT * FROM status_history WHERE ticket_id = ? ORDER BY changed_at ASC", (ticket_id,))
    history = [dict(row) for row in c.fetchall()]
    conn.close()
    
    return render_template('admin_ticket_detail.html', ticket=dict(ticket), comments=comments, history=history)

@app.route('/admin/api/ticket/<int:ticket_id>/update', methods=['POST'])
@login_required
def update_ticket_status(ticket_id):
    data = request.get_json()
    new_status = data.get('status')
    
    if not new_status:
        return jsonify({'error': 'Status required'}), 400
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Get old status and ticket info
    c.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,))
    result = c.fetchone()
    if not result:
        conn.close()
        return jsonify({'error': 'Ticket not found'}), 404
    
    old_status = result[0]
    
    # Check if ticket is already closed - prevent updates
    if old_status == 'Closed':
        conn.close()
        return jsonify({'error': 'Closed tickets cannot be modified'}), 403
    
    # Update ticket
    c.execute("UPDATE tickets SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", 
              (new_status, ticket_id))
    
    # Log status change
    c.execute("INSERT INTO status_history (ticket_id, old_status, new_status, changed_by) VALUES (?, ?, ?, ?)",
              (ticket_id, old_status, new_status, session['username']))
    
    conn.commit()
    conn.close()
    
    return jsonify({'success': True, 'message': 'Status updated successfully'})

@app.route('/admin/api/ticket/<int:ticket_id>/comment', methods=['POST'])
@login_required
def add_comment(ticket_id):
    data = request.get_json()
    comment_text = data.get('comment', '').strip()
    
    if not comment_text:
        return jsonify({'error': 'Comment cannot be empty'}), 400
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Check if ticket is closed - prevent comments on closed tickets
    c.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,))
    result = c.fetchone()
    if not result:
        conn.close()
        return jsonify({'error': 'Ticket not found'}), 404
    
    if result[0] == 'Closed':
        conn.close()
        return jsonify({'error': 'Cannot add comments to closed tickets'}), 403
    
    # Insert comment
    c.execute("INSERT INTO comments (ticket_id, comment, created_by) VALUES (?, ?, ?)",
              (ticket_id, comment_text, session['username']))
    c.execute("UPDATE tickets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (ticket_id,))
    conn.commit()
    comment_id = c.lastrowid
    conn.close()
    
    return jsonify({
        'success': True,
        'message': 'Comment added successfully',
        'comment': {
            'id': comment_id,
            'comment': comment_text,
            'created_by': session['username'],
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
    })

@app.route('/admin/uploads/<filename>')
@login_required
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/admin/api/tickets/export')
@login_required
def export_tickets_csv():
    """Export ticket history as CSV, respecting current filters"""
    status_filter = request.args.get('status', '')
    category_filter = request.args.get('category', '')
    priority_filter = request.args.get('priority', '')
    search = request.args.get('search', '')

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    base_where = "WHERE 1=1"
    filter_clauses = []
    filter_params = []

    if status_filter:
        filter_clauses.append("status = ?")
        filter_params.append(status_filter)
    if category_filter:
        filter_clauses.append("category = ?")
        filter_params.append(category_filter)
    if priority_filter:
        filter_clauses.append("priority = ?")
        filter_params.append(priority_filter)
    if search:
        filter_clauses.append("(ticket_number LIKE ? OR name LIKE ? OR email LIKE ? OR subject LIKE ?)")
        search_term = f"%{search}%"
        filter_params.extend([search_term, search_term, search_term, search_term])

    if filter_clauses:
        base_where += " AND " + " AND ".join(filter_clauses)

    query = f"""
        SELECT
            ticket_number,
            name,
            email,
            location,
            category,
            subcategory,
            priority,
            subject,
            status,
            created_at,
            updated_at
        FROM tickets
        {base_where}
        ORDER BY created_at DESC
    """

    c.execute(query, filter_params)
    rows = c.fetchall()
    conn.close()

    # Build CSV in memory
    import csv
    from io import StringIO

    si = StringIO()
    writer = csv.writer(si)
    header = ["Ticket Number", "Name", "Email", "Location", "Category", "Ticket Type", "Priority", "Subject", "Status", "Created At", "Updated At"]
    writer.writerow(header)

    for r in rows:
        writer.writerow([
            r["ticket_number"],
            r["name"],
            r["email"],
            r["location"],
            r["category"],
            r["subcategory"],
            r["priority"],
            r["subject"],
            r["status"],
            r["created_at"],
            r["updated_at"],
        ])

    output = si.getvalue()
    si.close()

    filename = f"tickets_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.errorhandler(413)
def too_large(e):
    flash('File too large. Maximum size is 16MB.', 'error')
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)
