import os
import re
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = 'your-secret-key'

DB_PATH = 'database.db'

def init_db():
    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE students (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admission_no TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                form TEXT NOT NULL
            )
        ''')
        c.execute('''
            CREATE TABLE terms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                amount REAL NOT NULL
            )
        ''')
        c.execute('''
            CREATE TABLE payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                term_id INTEGER NOT NULL,
                amount_paid REAL NOT NULL,
                payment_date TEXT NOT NULL,
                FOREIGN KEY(student_id) REFERENCES students(id),
                FOREIGN KEY(term_id) REFERENCES terms(id)
            )
        ''')
        conn.commit()
        conn.close()


def query_db(query, args=(), one=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(query, args)
    rv = cur.fetchall()
    conn.commit()
    conn.close()
    return (rv[0] if rv else None) if one else rv

@app.route('/')
def home():
    return redirect(url_for('view_students'))

@app.route('/students')
def view_students():
    students = query_db('SELECT * FROM students')
    return render_template('students.html', students=students)

@app.route('/terms')
def view_terms():
    terms = query_db('SELECT * FROM terms')
    return render_template('terms.html', terms=terms)

@app.route('/payments')
def view_payments():
    payments = query_db('''
        SELECT payments.id, students.name AS student_name, students.admission_no,
               terms.name AS term_name, payments.amount_paid, payments.payment_date
        FROM payments
        JOIN students ON payments.student_id = students.id
        JOIN terms ON payments.term_id = terms.id
        ORDER BY payments.payment_date DESC
    ''')
    students = query_db('SELECT * FROM students')
    terms = query_db('SELECT * FROM terms')
    return render_template('payments.html', payments=payments, students=students, terms=terms)

@app.route('/student/add', methods=['GET', 'POST'])
def add_student():
    if request.method == 'POST':
        admission_no = request.form['admission_no']
        name = request.form['name']
        form = request.form['form']
        try:
            query_db(
                'INSERT INTO students (admission_no, name, form) VALUES (?, ?, ?)',
                (admission_no, name, form)
            )
            flash("Student added successfully.")
        except sqlite3.IntegrityError:
            flash("Admission number must be unique.")
        return redirect(url_for('add_student'))

    # On GET: load existing students
    students = query_db('SELECT * FROM students ORDER BY id DESC LIMIT 10')
    return render_template('add_student.html', students=students)

@app.route('/term/add', methods=['POST'])
def add_term():
    name = request.form['term_name']
    amount = request.form['amount']
    try:
        query_db('INSERT INTO terms (name, amount) VALUES (?, ?)', (name, amount))
        flash("Term added successfully.")
    except sqlite3.IntegrityError:
        flash("Term name must be unique.")
 
@app.route('/payments')
def view_payments():
    search_query = request.args.get('q', '').strip()

    # Base SQL query to get payment details
    base_query = '''
        SELECT payments.id, students.name AS student_name, students.admission_no,
               terms.name AS term_name, payments.amount_paid, payments.payment_date
        FROM payments
        JOIN students ON payments.student_id = students.id
        JOIN terms ON payments.term_id = terms.id
    '''
    params = ()

    # If search query exists, add WHERE clause
    if search_query:
        base_query += '''
            WHERE students.name LIKE ? OR
                  students.admission_no LIKE ? OR
                  terms.name LIKE ?
        '''
        like_query = f"%{search_query}%"
        params = (like_query, like_query, like_query)

    # Order the results by most recent payment
    base_query += ' ORDER BY payments.payment_date DESC'

    # Execute the final query
    payments = query_db(base_query, params)

    # Optional: You can also fetch student and term data for dropdowns/forms
    students = query_db('SELECT * FROM students ORDER BY name')
    terms = query_db('SELECT * FROM terms ORDER BY id DESC')

    return render_template(
        'payments.html',
        payments=payments,
        students=students,
        terms=terms,
        search_query=search_query
    )

@app.route('/student/edit/<int:id>', methods=['POST'])
def edit_student(id):
    name = request.form['name']
    form = request.form['form']
    query_db('UPDATE students SET name=?, form=? WHERE id=?', (name, form, id))
    flash("Student updated successfully.")
    return redirect(url_for('view_students'))

@app.route('/student/delete/<int:id>', methods=['POST'])
def delete_student(id):
    query_db('DELETE FROM students WHERE id=?', (id,))
    flash("Student deleted successfully.")
    return redirect(url_for('view_students'))

@app.route('/term/edit/<int:id>', methods=['POST'])
def edit_term(id):
    name = request.form['term_name']
    amount = request.form['amount']
    query_db('UPDATE terms SET name=?, amount=? WHERE id=?', (name, amount, id))
    flash("Term updated successfully.")
    return redirect(url_for('view_terms'))

@app.route('/term/delete/<int:id>', methods=['POST'])
def delete_term(id):
    query_db('DELETE FROM terms WHERE id=?', (id,))
    flash("Term deleted successfully.")
    return redirect(url_for('view_terms'))

@app.route('/payment/edit/<int:id>', methods=['POST'])
def edit_payment(id):
    student_id = request.form['student_id']
    term_id = request.form['term_id']
    amount_paid = request.form['amount_paid']
    payment_date = request.form['payment_date']
    query_db('''
        UPDATE payments SET student_id=?, term_id=?, amount_paid=?, payment_date=?
        WHERE id=?
    ''', (student_id, term_id, amount_paid, payment_date, id))
    flash("Payment updated successfully.")
    return redirect(url_for('view_payments'))

@app.route('/payment/delete/<int:id>', methods=['POST'])
def delete_payment(id):
    query_db('DELETE FROM payments WHERE id=?', (id,))
    flash("Payment deleted successfully.")
    return redirect(url_for('view_payments'))

@app.route('/receipt/<int:payment_id>')
def view_receipt(payment_id):
    payment = query_db('''
        SELECT payments.id, students.name AS student_name, students.admission_no,
               students.form, terms.name AS term_name, terms.amount AS term_amount,
               payments.amount_paid, payments.payment_date
        FROM payments
        JOIN students ON payments.student_id = students.id
        JOIN terms ON payments.term_id = terms.id
        WHERE payments.id = ?
    ''', (payment_id,), one=True)

    if not payment:
        return "Receipt not found", 404

    return render_template('receipt.html', payment=payment, current_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

@app.route('/reports/outstanding_balance')
def report_outstanding_balance():
    data = query_db('''
        SELECT 
            students.id,
            students.name,
            students.admission_no,
            IFNULL(SUM(terms.amount), 0) AS total_due,
            IFNULL(SUM(payments.amount_paid), 0) AS total_paid,
            (IFNULL(SUM(terms.amount), 0) - IFNULL(SUM(payments.amount_paid), 0)) AS outstanding_balance
        FROM students
        LEFT JOIN terms ON 1=1
        LEFT JOIN payments ON payments.student_id = students.id AND payments.term_id = terms.id
        GROUP BY students.id
        HAVING outstanding_balance > 0
        ORDER BY outstanding_balance DESC
    ''')
    return render_template('reports_outstanding_balance.html', data=data)

# ✅ Proper entry point
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
