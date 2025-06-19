import os
import time
import psycopg2
from psycopg2 import pool, extras
from datetime import datetime, timedelta
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
import logging
import sys
import random  # Add this with your other imports
# Load environment variables
load_dotenv()

# Configuration
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'default-secret-key')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection pool
db_pool = None

def init_db_pool():
    global db_pool
    max_retries = 5
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
                raise

def get_logo_base64():
    try:
        logo_path = os.path.join(app.static_folder, 'images', 'LOGO.jpg')
        with open(logo_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        logger.error(f"Error loading logo: {str(e)}")
        return None

@contextmanager
def get_db_connection():
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)

@contextmanager
def get_db_cursor(dict_cursor=False, commit=False):
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
        
        # Create payment_allocations table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS payment_allocations (
                payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
                term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                amount DECIMAL(10,2) NOT NULL,
                PRIMARY KEY (payment_id, term_id)
            )
        ''')
        
        # Create term application order table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS term_application_order (
                term_id INTEGER PRIMARY KEY REFERENCES terms(id) ON DELETE CASCADE,
                application_order INTEGER NOT NULL
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
        
        # Create outstanding balance notices table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS outstanding_balance_notices (
                id SERIAL PRIMARY KEY,
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                amount DECIMAL(10,2) NOT NULL,
                issued_date DATE NOT NULL,
                due_date DATE NOT NULL,
                reference_number TEXT UNIQUE NOT NULL,
                is_paid BOOLEAN DEFAULT FALSE,
                paid_date DATE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create term_balances table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS term_balances (
                student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
                balance DECIMAL(10,2) NOT NULL,
                PRIMARY KEY (student_id, term_id)
            )
        ''')
        
        # Create indexes
        cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_student_id ON payments(student_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_term_id ON payments(term_id)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_payments_receipt_number ON payments(receipt_number)')
        
        # Create admin user if not exists
        cur.execute("SELECT 1 FROM users WHERE username = 'admin'")
        if not cur.fetchone():
            hashed_password = generate_password_hash('admin')
            cur.execute('INSERT INTO users (username, password) VALUES (%s, %s)', 
                       ('admin', hashed_password))
def calculate_term_balance(student_id, term_id):
    """Calculate and update balance for a specific term"""
    with get_db_cursor(commit=True) as cur:  # Added commit=True to ensure updates are saved
        try:
            # Get term amount
            cur.execute('SELECT amount FROM terms WHERE id = %s', (term_id,))
            term_result = cur.fetchone()
            term_amount = term_result[0] if term_result else Decimal('0')
            
            # Get total payments for this term
            cur.execute('''
                SELECT COALESCE(SUM(amount_paid), 0)
                FROM payments
                WHERE student_id = %s AND term_id = %s
            ''', (student_id, term_id))
            total_paid = cur.fetchone()[0] or Decimal('0')
            
            balance = term_amount - total_paid
            
            # Update term_balances table
            cur.execute('''
                INSERT INTO term_balances (student_id, term_id, balance)
                VALUES (%s, %s, %s)
                ON CONFLICT (student_id, term_id) DO UPDATE
                SET balance = EXCLUDED.balance
            ''', (student_id, term_id, balance))
            
            logger.info(f"Updated term balance - Student: {student_id}, Term: {term_id}, Balance: {balance}")
            return balance
            
        except Exception as e:
            logger.error(f"Error calculating term balance for student {student_id}, term {term_id}: {str(e)}")
            raise  # Re-raise the exception to be handled by the caller
def calculate_cumulative_balance(student_id):
    """Calculate and update total outstanding balance across all terms"""
    with get_db_cursor(commit=True) as cur:  # Added commit=True to ensure changes are saved
        try:
            # Get all terms with their amounts
            cur.execute('SELECT id, amount FROM terms ORDER BY id')
            terms = cur.fetchall()
            
            total_balance = Decimal('0')
            
            # Calculate balance for each term and update term_balances
            for term_id, term_amount in terms:
                # Get total payments for this term
                cur.execute('''
                    SELECT COALESCE(SUM(amount_paid), 0)
                    FROM payments
                    WHERE student_id = %s AND term_id = %s
                ''', (student_id, term_id))
                total_paid = cur.fetchone()[0] or Decimal('0')
                
                term_balance = term_amount - total_paid
                total_balance += term_balance
                
                # Update term-specific balance
                cur.execute('''
                    INSERT INTO term_balances (student_id, term_id, balance)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (student_id, term_id) DO UPDATE
                    SET balance = EXCLUDED.balance
                ''', (student_id, term_id, term_balance))
            
            # Update the student's overall balance
            cur.execute('''
                INSERT INTO student_balances (student_id, current_balance)
                VALUES (%s, %s)
                ON CONFLICT (student_id) DO UPDATE
                SET current_balance = EXCLUDED.current_balance,
                    updated_at = CURRENT_TIMESTAMP
            ''', (student_id, total_balance))
            
            logger.info(f"Updated cumulative balance for student {student_id}: {total_balance}")
            return total_balance
            
        except Exception as e:
            logger.error(f"Error calculating cumulative balance for student {student_id}: {str(e)}")
            raise  # Re-raise the exception to be handled by the caller
def generate_receipt_number():
    """Generate a unique receipt number"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return f"RCPT-{timestamp}"

# Initialize the connection pool
init_db_pool()

# Helper functions
def get_students():
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT id, name, admission_no FROM students ORDER BY name")
        return cur.fetchall()

def get_terms():
    with get_db_cursor(dict_cursor=True) as cur:
        cur.execute("SELECT id, name, amount FROM terms ORDER BY name")
        return cur.fetchall()

# Authentication
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

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

# Main routes
@app.route('/')
@login_required
def dashboard():
    return redirect(url_for('view_students'))

# Student management
@app.route('/students')
@login_required
def view_students():
    search = request.args.get('search', '')
    query = 'SELECT s.*, COALESCE(sb.current_balance, 0) AS balance FROM students s LEFT JOIN student_balances sb ON s.id = sb.student_id'
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
        
        # Validate inputs
        if not all([admission_no, name, form]):
            flash('All fields are required', 'danger')
            return render_template('add_student.html')
        
        if len(admission_no) > 20:
            flash('Admission number too long (max 20 characters)', 'danger')
            return render_template('add_student.html')
        
        try:
            with get_db_cursor(commit=True) as cur:
                # Check for duplicate admission number
                cur.execute('SELECT 1 FROM students WHERE admission_no = %s', (admission_no,))
                if cur.fetchone():
                    flash('Admission number already exists', 'danger')
                    return render_template('add_student.html')
                
                # Insert new student
                cur.execute('''
                    INSERT INTO students (admission_no, name, form) 
                    VALUES (%s, %s, %s) 
                    RETURNING id
                ''', (admission_no, name, form))
                student_id = cur.fetchone()[0]
                
                # Initialize balance
                cur.execute('''
                    INSERT INTO student_balances (student_id, current_balance)
                    VALUES (%s, 0)
                ''', (student_id,))
                
                flash('Student added successfully!', 'success')
                return redirect(url_for('view_students'))
                
        except psycopg2.Error as e:
            logger.error(f"Database error in add_student: {str(e)}")
            flash('Database error while adding student', 'danger')
        except Exception as e:
            logger.error(f"Unexpected error in add_student: {str(e)}")
            flash('Error adding student', 'danger')
    
    return render_template('add_student.html')


@app.route('/student/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_student(id):
    # Get existing student data
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('SELECT * FROM students WHERE id = %s', (id,))
            student = cur.fetchone()
            
            if not student:
                flash('Student not found', 'danger')
                return redirect(url_for('view_students'))
    except Exception as e:
        logger.error(f"Error retrieving student: {str(e)}")
        flash('Error retrieving student data', 'danger')
        return redirect(url_for('view_students'))

    if request.method == 'POST':
        name = request.form['name'].strip()
        form = request.form['form'].strip()
        
        if not all([name, form]):
            flash('All fields are required', 'danger')
            return redirect(url_for('edit_student', id=id))
        
        try:
            with get_db_cursor(commit=True) as cur:
                cur.execute('''
                    UPDATE students 
                    SET name = %s, form = %s, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = %s
                ''', (name, form, id))
                
                flash('Student updated successfully!', 'success')
                return redirect(url_for('view_students'))
                
        except Exception as e:
            logger.error(f"Error updating student: {str(e)}")
            flash('Error updating student', 'danger')
    
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
        
        # Validate inputs
        if not all([name, amount]):
            flash('All fields are required', 'danger')
            return render_template('add_term.html')
        
        try:
            amount = Decimal(amount)
            if amount <= 0:
                flash('Amount must be positive', 'danger')
                return render_template('add_term.html')
            if amount > Decimal('9999999.99'):
                flash('Amount is too large (max 9,999,999.99)', 'danger')
                return render_template('add_term.html')
                
            with get_db_cursor(commit=True) as cur:
                # Check for duplicate term name
                cur.execute('SELECT 1 FROM terms WHERE name = %s', (name,))
                if cur.fetchone():
                    flash('Term name already exists', 'danger')
                    return render_template('add_term.html')
                
                # Insert new term
                cur.execute('''
                    INSERT INTO terms (name, amount)
                    VALUES (%s, %s)
                ''', (name, amount))
                
                flash('Term added successfully!', 'success')
                return redirect(url_for('view_terms'))
                
        except (ValueError, InvalidOperation):
            flash('Amount must be a valid number', 'danger')
        except psycopg2.IntegrityError:
            flash('Term name must be unique', 'danger')
        except Exception as e:
            logger.error(f"Error adding term: {str(e)}")
            flash('Error adding term', 'danger')
    
    return render_template('add_term.html')


@app.route('/term/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_term(id):
    # Get existing term data
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('SELECT * FROM terms WHERE id = %s', (id,))
            term = cur.fetchone()
            
            if not term:
                flash('Term not found', 'danger')
                return redirect(url_for('view_terms'))
    except Exception as e:
        logger.error(f"Error retrieving term: {str(e)}")
        flash('Error retrieving term data', 'danger')
        return redirect(url_for('view_terms'))

    if request.method == 'POST':
        name = request.form['name'].strip()
        amount = request.form['amount'].strip()
        
        if not all([name, amount]):
            flash('All fields are required', 'danger')
            return redirect(url_for('edit_term', id=id))
        
        try:
            amount = Decimal(amount)
            if amount <= 0:
                flash('Amount must be positive', 'danger')
                return redirect(url_for('edit_term', id=id))
            if amount > Decimal('9999999.99'):
                flash('Amount is too large (max 9,999,999.99)', 'danger')
                return redirect(url_for('edit_term', id=id))
                
            with get_db_cursor(commit=True) as cur:
                # Check for duplicate term name (excluding current term)
                cur.execute('''
                    SELECT 1 FROM terms 
                    WHERE name = %s AND id != %s
                ''', (name, id))
                if cur.fetchone():
                    flash('Term name already exists', 'danger')
                    return redirect(url_for('edit_term', id=id))
                
                # Update term
                cur.execute('''
                    UPDATE terms 
                    SET name = %s, amount = %s, updated_at = CURRENT_TIMESTAMP 
                    WHERE id = %s
                ''', (name, amount, id))
                
                # Recalculate all student balances
                cur.execute('SELECT id FROM students')
                student_ids = [row[0] for row in cur.fetchall()]
                for student_id in student_ids:
                    calculate_student_balance(student_id)
                
                flash('Term updated successfully!', 'success')
                return redirect(url_for('view_terms'))
                
        except (ValueError, InvalidOperation):
            flash('Amount must be a valid number', 'danger')
        except psycopg2.IntegrityError:
            flash('Term name must be unique', 'danger')
        except Exception as e:
            logger.error(f"Error updating term: {str(e)}")
            flash('Error updating term', 'danger')
    
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
    search = request.args.get('search', '')
    query = '''
        SELECT p.id, p.amount_paid, p.payment_date, p.receipt_number,
               s.name AS student_name, s.admission_no,
               t.name AS term_name
        FROM payments p
        JOIN students s ON p.student_id = s.id
        JOIN terms t ON p.term_id = t.id
    '''
    params = []
    
    if search:
        query += ' WHERE s.admission_no ILIKE %s OR s.name ILIKE %s OR p.receipt_number ILIKE %s'
        params.extend([f'%{search}%', f'%{search}%', f'%{search}%'])
    
    query += ' ORDER BY p.payment_date DESC'
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute(query, params)
            payments = cur.fetchall()
    except Exception as e:
        logger.error(f"Error in view_payments: {str(e)}")
        flash('Error retrieving payments', 'danger')
        payments = []
    
    return render_template('payments.html', payments=payments, search=search)
@app.route('/payment/add', methods=['GET', 'POST'])
@login_required
def add_payment():
    # Get terms for dropdown
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('SELECT id, name, amount FROM terms ORDER BY name')
            terms = cur.fetchall()
    except Exception as e:
        logger.error(f"Error loading terms: {str(e)}")
        flash('Error loading payment form', 'danger')
        return redirect(url_for('view_payments'))

    if request.method == 'POST':
        student_identifier = request.form.get('student_identifier', '').strip()
        term_id = request.form.get('term_id')
        amount_paid = request.form.get('amount_paid')
        payment_date = request.form.get('payment_date')
        
        # Validate inputs
        if not all([student_identifier, term_id, amount_paid, payment_date]):
            flash('All fields are required', 'danger')
            return render_template('add_payment.html', 
                                terms=terms,
                                default_date=datetime.now().strftime('%Y-%m-%d'),
                                form_data=request.form)

        try:
            amount_paid = Decimal(amount_paid)
            payment_date = datetime.strptime(payment_date, '%Y-%m-%d').date()
            
            with get_db_cursor(commit=True) as cur:
                # Find student (using tuple access)
                cur.execute('''
                    SELECT id FROM students 
                    WHERE admission_no = %s OR name ILIKE %s
                    LIMIT 1
                ''', (student_identifier, f'%{student_identifier}%'))
                student = cur.fetchone()
                
                if not student:
                    flash('Student not found', 'danger')
                    return render_template('add_payment.html',
                                        terms=terms,
                                        default_date=datetime.now().strftime('%Y-%m-%d'),
                                        form_data=request.form)
                
                student_id = student[0]  # Access first element of tuple
                
                # Generate receipt number
                receipt_number = generate_receipt_number()
                
                # Insert payment
                cur.execute('''
                    INSERT INTO payments 
                    (student_id, term_id, amount_paid, payment_date, receipt_number)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (student_id, term_id, amount_paid, payment_date, receipt_number))
                
                # Update balances
                try:
                    term_balance = calculate_term_balance(student_id, term_id)
                    cumulative_balance = calculate_cumulative_balance(student_id)
                    flash(f'Payment recorded. Term balance: KSh {term_balance:,.2f}, Cumulative balance: KSh {cumulative_balance:,.2f}', 'success')
                except Exception as e:
                    logger.error(f"Balance update failed: {str(e)}")
                    flash('Payment recorded but balance update failed', 'warning')
                
                return redirect(url_for('view_payments'))
                
        except ValueError:
            flash('Invalid date or amount format', 'danger')
        except Exception as e:
            logger.error(f"Error adding payment: {str(e)}")
            flash('Error adding payment', 'danger')
    
    # GET request
    return render_template('add_payment.html', 
                         terms=terms,
                         default_date=datetime.now().strftime('%Y-%m-%d'))
@app.route('/student/<int:student_id>/balances')
@login_required
def view_student_balances(student_id):
    """View all balances for a student"""
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get student info
            cur.execute('SELECT id, name, admission_no FROM students WHERE id = %s', (student_id,))
            student = cur.fetchone()
            
            if not student:
                flash('Student not found', 'danger')
                return redirect(url_for('view_students'))
            
            # Get all terms with balances
            cur.execute('''
                SELECT t.id, t.name, t.amount, 
                       COALESCE(tb.balance, t.amount) as balance,
                       t.amount - COALESCE(tb.balance, t.amount) as paid
                FROM terms t
                LEFT JOIN term_balances tb ON t.id = tb.term_id AND tb.student_id = %s
                ORDER BY t.id
            ''', (student_id,))
            term_balances = cur.fetchall()
            
            # Get cumulative balance
            cumulative_balance = calculate_cumulative_balance(student_id)
            
            return render_template('student_balances.html',
                                student=student,
                                term_balances=term_balances,
                                cumulative_balance=cumulative_balance)
            
    except Exception as e:
        logger.error(f"Error viewing balances: {str(e)}")
        flash('Error retrieving balance information', 'danger')
        return redirect(url_for('view_students'))

@app.route('/student/<int:student_id>/term/<int:term_id>/balance')
@login_required
def view_term_balance(student_id, term_id):
    """View balance for a specific term"""
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get student and term info
            cur.execute('SELECT id, name FROM students WHERE id = %s', (student_id,))
            student = cur.fetchone()
            
            cur.execute('SELECT id, name, amount FROM terms WHERE id = %s', (term_id,))
            term = cur.fetchone()
            
            if not student or not term:
                flash('Student or term not found', 'danger')
                return redirect(url_for('view_students'))
            
            # Calculate term balance
            balance = calculate_term_balance(student_id, term_id)
            
            # Get payment history for this term
            cur.execute('''
                SELECT amount_paid, payment_date, receipt_number
                FROM payments
                WHERE student_id = %s AND term_id = %s
                ORDER BY payment_date DESC
            ''', (student_id, term_id))
            payments = cur.fetchall()
            
            return render_template('term_balance.html',
                                student=student,
                                term=term,
                                balance=balance,
                                payments=payments)
            
    except Exception as e:
        logger.error(f"Error viewing term balance: {str(e)}")
        flash('Error retrieving term balance', 'danger')
        return redirect(url_for('view_students'))
@app.route('/api/students/search')
@login_required
def search_students():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('''
                SELECT id, name, admission_no 
                FROM students 
                WHERE admission_no ILIKE %s OR name ILIKE %s
                ORDER BY name
                LIMIT 10
            ''', (f'%{query}%', f'%{query}%'))
            students = cur.fetchall()
            return jsonify(students)
    except Exception as e:
        logger.error(f"Error searching students: {str(e)}")
        return jsonify([])
@app.route('/payment/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_payment(id):
    if request.method == 'POST':
        student_id = request.form['student_id']
        term_id = request.form['term_id']
        amount_paid = request.form['amount_paid']
        payment_date = request.form['payment_date']
        
        try:
            amount_paid = Decimal(amount_paid)
            if amount_paid <= 0:
                flash('Amount must be positive', 'danger')
                return redirect(url_for('edit_payment', id=id))
                
            payment_date = datetime.strptime(payment_date, '%Y-%m-%d').date()
            
            with get_db_cursor(commit=True) as cur:
                # Get old student ID for balance recalculation
                cur.execute('SELECT student_id FROM payments WHERE id = %s', (id,))
                old_student_id = cur.fetchone()[0]
                
                # Update payment
                cur.execute('''
                    UPDATE payments 
                    SET student_id = %s, term_id = %s, 
                        amount_paid = %s, payment_date = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                ''', (student_id, term_id, amount_paid, payment_date, id))
                
                # Recalculate balances for both old and new student
                calculate_student_balance(old_student_id)
                if old_student_id != student_id:
                    calculate_student_balance(student_id)
                
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
            
            if not payment:
                flash('Payment not found', 'danger')
                return redirect(url_for('view_payments'))
            
            cur.execute('SELECT id, name FROM students ORDER BY name')
            students = cur.fetchall()
            
            cur.execute('SELECT id, name FROM terms ORDER BY name')
            terms = cur.fetchall()
            
            return render_template('edit_payment.html',
                                payment=payment,
                                students=students,
                                terms=terms,
                                formatted_date=payment['payment_date'].strftime('%Y-%m-%d'))
            
    except Exception as e:
        logger.error(f"Error retrieving payment: {str(e)}")
        flash('Error retrieving payment', 'danger')
        return redirect(url_for('view_payments'))

@app.route('/payment/delete/<int:id>', methods=['POST'])
@login_required
def delete_payment(id):
    try:
        with get_db_cursor(commit=True) as cur:
            # First check if the payment exists and get student_id
            cur.execute('SELECT student_id FROM payments WHERE id = %s', (id,))
            payment = cur.fetchone()
            
            if not payment:
                flash('Payment not found', 'danger')
                return redirect(url_for('view_payments'))
                
            # Access the student_id from the tuple (index 0)
            student_id = payment[0]
            
            # Delete payment allocations first
            try:
                cur.execute('DELETE FROM payment_allocations WHERE payment_id = %s', (id,))
            except Exception as e:
                logger.warning(f"Could not delete payment allocations: {str(e)}")
            
            # Delete the payment
            cur.execute('DELETE FROM payments WHERE id = %s', (id,))
            
            # Recalculate balances
            try:
                # Get all terms
                cur.execute('SELECT id FROM terms')
                terms = cur.fetchall()
                
                # Calculate balance for each term
                for term in terms:
                    term_id = term[0]  # Access term ID from tuple
                    
                    # Get total paid for this term
                    cur.execute('''
                        SELECT COALESCE(SUM(amount_paid), 0) 
                        FROM payments 
                        WHERE student_id = %s AND term_id = %s
                    ''', (student_id, term_id))
                    total_paid = cur.fetchone()[0] or Decimal('0')
                    
                    # Get term amount
                    cur.execute('SELECT amount FROM terms WHERE id = %s', (term_id,))
                    term_amount = cur.fetchone()[0] or Decimal('0')
                    
                    # Update term balance
                    term_balance = term_amount - total_paid
                    cur.execute('''
                        INSERT INTO term_balances (student_id, term_id, balance)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (student_id, term_id) DO UPDATE
                        SET balance = EXCLUDED.balance
                    ''', (student_id, term_id, term_balance))
                
                # Update overall balance
                cur.execute('''
                    SELECT COALESCE(SUM(amount), 0) FROM terms
                ''')
                total_fees = cur.fetchone()[0] or Decimal('0')
                
                cur.execute('''
                    SELECT COALESCE(SUM(amount_paid), 0) 
                    FROM payments 
                    WHERE student_id = %s
                ''', (student_id,))
                total_paid = cur.fetchone()[0] or Decimal('0')
                
                overall_balance = total_fees - total_paid
                cur.execute('''
                    INSERT INTO student_balances (student_id, current_balance)
                    VALUES (%s, %s)
                    ON CONFLICT (student_id) DO UPDATE
                    SET current_balance = EXCLUDED.current_balance
                ''', (student_id, overall_balance))
                
            except Exception as e:
                logger.error(f"Error recalculating balances: {str(e)}")
                flash('Payment deleted but balance recalculation failed', 'warning')
                return redirect(url_for('view_payments'))
            
            flash('Payment deleted successfully!', 'success')
            return redirect(url_for('view_payments'))
            
    except Exception as e:
        logger.error(f"Error in delete_payment: {str(e)}")
        flash(f'Error deleting payment: {str(e)}', 'danger')
        return redirect(url_for('view_payments'))
@app.route('/receipt/<int:payment_id>')
def view_receipt(payment_id):
    """View payment receipt with detailed allocation information"""
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get payment details with student information
            cur.execute('''
                SELECT 
                    p.id, 
                    p.student_id, 
                    p.amount_paid, 
                    p.payment_date, 
                    p.receipt_number,
                    s.name AS student_name, 
                    s.admission_no, 
                    s.form
                FROM payments p
                JOIN students s ON p.student_id = s.id
                WHERE p.id = %s
            ''', (payment_id,))
            payment = cur.fetchone()
            
            if not payment:
                flash('Receipt not found', 'danger')
                return redirect(url_for('view_payments'))

            # Get payment allocations across terms
            cur.execute('''
                SELECT 
                    pa.term_id,
                    t.name AS term_name,
                    t.amount AS term_amount,
                    pa.amount AS allocated_amount,
                    (SELECT COALESCE(SUM(pa2.amount), 0) 
                     FROM payment_allocations pa2 
                     JOIN payments p2 ON pa2.payment_id = p2.id 
                     WHERE p2.student_id = %s AND pa2.term_id = pa.term_id
                     AND p2.id <= %s) AS running_total
                FROM payment_allocations pa
                JOIN terms t ON pa.term_id = t.id
                WHERE pa.payment_id = %s
                ORDER BY t.id
            ''', (payment['student_id'], payment_id, payment_id))
            allocations = cur.fetchall()

            # Calculate term balances after this payment
            term_balances = []
            for alloc in allocations:
                term_balance = alloc['term_amount'] - alloc['running_total']
                term_balances.append({
                    'term_name': alloc['term_name'],
                    'allocated': float(alloc['allocated_amount']),
                    'running_total': float(alloc['running_total']),
                    'balance': float(term_balance),
                    'is_paid': term_balance <= 0
                })

            # Get cumulative balance
            cur.execute('''
                SELECT current_balance 
                FROM student_balances 
                WHERE student_id = %s
            ''', (payment['student_id'],))
            balance_row = cur.fetchone()
            cumulative_balance = float(balance_row['current_balance']) if balance_row else 0.0
            
            # Format dates
            payment_date = payment['payment_date'].strftime('%d/%m/%Y')
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Generate QR code with allocation details
            allocation_details = "\n".join(
                f"{alloc['term_name']}: {alloc['allocated']:.2f}" 
                for alloc in term_balances
            )
            qr_data = f"""
            Receipt: {payment['receipt_number']}
            Student: {payment['student_name']} ({payment['admission_no']})
            Total Paid: {float(payment['amount_paid']):.2f}
            Allocation:
            {allocation_details}
            Cumulative Balance: {"+" if cumulative_balance > 0 else ""}{abs(cumulative_balance):.2f}
            Date: {payment_date}
            """
            
            qr_img = qrcode.make(qr_data)
            qr_buffer = BytesIO()
            qr_img.save(qr_buffer, format="PNG")
            qr_b64 = base64.b64encode(qr_buffer.getvalue()).decode('utf-8')
            
            logo_base64 = get_logo_base64()
            
            return render_template('receipt.html',
                                payment=payment,
                                amount_paid=float(payment['amount_paid']),
                                allocations=term_balances,
                                cumulative_balance=cumulative_balance,
                                current_time=current_time,
                                qr_code=qr_b64,
                                logo_base64=logo_base64,
                                formatted_date=payment_date)
            
    except Exception as e:
        logger.error(f"Error in view_receipt: {str(e)}", exc_info=True)
        flash('Error generating receipt', 'danger')
        return redirect(url_for('view_payments'))
@app.route('/outstanding')
@login_required
def outstanding_balances():
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get students with outstanding balances
            cur.execute('''
                SELECT 
                    s.id, s.name, s.admission_no, s.form,
                    sb.current_balance AS balance,
                    COUNT(obn.id) AS notices_count,
                    MAX(obn.due_date) AS latest_due_date
                FROM students s
                JOIN student_balances sb ON s.id = sb.student_id
                LEFT JOIN outstanding_balance_notices obn ON s.id = obn.student_id AND NOT obn.is_paid
                WHERE sb.current_balance > 0
                GROUP BY s.id, s.name, s.admission_no, s.form, sb.current_balance
                ORDER BY sb.current_balance DESC
            ''')
            students = cur.fetchall()

            # Calculate summary statistics
            cur.execute('''
                SELECT 
                    COUNT(*) AS total_students,
                    COALESCE(SUM(sb.current_balance), 0) AS total_outstanding,
                    COUNT(obn.id) AS total_active_notices
                FROM students s
                JOIN student_balances sb ON s.id = sb.student_id
                LEFT JOIN outstanding_balance_notices obn ON s.id = obn.student_id AND NOT obn.is_paid
                WHERE sb.current_balance > 0
            ''')
            stats = cur.fetchone()

            return render_template('outstanding_balances.html',
                                students=students,
                                stats=stats,
                                current_date=datetime.now().date())

    except Exception as e:
        logger.error(f"Error in outstanding_balances: {str(e)}")
        flash('Error retrieving outstanding balances', 'danger')
        return redirect(url_for('dashboard'))


@app.route('/outstanding/student/<int:student_id>')
@login_required
def student_outstanding_details(student_id):
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get student info and balance
            cur.execute('''
                SELECT 
                    s.id, s.name, s.admission_no, s.form,
                    sb.current_balance AS balance
                FROM students s
                JOIN student_balances sb ON s.id = sb.student_id
                WHERE s.id = %s AND sb.current_balance > 0
            ''', (student_id,))
            student = cur.fetchone()

            if not student:
                flash('No outstanding balance for this student', 'info')
                return redirect(url_for('outstanding_balances'))

            # Get payment history
            cur.execute('''
                SELECT 
                    p.id, p.amount_paid, p.payment_date, p.receipt_number,
                    t.name AS term_name
                FROM payments p
                JOIN terms t ON p.term_id = t.id
                WHERE p.student_id = %s
                ORDER BY p.payment_date DESC
            ''', (student_id,))
            payments = cur.fetchall()

            # Get outstanding notices
            cur.execute('''
                SELECT 
                    id, amount, issued_date, due_date, 
                    reference_number, is_paid, paid_date
                FROM outstanding_balance_notices
                WHERE student_id = %s
                ORDER BY issued_date DESC
            ''', (student_id,))
            notices = cur.fetchall()

            # Get term fees
            cur.execute('SELECT id, name, amount FROM terms ORDER BY name')
            terms = cur.fetchall()

            return render_template('student_outstanding_details.html',
                                student=student,
                                payments=payments,
                                notices=notices,
                                terms=terms,
                                current_date=datetime.now().date())

    except Exception as e:
        logger.error(f"Error in student_outstanding_details: {str(e)}")
        flash('Error retrieving student details', 'danger')
        return redirect(url_for('outstanding_balances'))


@app.route('/outstanding/generate_notice/<int:student_id>', methods=['POST'])
@login_required
def generate_outstanding_notice(student_id):
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Verify student exists and has balance
            cur.execute('''
                SELECT s.id, s.name, s.admission_no, sb.current_balance
                FROM students s
                JOIN student_balances sb ON s.id = sb.student_id
                WHERE s.id = %s AND sb.current_balance > 0
            ''', (student_id,))
            student = cur.fetchone()
            
            if not student:
                flash('Student not found or has no outstanding balance', 'warning')
                return redirect(url_for('outstanding_balances'))

            # Check for existing unpaid notice
            cur.execute('''
                SELECT 1 
                FROM outstanding_balance_notices
                WHERE student_id = %s AND is_paid = FALSE
            ''', (student_id,))
            if cur.fetchone():
                flash('This student already has an active outstanding notice', 'warning')
                return redirect(url_for('student_outstanding_details', student_id=student_id))

            # Generate unique reference number
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            random_suffix = random.randint(1000, 9999)
            reference_no = f"OB-{timestamp}-{student_id}-{random_suffix}"
            
            # Set dates
            issued_date = datetime.now().date()
            due_date = issued_date + timedelta(days=14)
            
            # Create notice
            with get_db_cursor(commit=True) as insert_cur:
                insert_cur.execute('''
                    INSERT INTO outstanding_balance_notices
                    (student_id, amount, issued_date, due_date, reference_number)
                    VALUES (%s, %s, %s, %s, %s)
                ''', (
                    student_id,
                    student['current_balance'],
                    issued_date,
                    due_date,
                    reference_no
                ))
            
            flash('Outstanding balance notice generated successfully', 'success')
            return redirect(url_for('student_outstanding_details', student_id=student_id))
            
    except Exception as e:
        logger.error(f"Error generating notice: {str(e)}", exc_info=True)
        flash('Error generating outstanding balance notice', 'danger')
        return redirect(url_for('student_outstanding_details', student_id=student_id))



@app.route('/outstanding/notice/<int:notice_id>')
@login_required
def view_outstanding_notice(notice_id):
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get notice details
            cur.execute('''
                SELECT 
                    obn.*,
                    s.name AS student_name,
                    s.admission_no,
                    s.form
                FROM outstanding_balance_notices obn
                JOIN students s ON obn.student_id = s.id
                WHERE obn.id = %s
            ''', (notice_id,))
            notice = cur.fetchone()

            if not notice:
                flash('Notice not found', 'danger')
                return redirect(url_for('outstanding_balances'))

            # Generate QR code
            qr_data = f"""Outstanding Balance Notice
Reference: {notice['reference_number']}
Student: {notice['student_name']} ({notice['admission_no']})
Amount: KSh {float(notice['amount']):,.2f}
Due Date: {notice['due_date'].strftime('%d/%m/%Y')}
Status: {'PAID' if notice['is_paid'] else 'PENDING'}"""
            
            qr_img = qrcode.make(qr_data)
            qr_buffer = BytesIO()
            qr_img.save(qr_buffer, format="PNG")
            qr_b64 = base64.b64encode(qr_buffer.getvalue()).decode('utf-8')

            return render_template('outstanding_notice.html',
                                notice=notice,
                                qr_code=qr_b64,
                                logo_base64=get_logo_base64(),
                                current_date=datetime.now().date())

    except Exception as e:
        logger.error(f"Error in view_outstanding_notice: {str(e)}")
        flash('Error retrieving notice', 'danger')
        return redirect(url_for('outstanding_balances'))


@app.route('/outstanding/notice/<int:notice_id>/pdf')
@login_required
def outstanding_notice_pdf(notice_id):
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get notice details with student info
            cur.execute('''
                SELECT 
                    obn.*,
                    s.name AS student_name,
                    s.admission_no,
                    s.form,
                    sb.current_balance
                FROM outstanding_balance_notices obn
                JOIN students s ON obn.student_id = s.id
                JOIN student_balances sb ON s.id = sb.student_id
                WHERE obn.id = %s
            ''', (notice_id,))
            notice = cur.fetchone()
            
            if not notice:
                flash('Notice not found', 'danger')
                return redirect(url_for('outstanding_balances'))

            # Format dates and amounts
            issued_date = notice['issued_date'].strftime('%d/%m/%Y')
            due_date = notice['due_date'].strftime('%d/%m/%Y')
            amount = float(notice['amount'])
            current_balance = float(notice['current_balance'])
            
            # Generate QR code
            qr_data = f"""Outstanding Balance Notice
Reference: {notice['reference_number']}
Student: {notice['student_name']} ({notice['admission_no']})
Amount Due: KSh {amount:,.2f}
Current Balance: KSh {current_balance:,.2f}
Issued: {issued_date}
Due: {due_date}
Status: {'PAID' if notice['is_paid'] else 'PENDING'}"""
            
            qr_img = qrcode.make(qr_data)
            qr_buffer = BytesIO()
            qr_img.save(qr_buffer, format="PNG")
            qr_b64 = base64.b64encode(qr_buffer.getvalue()).decode('utf-8')

            # Generate PDF
            html = render_template('outstanding_notice_pdf.html',
                                notice=notice,
                                issued_date=issued_date,
                                due_date=due_date,
                                amount=amount,
                                current_balance=current_balance,
                                qr_code=qr_b64,
                                logo_base64=get_logo_base64(),
                                current_date=datetime.now().strftime('%d/%m/%Y'))
            
            pdf_bytes = HTML(string=html, base_url=request.host_url).write_pdf()
            
            response = make_response(pdf_bytes)
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = f'inline; filename=Outstanding_Balance_{notice["reference_number"]}.pdf'
            return response
            
    except Exception as e:
        logger.error(f"Error generating PDF notice: {str(e)}", exc_info=True)
        flash('Error generating PDF notice', 'danger')
        return redirect(url_for('outstanding_balances'))
if __name__ == '__main__':
    try:
        port = int(os.environ.get('FLASK_PORT', 5000))
        app.run(host='0.0.0.0', port=port, debug=True)
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}")
        sys.exit(1)
