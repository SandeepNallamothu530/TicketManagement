# app.py - Main Flask application entry point (UPDATED WITH EMAIL NOTIFICATIONS)
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

# Email configuration (Gmail)
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_USER = os.environ.get('GMAIL_USER', 'your-email@gmail.com')  # Set environment variable
EMAIL_PASS = os.environ.get('GMAIL_APP_PASSWORD', 'your-app-password')  # Use App Password for Gmail
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin@company.com')

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
    """Generate unique ticket number: TK-YYYYMMDD-XXXX"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today = datetime.now().strftime('%Y%m%d')
    c.execute("SELECT COUNT(*) FROM tickets WHERE ticket_number LIKE ?", (f'TK-{today}-%',))
    count = c.fetchone()[0] + 1
    conn.close()
    return f"TK-{today}-{count:04d}"

def send_email_notification(ticket_data, attachment_path=None):
    """Send email notifications to admin and user"""
    try:
        # Email to user (acknowledgment)
        user_msg = MIMEMultipart()
        user_msg['From'] = EMAIL_USER
        user_msg['To'] = ticket_data['email']
        user_msg['Subject'] = f"Ticket #{ticket_data['ticket_number']} Received - {ticket_data['subject']}"
        
        user_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>Ticket Acknowledgment</h2>
            <p>Dear {ticket_data['name']},</p>
            <p>Your ticket has been successfully submitted and is being processed.</p>
            <p><strong>Ticket Number:</strong> {ticket_data['ticket_number']}<br>
            <strong>Subject:</strong> {ticket_data['subject']}<br>
            <strong>Priority:</strong> {ticket_data['priority']}<br>
            <strong>Location:</strong> {ticket_data.get('location', '')}<br>
            <strong>Category:</strong> {ticket_data['category']}<br>
            <strong>Ticket Type:</strong> {ticket_data.get('subcategory', '')}</p>
            <p>We will update you on the status shortly.</p>
            <hr>
            <p style="font-size: 12px; color: #666;">This is an automated message. Please do not reply.</p>
        </body>
        </html>
        """
        user_msg.attach(MIMEText(user_body, 'html'))
        
        # Email to admin
        admin_msg = MIMEMultipart()
        admin_msg['From'] = EMAIL_USER
        admin_msg['To'] = ADMIN_EMAIL
        admin_msg['Subject'] = f"New Ticket #{ticket_data['ticket_number']} - {ticket_data['priority']} Priority"
        
        admin_body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>New Ticket Submitted</h2>
            <p><strong>Ticket Number:</strong> {ticket_data['ticket_number']}<br>
            <strong>Submitted By:</strong> {ticket_data['name']} ({ticket_data['email']})<br>
            <strong>Location:</strong> {ticket_data.get('location', '')}<br>
            <strong>Category:</strong> {ticket_data['category']}<br>
            <strong>Ticket Type:</strong> {ticket_data.get('subcategory', '')}<br>
            <strong>Priority:</strong> {ticket_data['priority']}<br>
            <strong>Subject:</strong> {ticket_data['subject']}</p>
            <p><strong>Description:</strong><br>{ticket_data['description']}</p>
            <p><a href="http://localhost:5000/admin/ticket/{ticket_data['id']}" style="background: #0066CC; color: white; padding: 10px 15px; text-decoration: none;">View Ticket</a></p>
        </body>
        </html>
        """
        admin_msg.attach(MIMEText(admin_body, 'html'))
        
        # Attach file if provided
        if attachment_path and os.path.exists(attachment_path):
            for msg in [user_msg, admin_msg]:
                with open(attachment_path, 'rb') as f:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(f.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename={os.path.basename(attachment_path)}')
                    msg.attach(part)
        
        # Send emails
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(user_msg)
            server.send_message(admin_msg)
        
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

def send_comment_email(ticket_data, comment_text, comment_author):
    """Send email notification to user when a comment is added"""
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_USER
        msg['To'] = ticket_data['email']
        msg['Subject'] = f"Update on Ticket #{ticket_data['ticket_number']} - {ticket_data['subject']}"
        
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>Ticket Update Notification</h2>
            <p>Dear {ticket_data['name']},</p>
            <p>There has been an update to your ticket <strong>#{ticket_data['ticket_number']}</strong>.</p>
            
            <div style="background: #f8f9fa; padding: 15px; border-left: 4px solid #667eea; margin: 15px 0;">
                <p><strong>Update from {comment_author}:</strong></p>
                <p style="color: #333;">{comment_text}</p>
            </div>
            
            <p><strong>Current Status:</strong> {ticket_data['status']}</p>
            <p>You can reply to this email to add more information, or log in to the portal to track your ticket.</p>
            
            <hr>
            <p style="font-size: 12px; color: #666;">This is an automated notification. Please do not reply directly to this email.</p>
        </body>
        </html>
        """
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        
        return True
    except Exception as e:
        print(f"Comment email error: {e}")
        return False

