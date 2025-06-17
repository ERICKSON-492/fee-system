import os
import time
import psycopg2
from psycopg2 import pool, extras
from datetime import datetime, timedelta
from flask import request
from flask_paginate import Pagination, get_page_args
import secrets
from flask import Flask, render_template, request, redirect, url_for, flash, make_response, session, jsonify
from weasyprint import HTML
from decimal import Decimal, InvalidOperation
import qrcode
import base64
from io import BytesIO
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import sys
import atexit
import logging

# Load environment variables
load_dotenv()

# Configuration
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection pool
db_pool = None

def init_db_pool():
    global db_pool
    max_retries = 3
    retry_delay = 2
    
    for attempt in range(max_retries):
        try:
            DATABASE_URL = os.getenv('DATABASE_URL')
            if not DATABASE_URL:
                raise ValueError("DATABASE_URL environment variable is required")
                
            if DATABASE_URL.startswith('postgres://'):
                DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
                
            db_pool = pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=DATABASE_URL,
                sslmode='require'
            )
            logger.info("✅ Database connection established")
            init_db()
            return
        except Exception as e:
            logger.error(f"❌ Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                logger.error("❌ Failed to connect to database after multiple attempts")
                sys.exit(1)

def get_logo_base64():
    try:
        logo_path = os.path.join(app.static_folder, 'images', 'LOGO.jpg')
        with open(logo_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Error loading logo: {str(e)}")
        return None

@atexit.register
def shutdown_db_pool():
    global db_pool
    if db_pool:
        db_pool.closeall()
        logger.info("Database connection pool closed")

@contextmanager
def get_db_connection():
    if db_pool is None:
        raise RuntimeError("Database connection pool not initialized")
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)

@contextmanager
def get_db_cursor(dict_cursor=False, commit=False):
    if not db_pool:
        raise RuntimeError("Database connection pool not initialized")
        
    conn = db_pool.getconn()
    try:
        if dict_cursor:
            cur = conn.cursor(cursor_factory=extras.DictCursor)
        else:
            cur = conn.cursor()
        
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            cur.close()
    finally:
        db_pool.putconn(conn)

def init_db():
    """Initialize database tables"""
    with get_db_cursor(commit=True) as cur:
        # Create users table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create students table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                admission_no TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                form TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create terms table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS terms (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                amount DECIMAL(10,2) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create payments table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                amount_paid DECIMAL(10,2) NOT NULL,
                payment_date DATE NOT NULL,
                receipt_number TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create student balances table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS student_balances (
                student_id INTEGER PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
                current_balance DECIMAL(10,2) NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create indexes for better performance
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_payments_student_id ON payments(student_id)
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_payments_term_id ON payments(term_id)
        ''')
        cur.execute('''
            CREATE INDEX IF NOT EXISTS idx_payments_receipt_number ON payments(receipt_number)
        ''')
        
        # Create admin user if not exists
        cur.execute("SELECT 1 FROM users WHERE username = 'admin'")
        if not cur.fetchone():
            hashed_password = generate_password_hash('admin')
            cur.execute('''
                INSERT INTO users (username, password)
                VALUES (%s, %s)
            ''', ('admin', hashed_password))

# Initialize the connection pool
init_db_pool()

# Helper functions
def get_students():
    """Fetch all students with admission numbers"""
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute("""
                SELECT id, name, admission_no 
                FROM students 
                ORDER BY name
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error fetching students: {str(e)}")
        flash('Error loading student list', 'danger')
        return []

def get_terms():
    """Fetch all available terms"""
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute("""
                SELECT id, name, amount
                FROM terms
                ORDER BY name
            """)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error fetching terms: {str(e)}")
        flash('Error loading term list', 'danger')
        return []

# Error handlers
@app.errorhandler(psycopg2.OperationalError)
def handle_db_error(e):
    logger.error(f"Database operational error: {str(e)}")
    flash('Database connection error. Please try again.', 'danger')
    return redirect(url_for('dashboard'))

@app.errorhandler(500)
def handle_server_error(e):
    logger.error(f"Server error: {str(e)}")
    flash('An internal server error occurred', 'danger')
    return redirect(url_for('dashboard'))

# Health check endpoint
@app.route('/health')
def health_check():
    try:
        with get_db_cursor() as cur:
            cur.execute('SELECT 1')
            return jsonify({'status': 'healthy', 'database': 'connected'})
    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({'status': 'unhealthy', 'database': str(e)}), 500

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Authentication routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        try:
            with get_db_cursor(dict_cursor=True) as cur:
                cur.execute('SELECT * FROM users WHERE username = %s', (username,))
                user = cur.fetchone()
                
                if user and check_password_hash(user['password'], password):
                    session['user_id'] = user['id']
                    session['username'] = user['username']
                    flash('Login successful!', 'success')
                    return redirect(url_for('dashboard'))
                else:
                    flash('Invalid username or password', 'danger')
        except Exception as e:
            logger.error(f"Login error: {str(e)}")
            flash('Database error during login', 'danger')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))

# Dashboard
@app.route('/')
@login_required
def dashboard():
    return redirect(url_for('view_students'))

# Student management
@app.route('/students')
@login_required
def view_students():
    search = request.args.get('search', '')
    query = 'SELECT * FROM students'
    params = []
    
    if search:
        query += ' WHERE admission_no ILIKE %s OR name ILIKE %s'
        params.extend([f'%{search}%', f'%{search}%'])
    
    query += ' ORDER BY name'
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute(query, params)
            students = cur.fetchall()
    except Exception as e:
        logger.error(f"Error in view_students: {str(e)}")
        flash('Error retrieving students', 'danger')
        students = []
    
    return render_template('students.html', students=students, search=search)

@app.route('/student/add', methods=['GET', 'POST'])
@login_required
def add_student():
    if request.method == 'POST':
        admission_no = request.form['admission_no'].strip()
        name = request.form['name'].strip()
        form = request.form['form'].strip()
        
        if not admission_no or not name or not form:
            flash('All fields are required', 'danger')
            return render_template('add_student.html')
        
        try:
            with get_db_cursor(commit=True) as cur:
                cur.execute(
                    'INSERT INTO students (admission_no, name, form) VALUES (%s, %s, %s)',
                    (admission_no, name, form)
                )
                flash('Student added successfully!', 'success')
                return redirect(url_for('view_students'))
        except psycopg2.IntegrityError:
            flash('Admission number must be unique!', 'danger')
        except Exception as e:
            logger.error(f"Error in add_student: {str(e)}")
            flash(f'Error adding student: {str(e)}', 'danger')
    
    return render_template('add_student.html')

@app.route('/student/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_student(id):
    if request.method == 'POST':
        name = request.form['name'].strip()
        form = request.form['form'].strip()
        
        if not name or not form:
            flash('All fields are required', 'danger')
            return redirect(url_for('edit_student', id=id))
        
        try:
            with get_db_cursor(commit=True) as cur:
                cur.execute(
                    'UPDATE students SET name = %s, form = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
                    (name, form, id)
                )
                flash('Student updated successfully!', 'success')
                return redirect(url_for('view_students'))
        except Exception as e:
            logger.error(f"Error in edit_student: {str(e)}")
            flash(f'Error updating student: {str(e)}', 'danger')
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('SELECT * FROM students WHERE id = %s', (id,))
            student = cur.fetchone()
    except Exception as e:
        logger.error(f"Error retrieving student: {str(e)}")
        flash('Error retrieving student', 'danger')
        return redirect(url_for('view_students'))
    
    if not student:
        flash('Student not found', 'danger')
        return redirect(url_for('view_students'))
    
    return render_template('edit_student.html', student=student)

@app.route('/student/delete/<int:id>', methods=['POST'])
@login_required
def delete_student(id):
    try:
        with get_db_cursor(commit=True) as cur:
            cur.execute('DELETE FROM students WHERE id = %s', (id,))
            flash('Student deleted successfully!', 'success')
    except Exception as e:
        logger.error(f"Error in delete_student: {str(e)}")
        flash(f'Error deleting student: {str(e)}', 'danger')
    
    return redirect(url_for('view_students'))

# Term management
@app.route('/terms')
@login_required
def view_terms():
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('SELECT * FROM terms ORDER BY name')
            terms = cur.fetchall()
    except Exception as e:
        logger.error(f"Error in view_terms: {str(e)}")
        flash('Error retrieving terms', 'danger')
        terms = []
    
    return render_template('terms.html', terms=terms)

@app.route('/term/add', methods=['GET', 'POST'])
@login_required
def add_term():
    if request.method == 'POST':
        name = request.form['name'].strip()
        amount = request.form['amount'].strip()
        
        if not name or not amount:
            flash('All fields are required', 'danger')
            return render_template('add_term.html')
        
        try:
            amount = Decimal(amount)
            if amount <= 0:
                flash('Amount must be positive', 'danger')
                return render_template('add_term.html')
                
            with get_db_cursor(commit=True) as cur:
                cur.execute(
                    'INSERT INTO terms (name, amount) VALUES (%s, %s)',
                    (name, amount)
                )
                flash('Term added successfully!', 'success')
                return redirect(url_for('view_terms'))
        except (ValueError, InvalidOperation):
            flash('Amount must be a valid number', 'danger')
        except psycopg2.IntegrityError:
            flash('Term name must be unique!', 'danger')
        except Exception as e:
            logger.error(f"Error in add_term: {str(e)}")
            flash(f'Error adding term: {str(e)}', 'danger')
    
    return render_template('add_term.html')

@app.route('/term/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_term(id):
    if request.method == 'POST':
        name = request.form['name'].strip()
        amount = request.form['amount'].strip()
        
        if not name or not amount:
            flash('All fields are required', 'danger')
            return redirect(url_for('edit_term', id=id))
        
        try:
            amount = Decimal(amount)
            if amount <= 0:
                flash('Amount must be positive', 'danger')
                return redirect(url_for('edit_term', id=id))
                
            with get_db_cursor(commit=True) as cur:
                cur.execute(
                    'UPDATE terms SET name = %s, amount = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s',
                    (name, amount, id)
                )
                flash('Term updated successfully!', 'success')
                return redirect(url_for('view_terms'))
        except (ValueError, InvalidOperation):
            flash('Amount must be a valid number', 'danger')
        except psycopg2.IntegrityError:
            flash('Term name must be unique!', 'danger')
        except Exception as e:
            logger.error(f"Error updating term: {str(e)}")
            flash(f'Error updating term: {str(e)}', 'danger')
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('SELECT * FROM terms WHERE id = %s', (id,))
            term = cur.fetchone()
    except Exception as e:
        logger.error(f"Error retrieving term: {str(e)}")
        flash('Error retrieving term', 'danger')
        return redirect(url_for('view_terms'))
    
    if not term:
        flash('Term not found', 'danger')
        return redirect(url_for('view_terms'))
    
    return render_template('edit_term.html', term=term)

@app.route('/term/delete/<int:id>', methods=['POST'])
@login_required
def delete_term(id):
    try:
        with get_db_cursor(commit=True) as cur:
            cur.execute('DELETE FROM terms WHERE id = %s', (id,))
            flash('Term deleted successfully!', 'success')
    except Exception as e:
        logger.error(f"Error in delete_term: {str(e)}")
        flash(f'Error deleting term: {str(e)}', 'danger')
    
    return redirect(url_for('view_terms'))

# Payment management
@app.route('/payments')
@login_required
def view_payments():
    search = request.args.get('search', '').strip()
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            query = '''
                SELECT p.id, p.receipt_number, p.amount_paid, p.payment_date,
                       s.id as student_id, s.name as student_name, s.admission_no,
                       t.id as term_id, t.name as term_name
                FROM payments p
                JOIN students s ON p.student_id = s.id
                JOIN terms t ON p.term_id = t.id
            '''
            
            params = []
            if search:
                query += " WHERE s.admission_no ILIKE %s OR s.name ILIKE %s OR p.receipt_number ILIKE %s"
                params = [f'%{search}%', f'%{search}%', f'%{search}%']
            
            query += " ORDER BY p.payment_date DESC"
            cur.execute(query, params)
            payments = cur.fetchall()
            
            for payment in payments:
                payment['amount_paid'] = float(payment['amount_paid']) if payment['amount_paid'] is not None else 0.0
            
            cur.execute("SELECT id, name, admission_no FROM students ORDER BY name")
            students = cur.fetchall()
            
            cur.execute("SELECT id, name FROM terms ORDER BY name")
            terms = cur.fetchall()
            
        return render_template('payments.html',
                            payments=payments,
                            students=students,
                            terms=terms,
                            search=search)
            
    except Exception as e:
        logger.error(f"Error in view_payments: {str(e)}")
        flash('Error retrieving payment records', 'danger')
        return redirect(url_for('dashboard'))

@app.route('/payment/add', methods=['GET', 'POST'])
@login_required
def add_payment():
    if request.method == 'POST':
        try:
            student_id = request.form['student_id']
            term_id = request.form['term_id']
            amount_paid = Decimal(request.form['amount_paid'])
            payment_date = request.form.get('payment_date') or datetime.now().date()
            
            if amount_paid <= 0:
                flash('Payment amount must be positive', 'danger')
                return redirect(url_for('add_payment'))
            
            with get_db_cursor(commit=True) as cur:
                # Get term amount
                cur.execute('SELECT amount FROM terms WHERE id = %s', (term_id,))
                term_amount = Decimal(str(cur.fetchone()[0]))
                
                # Get current balance
                cur.execute('''
                    SELECT current_balance FROM student_balances 
                    WHERE student_id = %s
                ''', (student_id,))
                balance_row = cur.fetchone()
                current_balance = Decimal(str(balance_row[0])) if balance_row else Decimal('0')
                
                # Calculate new balance
                new_balance = current_balance + amount_paid - term_amount
                
                # Update balance record
                if balance_row:
                    cur.execute('''
                        UPDATE student_balances 
                        SET current_balance = %s,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE student_id = %s
                    ''', (new_balance, student_id))
                else:
                    cur.execute('''
                        INSERT INTO student_balances (student_id, current_balance)
                        VALUES (%s, %s)
                    ''', (student_id, new_balance))
                
                # Record payment
                receipt_number = f"RCPT-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                cur.execute('''
                    INSERT INTO payments 
                    (student_id, term_id, amount_paid, payment_date, receipt_number)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (student_id, term_id, amount_paid, payment_date, receipt_number))
                
                # Return appropriate message
                if new_balance >= 0:
                    flash(f'Payment recorded! {amount_paid:.2f} paid. New balance: {new_balance:.2f} (credit)', 'success')
                else:
                    flash(f'Payment recorded! {amount_paid:.2f} paid. Balance due: {-new_balance:.2f}', 'success')
                    
                return redirect(url_for('view_payments'))
                
        except (ValueError, InvalidOperation):
            flash('Amount must be a valid number', 'danger')
        except Exception as e:
            logger.error(f"Payment error: {str(e)}")
            flash('Error processing payment', 'danger')
    
    students = get_students()
    terms = get_terms()
    return render_template('add_payment.html', students=students, terms=terms)

@app.route('/student/add-from-payment', methods=['GET', 'POST'])
@login_required
def add_student_from_payment():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        admission_no = request.form.get('admission_no', '').strip().upper()
        form = request.form.get('form', '').strip()
        
        if not name or not admission_no or not form:
            flash('All fields are required', 'danger')
            return render_template('add_student_from_payment.html',
                                admission_no=admission_no,
                                name=name,
                                form=form)
        
        try:
            with get_db_cursor(commit=True) as cur:
                cur.execute(
                    'INSERT INTO students (admission_no, name, form) VALUES (%s, %s, %s) RETURNING id',
                    (admission_no, name, form)
                )
                student_id = cur.fetchone()[0]
                
                if 'pending_payment' in session:
                    payment_data = session.pop('pending_payment')
                    amount_paid = Decimal(payment_data['amount_paid'])
                    
                    cur.execute('''
                        INSERT INTO payments 
                        (student_id, term_id, amount_paid, payment_date, receipt_number)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id
                    ''', (student_id, 
                          payment_data['term_id'], 
                          amount_paid,
                          payment_data['payment_date'], 
                          payment_data['receipt_number']))
                    
                    payment_id = cur.fetchone()[0]
                    flash('Student and payment recorded successfully!', 'success')
                    return redirect(url_for('view_receipt', payment_id=payment_id))
                
        except psycopg2.IntegrityError:
            flash('Admission number already exists!', 'danger')
        except Exception as e:
            logger.error(f"Error creating student: {str(e)}")
            flash(f'Error creating student: {str(e)}', 'danger')
    
    payment_data = session.get('pending_payment', {})
    return render_template('add_student_from_payment.html',
                         admission_no=payment_data.get('admission_no', ''),
                         default_form='Form 1')

@app.route('/payment/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_payment(id):
    if request.method == 'POST':
        student_id = request.form['student_id'].strip()
        term_id = request.form['term_id'].strip()
        amount_paid = request.form['amount_paid'].strip()
        payment_date = request.form['payment_date'].strip()
        
        try:
            amount_paid = Decimal(amount_paid)
            if amount_paid <= 0:
                flash('Amount must be positive', 'danger')
                return redirect(url_for('edit_payment', id=id))
                
            with get_db_cursor(commit=True) as cur:
                cur.execute('''
                    UPDATE payments 
                    SET student_id = %s, term_id = %s, 
                        amount_paid = %s, payment_date = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                ''', (student_id, term_id, amount_paid, payment_date, id))
                
                flash('Payment updated successfully!', 'success')
                return redirect(url_for('view_payments'))
        except (ValueError, InvalidOperation):
            flash('Amount must be a valid number', 'danger')
        except Exception as e:
            logger.error(f"Error in edit_payment: {str(e)}")
            flash(f'Error updating payment: {str(e)}', 'danger')
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('''
                SELECT p.id, p.student_id, p.term_id, p.amount_paid, p.payment_date,
                       s.name AS student_name, s.admission_no,
                       t.name AS term_name
                FROM payments p
                JOIN students s ON p.student_id = s.id
                JOIN terms t ON p.term_id = t.id
                WHERE p.id = %s
            ''', (id,))
            payment = cur.fetchone()
            
            cur.execute('SELECT id, name FROM students ORDER BY name')
            students = cur.fetchall()
            
            cur.execute('SELECT id, name FROM terms ORDER BY name')
            terms = cur.fetchall()
    except Exception as e:
        logger.error(f"Error retrieving payment: {str(e)}")
        flash('Error retrieving payment', 'danger')
        return redirect(url_for('view_payments'))
    
    if not payment:
        flash('Payment not found', 'danger')
        return redirect(url_for('view_payments'))
    
    return render_template('edit_payment.html',
                         payment=payment,
                         students=students,
                         terms=terms)

@app.route('/payment/delete/<int:id>', methods=['POST'])
@login_required
def delete_payment(id):
    try:
        with get_db_cursor(commit=True) as cur:
            cur.execute('DELETE FROM payments WHERE id = %s', (id,))
            flash('Payment deleted successfully!', 'success')
    except Exception as e:
        logger.error(f"Error in delete_payment: {str(e)}")
        flash(f'Error deleting payment: {str(e)}', 'danger')
    
    return redirect(url_for('view_payments'))

@app.route('/receipt/<int:payment_id>')
@login_required
def view_receipt(payment_id):
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get payment details with student and term information
            cur.execute('''
                SELECT 
                    p.id, 
                    p.student_id, 
                    p.term_id, 
                    p.amount_paid, 
                    p.payment_date, 
                    p.receipt_number,
                    s.name AS student_name, 
                    s.admission_no, 
                    s.form,
                    t.name AS term_name, 
                    t.amount AS term_amount
                FROM payments p
                JOIN students s ON p.student_id = s.id
                JOIN terms t ON p.term_id = t.id
                WHERE p.id = %s
            ''', (payment_id,))
            payment = cur.fetchone()
            
            if not payment:
                flash('Receipt not found', 'danger')
                return redirect(url_for('view_payments'))

            # Convert to Decimal with proper null handling
            term_amount = Decimal(str(payment.get('term_amount', 0)))
            amount_paid = Decimal(str(payment.get('amount_paid', 0)))
            
            # Safely get payment date
            payment_date = payment.get('payment_date')
            if payment_date:
                formatted_date = payment_date.strftime('%d/%m/%Y')
            else:
                formatted_date = 'N/A'
            
            # Calculate total payments for this term with null handling
            cur.execute('''
                SELECT COALESCE(SUM(amount_paid), 0) AS total_paid
                FROM payments
                WHERE student_id = %s AND term_id = %s
            ''', (payment.get('student_id'), payment.get('term_id')))
            term_total_paid = Decimal(str(cur.fetchone().get('total_paid', 0)))
            
            # Calculate balances with proper null handling
            term_balance = term_amount - term_total_paid
            
            # Calculate cumulative balance with proper null handling
            cur.execute('''
                SELECT 
                    COALESCE(SUM(t.amount), 0) AS total_due,
                    COALESCE(SUM(p.amount_paid), 0) AS total_paid
                FROM students s
                CROSS JOIN terms t
                LEFT JOIN payments p ON s.id = p.student_id AND t.id = p.term_id
                WHERE s.id = %s
                GROUP BY s.id
            ''', (payment.get('student_id'),))
            result = cur.fetchone()
            
            cumulative_balance = Decimal('0')
            if result:
                total_due = Decimal(str(result.get('total_due', 0)))
                total_paid = Decimal(str(result.get('total_paid', 0)))
                cumulative_balance = total_paid - total_due
            
            # Generate QR code with all null checks
            qr_data = f"""
            Receipt: {payment.get('receipt_number', 'N/A')}
            Student: {payment.get('student_name', 'N/A')} ({payment.get('admission_no', 'N/A')})
            Amount Paid: {float(amount_paid):.2f}
            Term: {payment.get('term_name', 'N/A')}
            Term Balance: {"-" if term_balance > 0 else ""}{float(abs(term_balance)):.2f}
            Cumulative Balance: {"+" if cumulative_balance > 0 else ""}{float(abs(cumulative_balance)):.2f}
            Date: {formatted_date}
            """
            
            try:
                qr_img = qrcode.make(qr_data)
                qr_buffer = BytesIO()
                qr_img.save(qr_buffer, format="PNG")
                qr_b64 = base64.b64encode(qr_buffer.getvalue()).decode('utf-8')
            except Exception as qr_error:
                logger.error(f"QR generation error: {str(qr_error)}")
                qr_b64 = None
            
            logo_base64 = get_logo_base64() or None
            
            return render_template('receipt.html',
                                payment=payment,
                                amount_paid=float(amount_paid),
                                term_total_paid=float(term_total_paid),
                                term_balance=float(term_balance),
                                cumulative_balance=float(cumulative_balance),
                                current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                qr_code=qr_b64,
                                logo_base64=logo_base64,
                                formatted_date=formatted_date)
            
    except Exception as e:
        logger.error(f"Error in view_receipt: {str(e)}", exc_info=True)
        flash('Error generating receipt', 'danger')
        return redirect(url_for('view_payments'))

@app.route('/reports/outstanding')
@login_required
def outstanding_report():
    current_term_id = None
    student_balances = {}
    total_outstanding = Decimal('0')
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get most recent term by creation date
            cur.execute('''
                SELECT id FROM terms 
                ORDER BY created_at DESC 
                LIMIT 1
            ''')
            current_term = cur.fetchone()
            current_term_id = current_term['id'] if current_term else None
            
            # Get outstanding balances with term breakdown
            cur.execute('''
                SELECT 
                    s.id, 
                    s.name, 
                    s.admission_no,
                    t.name AS term_name,
                    t.id AS term_id,
                    t.amount AS term_fee,
                    COALESCE(SUM(p.amount_paid), 0) AS term_paid,
                    (t.amount - COALESCE(SUM(p.amount_paid), 0)) AS term_balance,
                    (SELECT SUM(amount) FROM terms) AS total_fees,
                    (SELECT COALESCE(SUM(amount_paid), 0) FROM payments WHERE student_id = s.id) AS total_paid,
                    ((SELECT SUM(amount) FROM terms) - 
                     (SELECT COALESCE(SUM(amount_paid), 0) FROM payments WHERE student_id = s.id)) AS total_balance
                FROM students s
                CROSS JOIN terms t
                LEFT JOIN payments p ON s.id = p.student_id AND t.id = p.term_id
                GROUP BY s.id, t.id, t.name, t.amount
                HAVING (t.amount - COALESCE(SUM(p.amount_paid), 0)) > 0
                ORDER BY 
                    CASE WHEN t.id = %s THEN 0 ELSE 1 END,
                    t.created_at DESC,
                    (t.amount - COALESCE(SUM(p.amount_paid), 0)) DESC
            ''', (current_term_id,))
            
            report_data = cur.fetchall()
            
            # Group by student for summary view
            student_balances = {}
            for row in report_data:
                if row['id'] not in student_balances:
                    student_balances[row['id']] = {
                        'id': row['id'],
                        'name': row['name'],
                        'admission_no': row['admission_no'],
                        'total_balance': row['total_balance'],
                        'terms': []
                    }
                    total_outstanding += Decimal(str(row['total_balance']))
                
                student_balances[row['id']]['terms'].append({
                    'term_name': row['term_name'],
                    'term_id': row['term_id'],
                    'term_fee': row['term_fee'],
                    'term_paid': row['term_paid'],
                    'term_balance': row['term_balance']
                })
            
            current_datetime = datetime.now()
            
    except Exception as e:
        logger.error(f"Error in outstanding_report: {str(e)}")
        flash('Error generating report: ' + str(e), 'danger')
        current_datetime = datetime.now()
    
    return render_template('outstanding_report.html', 
                         student_balances=student_balances,
                         current_term_id=current_term_id,
                         total_outstanding=total_outstanding,
                         current_datetime=current_datetime)

@app.route('/receipt/<int:payment_id>/pdf')
@login_required
def generate_receipt_pdf(payment_id):
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('''
                SELECT p.id, p.student_id, p.amount_paid, p.payment_date, p.receipt_number,
                       s.name AS student_name, s.admission_no, s.form,
                       t.name AS term_name, t.amount AS term_amount
                FROM payments p
                JOIN students s ON p.student_id = s.id
                JOIN terms t ON p.term_id = t.id
                WHERE p.id = %s
            ''', (payment_id,))
            payment = cur.fetchone()
            
            if not payment:
                flash('Receipt not found', 'danger')
                return redirect(url_for('view_payments'))
            
            term_amount = float(payment['term_amount'])
            amount_paid = float(payment['amount_paid'])
            
            cur.execute('''
                SELECT COALESCE(SUM(amount_paid), 0)::float
                FROM payments
                WHERE student_id = %s
            ''', (payment['student_id'],))
            total_paid = float(cur.fetchone()[0])
            
            outstanding_balance = term_amount - total_paid
            
            qr_data = f"""
            Receipt: {payment['receipt_number']}
            Student: {payment['student_name']} ({payment['admission_no']})
            Amount: {amount_paid:.2f}
            Date: {payment['payment_date']}
            """
            qr_img = qrcode.make(qr_data)
            qr_buffer = BytesIO()
            qr_img.save(qr_buffer, format="PNG")
            qr_b64 = base64.b64encode(qr_buffer.getvalue()).decode('utf-8')
            
            logo_base64 = get_logo_base64()
            
            html = render_template('receipt_pdf.html',
                                payment=payment,
                                total_paid=total_paid,
                                outstanding_balance=outstanding_balance,
                                current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                qr_code=qr_b64,
                                logo_base64=logo_base64)
            
            pdf_bytes = HTML(string=html).write_pdf()
            
            response = make_response(pdf_bytes)
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = f'inline; filename=receipt_{payment["receipt_number"]}.pdf'
            return response
            
    except Exception as e:
        flash('Error generating PDF receipt', 'danger')
        print(f"Error in generate_receipt_pdf: {str(e)}")
        return redirect(url_for('view_payments'))

@app.route('/reports/outstanding/pdf')
@login_required
def outstanding_report_pdf():
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('''
                SELECT s.id, s.name, s.admission_no,
                       SUM(t.amount)::float AS total_due,
                       COALESCE(SUM(p.amount_paid), 0)::float AS total_paid,
                       (SUM(t.amount) - COALESCE(SUM(p.amount_paid), 0))::float AS balance
                FROM students s
                CROSS JOIN terms t
                LEFT JOIN payments p ON s.id = p.student_id AND t.id = p.term_id
                GROUP BY s.id
                HAVING SUM(t.amount) - COALESCE(SUM(p.amount_paid), 0) > 0
                ORDER BY balance DESC
            ''')
            report_data = cur.fetchall()

            if not report_data:
                flash('No outstanding balances found', 'info')
                return redirect(url_for('outstanding_report'))

            logo_base64 = get_logo_base64()

            html = render_template('outstanding_report_pdf.html',
                                report_data=report_data,
                                current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                logo_base64=logo_base64)
            
            pdf_bytes = HTML(string=html).write_pdf()
            
            response = make_response(pdf_bytes)
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = 'inline; filename=outstanding_balances.pdf'
            return response
            
    except Exception as e:
        flash('Error generating PDF report', 'danger')
        print(f"Error in outstanding_report_pdf: {str(e)}")
        return redirect(url_for('outstanding_report'))  

@app.route('/receipt/outstanding/<int:student_id>')
@login_required
def outstanding_balance_receipt(student_id):
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('''
                SELECT s.id, s.name, s.admission_no, s.form,
                       SUM(t.amount)::float AS total_due,
                       COALESCE(SUM(p.amount_paid), 0)::float AS total_paid,
                       (SUM(t.amount) - COALESCE(SUM(p.amount_paid), 0))::float AS balance
                FROM students s
                CROSS JOIN terms t
                LEFT JOIN payments p ON s.id = p.student_id AND t.id = p.term_id
                WHERE s.id = %s
                GROUP BY s.id, s.name, s.admission_no, s.form
            ''', (student_id,))
            student = cur.fetchone()
            
            if not student:
                flash('Student not found', 'danger')
                return redirect(url_for('outstanding_report'))

            current_date = datetime.now().strftime('%d/%m/%Y')
            due_date = (datetime.now() + timedelta(days=14)).strftime('%d/%m/%Y')
            reference_no = f"BAL-{student['admission_no']}-{current_date.replace('/', '')}"

            qr_data = f"""Student: {student['name']}
Adm No: {student['admission_no']}
Balance: KSh{student['balance']:,.2f}
Date: {current_date}"""
            
            qr_img = qrcode.make(qr_data)
            qr_buffer = BytesIO()
            qr_img.save(qr_buffer, format="PNG")
            qr_b64 = base64.b64encode(qr_buffer.getvalue()).decode('utf-8')
            
            return render_template('outstanding_receipt.html',
                                student=student,
                                current_date=current_date,
                                due_date=due_date,
                                reference_no=reference_no,
                                qr_code=qr_b64,
                                logo_base64=get_logo_base64())
            
    except Exception as e:
        print(f"Error generating receipt: {str(e)}")
        flash('Error generating receipt. Please try again.', 'danger')
        return redirect(url_for('outstanding_report'))
if __name__ == '__main__':
    try:
        port = int(os.environ.get('FLASK_PORT', 5000))
        app.run(host='0.0.0.0', port=port, debug=True)
    except Exception as e:
        print(f"Failed to start application: {str(e)}")
        sys.exit(1)
