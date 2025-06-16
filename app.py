import os
import time
import psycopg2
from psycopg2 import pool, extras
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, make_response, session, jsonify
from weasyprint import HTML
import qrcode
import base64
from io import BytesIO
from contextlib import contextmanager
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from dotenv import load_dotenv
import sys
import atexit

# Load environment variables
load_dotenv()

# Configuration
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'dev-secret-key')

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
                
            # Ensure connection string uses postgresql://
            if DATABASE_URL.startswith('postgres://'):
                DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
                
            db_pool = pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=DATABASE_URL,
                sslmode='require'
            )
            print("✅ Database connection established")
            init_db()
            return
        except Exception as e:
            print(f"❌ Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                print("❌ Failed to connect to database after multiple attempts")
                sys.exit(1)
def get_logo_base64():
    try:
        logo_path = os.path.join(app.static_folder, 'images', 'LOGO.jpg')
        with open(logo_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        print(f"Error loading logo: {str(e)}")
        return None
@atexit.register
def shutdown_db_pool():
    global db_pool
    if db_pool:
        db_pool.closeall()
        print("Database connection pool closed")

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
def get_db_cursor(dict_cursor=False):
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
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"Database error: {e}")
            raise
        finally:
            cur.close()
    finally:
        db_pool.putconn(conn)

def init_db():
    """Initialize database tables"""
    with get_db_cursor() as cur:
        # Core tables
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
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
        
        cur.execute('''
            CREATE TABLE IF NOT EXISTS terms (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                amount DECIMAL(10,2) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
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

# Error handlers
@app.errorhandler(psycopg2.OperationalError)
def handle_db_error(e):
    flash('Database connection error. Please try again.', 'danger')
    return redirect(url_for('dashboard'))

@app.errorhandler(500)
def handle_server_error(e):
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
        return jsonify({'status': 'unhealthy', 'database': str(e)}), 500

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
            with get_db_cursor() as cur:
                cur.execute('SELECT * FROM users WHERE username = %s', (username,))
                user = cur.fetchone()
                
                if user and check_password_hash(user[2], password):
                    session['user_id'] = user[0]
                    session['username'] = user[1]
                    flash('Login successful!', 'success')
                    return redirect(url_for('dashboard'))
                else:
                    flash('Invalid username or password', 'danger')
        except Exception as e:
            flash('Database error during login', 'danger')
            print(f"Login error: {str(e)}")
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out', 'info')
    return redirect(url_for('login'))

@app.route('/')
@login_required
def dashboard():
    return redirect(url_for('view_students'))

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
        flash('Error retrieving students', 'danger')
        print(f"Error in view_students: {str(e)}")
        students = []
    
    return render_template('students.html', students=students, search=search)

@app.route('/student/add', methods=['GET', 'POST'])
@login_required
def add_student():
    if request.method == 'POST':
        admission_no = request.form['admission_no'].strip()
        name = request.form['name'].strip()
        form = request.form['form'].strip()
        
        try:
            with get_db_cursor() as cur:
                cur.execute(
                    'INSERT INTO students (admission_no, name, form) VALUES (%s, %s, %s)',
                    (admission_no, name, form)
                )
                flash('Student added successfully!', 'success')
                return redirect(url_for('view_students'))
        except psycopg2.IntegrityError:
            flash('Admission number must be unique!', 'danger')
        except Exception as e:
            flash(f'Error adding student: {str(e)}', 'danger')
            print(f"Error in add_student: {str(e)}")
    
    return render_template('add_student.html')

@app.route('/student/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_student(id):
    if request.method == 'POST':
        name = request.form['name'].strip()
        form = request.form['form'].strip()
        
        try:
            with get_db_cursor() as cur:
                cur.execute(
                    'UPDATE students SET name = %s, form = %s WHERE id = %s',
                    (name, form, id)
                )
                flash('Student updated successfully!', 'success')
                return redirect(url_for('view_students'))
        except Exception as e:
            flash(f'Error updating student: {str(e)}', 'danger')
            print(f"Error in edit_student: {str(e)}")
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('SELECT * FROM students WHERE id = %s', (id,))
            student = cur.fetchone()
    except Exception as e:
        flash('Error retrieving student', 'danger')
        print(f"Error retrieving student: {str(e)}")
        return redirect(url_for('view_students'))
    
    if not student:
        flash('Student not found', 'danger')
        return redirect(url_for('view_students'))
    
    return render_template('edit_student.html', student=student)

@app.route('/student/delete/<int:id>', methods=['POST'])
@login_required
def delete_student(id):
    try:
        with get_db_cursor() as cur:
            cur.execute('DELETE FROM students WHERE id = %s', (id,))
            flash('Student deleted successfully!', 'success')
    except Exception as e:
        flash(f'Error deleting student: {str(e)}', 'danger')
        print(f"Error in delete_student: {str(e)}")
    
    return redirect(url_for('view_students'))

@app.route('/terms')
@login_required
def view_terms():
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('SELECT * FROM terms ORDER BY name')
            terms = cur.fetchall()
    except Exception as e:
        flash('Error retrieving terms', 'danger')
        print(f"Error in view_terms: {str(e)}")
        terms = []
    
    return render_template('terms.html', terms=terms)

@app.route('/term/add', methods=['GET', 'POST'])
@login_required
def add_term():
    if request.method == 'POST':
        name = request.form['name'].strip()
        amount = request.form['amount'].strip()
        
        try:
            amount = float(amount)
            with get_db_cursor() as cur:
                cur.execute(
                    'INSERT INTO terms (name, amount) VALUES (%s, %s)',
                    (name, amount)
                )
                flash('Term added successfully!', 'success')
                return redirect(url_for('view_terms'))
        except ValueError:
            flash('Amount must be a valid number', 'danger')
        except psycopg2.IntegrityError:
            flash('Term name must be unique!', 'danger')
        except Exception as e:
            flash(f'Error adding term: {str(e)}', 'danger')
            print(f"Error in add_term: {str(e)}")
    
    return render_template('add_term.html')

@app.route('/term/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_term(id):
    if request.method == 'POST':
        name = request.form['name'].strip()
        amount = request.form['amount'].strip()
        
        try:
            amount = float(amount)
            with get_db_cursor() as cur:
                cur.execute(
                    'UPDATE terms SET name = %s, amount = %s WHERE id = %s',
                    (name, amount, id)
                )
                flash('Term updated successfully!', 'success')
                return redirect(url_for('view_terms'))
        except ValueError:
            flash('Amount must be a valid number', 'danger')
        except psycopg2.IntegrityError:
            flash('Term name must be unique!', 'danger')
        except Exception as e:
            flash(f'Error updating term: {str(e)}', 'danger')
            print(f"Error in edit_term: {str(e)}")
    
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('SELECT * FROM terms WHERE id = %s', (id,))
            term = cur.fetchone()
    except Exception as e:
        flash('Error retrieving term', 'danger')
        print(f"Error retrieving term: {str(e)}")
        return redirect(url_for('view_terms'))
    
    if not term:
        flash('Term not found', 'danger')
        return redirect(url_for('view_terms'))
    
    return render_template('edit_term.html', term=term)

@app.route('/term/delete/<int:id>', methods=['POST'])
@login_required
def delete_term(id):
    try:
        with get_db_cursor() as cur:
            cur.execute('DELETE FROM terms WHERE id = %s', (id,))
            flash('Term deleted successfully!', 'success')
    except Exception as e:
        flash(f'Error deleting term: {str(e)}', 'danger')
        print(f"Error in delete_term: {str(e)}")
    
    return redirect(url_for('view_terms'))

@app.route('/payments')
@login_required
def view_payments():
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('''
                SELECT p.id, s.name AS student_name, s.admission_no,
                       t.name AS term_name, p.amount_paid, p.payment_date,
                       p.receipt_number
                FROM payments p
                JOIN students s ON p.student_id = s.id
                JOIN terms t ON p.term_id = t.id
                ORDER BY p.payment_date DESC
            ''')
            payments = cur.fetchall()
            
            cur.execute('SELECT id, name FROM students ORDER BY name')
            students = cur.fetchall()
            
            cur.execute('SELECT id, name FROM terms ORDER BY name')
            terms = cur.fetchall()
    except Exception as e:
        flash('Error retrieving payments', 'danger')
        print(f"Error in view_payments: {str(e)}")
        payments = []
        students = []
        terms = []
    
    return render_template('payments.html', 
                         payments=payments,
                         students=students,
                         terms=terms)

@app.route('/payment/add', methods=['POST'])
@login_required
def add_payment():
    student_input = request.form.get('student_input', '').strip()
    term_id = request.form.get('term_id', '').strip()
    amount_paid = request.form.get('amount_paid', '').strip()
    payment_date = request.form.get('payment_date', '').strip()
    
    if not all([student_input, term_id, amount_paid, payment_date]):
        flash('Please fill in all payment fields', 'danger')
        return redirect(url_for('view_payments'))
    
    try:
        amount_paid = float(amount_paid)
        receipt_number = f"RCPT-{datetime.now().strftime('%s%m%d')}-{os.urandom(2).hex().upper()}"
        
        with get_db_cursor() as cur:
            cur.execute('''
                SELECT id FROM students 
                WHERE id = %s OR admission_no = %s
            ''', (student_input, student_input))
            student = cur.fetchone()
            
            if not student:
                flash('Student not found', 'danger')
                return redirect(url_for('view_payments'))
            
            student_id = student[0]
            
            cur.execute('SELECT id FROM terms WHERE id = %s', (term_id,))
            if not cur.fetchone():
                flash('Term not found', 'danger')
                return redirect(url_for('view_payments'))
            
            cur.execute('''
                INSERT INTO payments 
                (student_id, term_id, amount_paid, payment_date, receipt_number)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            ''', (student_id, term_id, amount_paid, payment_date, receipt_number))
            
            payment_id = cur.fetchone()[0]
            flash('Payment recorded successfully!', 'success')
            return redirect(url_for('view_receipt', payment_id=payment_id))
            
    except ValueError:
        flash('Amount must be a valid number', 'danger')
    except Exception as e:
        flash(f'Error recording payment: {str(e)}', 'danger')
        print(f"Error in add_payment: {str(e)}")
    
    return redirect(url_for('view_payments'))

@app.route('/payment/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_payment(id):
    if request.method == 'POST':
        student_id = request.form['student_id'].strip()
        term_id = request.form['term_id'].strip()
        amount_paid = request.form['amount_paid'].strip()
        payment_date = request.form['payment_date'].strip()
        
        try:
            amount_paid = float(amount_paid)
            with get_db_cursor() as cur:
                cur.execute('''
                    UPDATE payments 
                    SET student_id = %s, term_id = %s, 
                        amount_paid = %s, payment_date = %s
                    WHERE id = %s
                ''', (student_id, term_id, amount_paid, payment_date, id))
                
                flash('Payment updated successfully!', 'success')
                return redirect(url_for('view_payments'))
        except ValueError:
            flash('Amount must be a valid number', 'danger')
        except Exception as e:
            flash(f'Error updating payment: {str(e)}', 'danger')
            print(f"Error in edit_payment: {str(e)}")
    
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
        flash('Error retrieving payment', 'danger')
        print(f"Error retrieving payment: {str(e)}")
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
        with get_db_cursor() as cur:
            cur.execute('DELETE FROM payments WHERE id = %s', (id,))
            flash('Payment deleted successfully!', 'success')
    except Exception as e:
        flash(f'Error deleting payment: {str(e)}', 'danger')
        print(f"Error in delete_payment: {str(e)}")
    
    return redirect(url_for('view_payments'))
@app.route('/receipt/<int:payment_id>')
@login_required
def view_receipt(payment_id):
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get payment details
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
            
            # Convert amounts to float for consistent calculations
            term_amount = float(payment['term_amount'])
            amount_paid = float(payment['amount_paid'])
            
            # Calculate total paid by this student
            cur.execute('''
                SELECT COALESCE(SUM(amount_paid), 0)::float
                FROM payments
                WHERE student_id = %s
            ''', (payment['student_id'],))
            total_paid = float(cur.fetchone()[0])
            
            # Calculate outstanding balance
            outstanding_balance = term_amount - total_paid
            
            # Generate QR code
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
            
            # Get logo as base64
            logo_base64 = get_logo_base64()
            
    except Exception as e:
        flash('Error generating receipt', 'danger')
        print(f"Error in view_receipt: {str(e)}")
        return redirect(url_for('view_payments'))
    
    return render_template('receipt.html',
                        payment=payment,
                        total_paid=total_paid,
                        outstanding_balance=outstanding_balance,
                        current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        qr_code=qr_b64,
                        logo_base64=logo_base64)
@app.route('/reports/outstanding')
@login_required
def outstanding_report():
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            cur.execute('''
                SELECT s.id, s.name, s.admission_no,
                       SUM(t.amount) AS total_due,
                       COALESCE(SUM(p.amount_paid), 0) AS total_paid,
                       SUM(t.amount) - COALESCE(SUM(p.amount_paid), 0) AS balance
                FROM students s
                CROSS JOIN terms t
                LEFT JOIN payments p ON s.id = p.student_id AND t.id = p.term_id
                GROUP BY s.id
                HAVING SUM(t.amount) - COALESCE(SUM(p.amount_paid), 0) > 0
                ORDER BY balance DESC
            ''')
            report_data = cur.fetchall()
    except Exception as e:
        flash('Error generating report', 'danger')
        print(f"Error in outstanding_report: {str(e)}")
        report_data = []
    
    return render_template('outstanding_report.html', report_data=report_data)
@app.route('/receipt/<int:payment_id>/pdf')
@login_required
def generate_receipt_pdf(payment_id):
    try:
        with get_db_cursor(dict_cursor=True) as cur:
            # Get payment details
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
            
            # Convert amounts to float
            term_amount = float(payment['term_amount'])
            amount_paid = float(payment['amount_paid'])
            
            # Calculate total paid
            cur.execute('''
                SELECT COALESCE(SUM(amount_paid), 0)::float
                FROM payments
                WHERE student_id = %s
            ''', (payment['student_id'],))
            total_paid = float(cur.fetchone()[0])
            
            outstanding_balance = term_amount - total_paid
            
            # Generate QR code
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
            
            # Get logo
            logo_base64 = get_logo_base64()
            
            # Render HTML
            html = render_template('receipt_pdf.html',
                                payment=payment,
                                total_paid=total_paid,
                                outstanding_balance=outstanding_balance,
                                current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                qr_code=qr_b64,
                                logo_base64=logo_base64)
            
            # Generate PDF
            pdf_bytes = HTML(string=html).write_pdf()
            
            # Create response
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
            # Get report data
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

            # Get logo
            logo_base64 = get_logo_base64()

            # Render HTML
            html = render_template('outstanding_report_pdf.html',
                                report_data=report_data,
                                current_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                logo_base64=logo_base64)
            
            # Generate PDF
            pdf_bytes = HTML(string=html).write_pdf()
            
            # Create response
            response = make_response(pdf_bytes)
            response.headers['Content-Type'] = 'application/pdf'
            response.headers['Content-Disposition'] = 'inline; filename=outstanding_balances.pdf'
            return response
            
    except Exception as e:
        flash('Error generating PDF report', 'danger')
        print(f"Error in outstanding_report_pdf: {str(e)}")
        return redirect(url_for('outstanding_report'))    
if __name__ == '__main__':
    try:
        port = int(os.environ.get('FLASK_PORT', 5000))
        app.run(host='0.0.0.0', port=port, debug=True)
    except Exception as e:
        print(f"Failed to start application: {str(e)}")
        sys.exit(1)