def send_status_update_email(ticket_data, old_status, new_status):
    """Send email notification to user when status changes"""
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_USER
        msg['To'] = ticket_data['email']
        msg['Subject'] = f"Status Update for Ticket #{ticket_data['ticket_number']} - {ticket_data['subject']}"
        
        # Color code based on new status
        status_color = {
            'Open': '#ffc107',
            'In Progress': '#17a2b8',
            'Resolved': '#28a745',
            'Closed': '#6c757d'
        }.get(new_status, '#667eea')
        
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>Ticket Status Updated</h2>
            <p>Dear {ticket_data['name']},</p>
            <p>The status of your ticket <strong>#{ticket_data['ticket_number']}</strong> has been updated.</p>
            
            <table style="width: 100%; max-width: 400px; margin: 20px 0; border-collapse: collapse;">
                <tr>
                    <td style="padding: 10px; background: #f8f9fa;"><strong>Previous Status:</strong></td>
                    <td style="padding: 10px;">{old_status}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; background: #f8f9fa;"><strong>New Status:</strong></td>
                    <td style="padding: 10px;"><span style="background: {status_color}; color: white; padding: 5px 10px; border-radius: 5px;">{new_status}</span></td>
                </tr>
                <tr>
                    <td style="padding: 10px; background: #f8f9fa;"><strong>Subject:</strong></td>
                    <td style="padding: 10px;">{ticket_data['subject']}</td>
                </tr>
            </table>
            
            <p><a href="http://localhost:5000/" style="background: #667eea; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">View Your Ticket</a></p>
            
            <hr>
            <p style="font-size: 12px; color: #666;">This is an automated notification. Please do not reply directly to this email.</p>
        </body>
        </html>
        """
        msg.attach(MIMEText(body, 'html'))
        
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.starttls()
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        
        return True
    except Exception as e:
        print(f"Status update email error: {e}")
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
        # Form fields coming from templates/index.html
        # UI "Sub-Category" is stored in DB as tickets.category
        # UI "Ticket Type" is stored in DB as tickets.subcategory
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
            errors.append('Sub-Category is required')
        if not subcategory:
            errors.append('Ticket Type is required')
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
        "avg_resolution_by_status": {}
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

    # Average resolution time by status (for tickets that have moved at least once in history)
    # We'll approximate resolution time as difference between first created_at and latest updated_at for that ticket
    c.execute(
        f"""
        SELECT status, AVG(
            CAST((JULIANDAY(updated_at) - JULIANDAY(created_at)) * 24.0 * 60.0 AS FLOAT)
        ) as avg_minutes
        FROM tickets
        {base_where}
        GROUP BY status
        """,
        filter_params
    )
    for row in c.fetchall():
        status = row[0]
        avg_minutes = row[1] or 0
        # convert to days with 1 decimal
        aggregates["avg_resolution_by_status"][status] = round(avg_minutes / 60.0 / 24.0, 1)

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
    c.execute("SELECT status, name, email, subject, ticket_number FROM tickets WHERE id = ?", (ticket_id,))
    result = c.fetchone()
    if not result:
        conn.close()
        return jsonify({'error': 'Ticket not found'}), 404
    
    old_status = result[0]
    
    # Check if ticket is already closed - prevent updates
    if old_status == 'Closed':
        conn.close()
        return jsonify({'error': 'Closed tickets cannot be modified'}), 403
    
    # Get ticket data for email
    ticket_data = {
        'name': result[1],
        'email': result[2],
        'subject': result[3],
        'ticket_number': result[4],
        'status': new_status
    }
    
    # Update ticket
    c.execute("UPDATE tickets SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", 
              (new_status, ticket_id))
    
    # Log status change
    c.execute("INSERT INTO status_history (ticket_id, old_status, new_status, changed_by) VALUES (?, ?, ?, ?)",
              (ticket_id, old_status, new_status, session['username']))
    
    conn.commit()
    conn.close()
    
    # Send email notification to user about status change
    send_status_update_email(ticket_data, old_status, new_status)
    
    return jsonify({'success': True, 'message': 'Status updated and user notified'})

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
    c.execute("SELECT status, name, email, subject, ticket_number FROM tickets WHERE id = ?", (ticket_id,))
    result = c.fetchone()
    if not result:
        conn.close()
        return jsonify({'error': 'Ticket not found'}), 404
    
    if result[0] == 'Closed':
        conn.close()
        return jsonify({'error': 'Cannot add comments to closed tickets'}), 403
    
    # Get ticket data for email
    ticket_data = {
        'name': result[1],
        'email': result[2],
        'subject': result[3],
        'ticket_number': result[4],
        'status': result[0]
    }
    
    # Insert comment
    c.execute("INSERT INTO comments (ticket_id, comment, created_by) VALUES (?, ?, ?)",
              (ticket_id, comment_text, session['username']))
    c.execute("UPDATE tickets SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (ticket_id,))
    conn.commit()
    comment_id = c.lastrowid
    conn.close()
    
    # Send email notification to user about the comment
    send_comment_email(ticket_data, comment_text, session['username'])
    
    return jsonify({
        'success': True,
        'message': 'Comment added and user notified',
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
