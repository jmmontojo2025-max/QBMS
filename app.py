import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone, timedelta
from sqlalchemy import func
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect

app = Flask(__name__)

# --- CONFIGURATION ---
app.secret_key = os.environ.get("SECRET_KEY", "coolaire_secret_key_2024")
csrf = CSRFProtect(app)

# Database Handling (Supabase Fix)
db_url = os.environ.get("DATABASE_URL",
                        "postgresql://postgres.mguajchtxgunyfzotipa:Itadmin36155912030*@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Rate Limiting
limiter = Limiter(key_func=get_remote_address, app=app, storage_uri="memory://")


# --- MODELS ---
class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    full_name = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    role = db.Column(db.String(20), default='customer')
    bookings = db.relationship('Booking', backref='customer', lazy=True)


class Booking(db.Model):
    __tablename__ = 'bookings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='pending')
    queue_entry = db.relationship('Queue', backref='booking', uselist=False)


class Queue(db.Model):
    __tablename__ = 'queues'
    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(10), nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=True)
    status = db.Column(db.String(20), default='waiting')
    call_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --- ROUTES ---

@app.route('/')
def home():
    return render_template('kiosk.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password_hash, request.form.get('password')):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash("Invalid Credentials", "danger")
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        if User.query.filter_by(username=username).first():
            flash("Username exists!", "danger")
            return redirect(url_for('register'))
        new_user = User(
            username=username,
            email=request.form.get('email'),
            password_hash=generate_password_hash(request.form.get('password')),
            full_name=request.form.get('full_name'),
            phone=request.form.get('phone')
        )
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role == 'staff':
        return redirect(url_for('staff_panel'))
    booking = Booking.query.filter_by(user_id=current_user.id).order_by(Booking.id.desc()).first()
    queue = booking.queue_entry if booking else None
    return render_template('dashboard.html', booking=booking, queue=queue, timedelta=timedelta)


@app.route('/book', methods=['GET', 'POST'])
@login_required
def book():
    if request.method == 'POST':
        time_str = request.form.get('time')
        appt_time = datetime.strptime(time_str, '%Y-%m-%dT%H:%M')
        new_booking = Booking(user_id=current_user.id, scheduled_time=appt_time, status='pending')
        db.session.add(new_booking)
        db.session.commit()
        return redirect(url_for('dashboard'))
    return render_template('book.html')


@app.route('/check-in', methods=['POST'])
def check_in():
    ref_num_raw = request.form.get('booking_id', '').strip()
    if not ref_num_raw.isdigit():
        flash("Invalid Reference Number", "danger")
        return redirect(url_for('home'))

    real_db_id = int(ref_num_raw) - 5000
    booking = db.session.get(Booking, real_db_id)

    if booking and booking.status == 'pending':
        today = datetime.now(timezone.utc).date()
        count = Queue.query.filter(Queue.ticket_number.like('A-%'), func.date(Queue.created_at) == today).count()
        new_ticket = Queue(ticket_number=f"A-{101 + count}", booking_id=booking.id)
        booking.status = 'checked_in'
        db.session.add(new_ticket)
        db.session.commit()
        return redirect(url_for('print_ticket', q_id=new_ticket.id))

    flash("Reference not found", "danger")
    return redirect(url_for('home'))


@app.route('/walk-in', methods=['POST'])
def walk_in():
    today = datetime.now(timezone.utc).date()
    count = Queue.query.filter(Queue.ticket_number.like('W-%'), func.date(Queue.created_at) == today).count()
    new_ticket = Queue(ticket_number=f"W-{101 + count}", status='waiting')
    db.session.add(new_ticket)
    db.session.commit()
    return redirect(url_for('print_ticket', q_id=new_ticket.id))


# THIS IS THE ONLY "print_ticket" FUNCTION ALLOWED
@app.route('/print-ticket/<int:q_id>')
def print_ticket(q_id):
    ticket = db.session.get(Queue, q_id)
    if not ticket:
        return redirect(url_for('home'))
    return render_template('print_ticket.html', ticket=ticket)


@app.route('/staff')
@login_required
def staff_panel():
    if current_user.role != 'staff': return "Unauthorized", 403
    waiting = Queue.query.filter_by(status='waiting').order_by(Queue.created_at.asc()).all()
    serving = Queue.query.filter_by(status='serving').first()
    return render_template('staff.html', waiting_tickets=waiting, now_serving=serving, timedelta=timedelta)


@app.route('/call-next', methods=['POST'])
@login_required
def call_next():
    if current_user.role != 'staff': return "Unauthorized", 403
    specific_id = request.form.get('specific_id')
    Queue.query.filter_by(status='serving').update({"status": 'done'})
    if specific_id:
        target = db.session.get(Queue, specific_id)
        if target:
            target.status = 'serving'
            target.call_count += 1
    else:
        target = Queue.query.filter_by(status='waiting').order_by(Queue.ticket_number.asc(),
                                                                  Queue.created_at.asc()).first()
        if target:
            target.status = 'serving'
            target.call_count = 1
    db.session.commit()
    return redirect(url_for('staff_panel'))


@app.route('/api/get-latest-queue')
def get_latest_queue():
    serving = Queue.query.filter_by(status='serving').first()
    waiting = Queue.query.filter_by(status='waiting').order_by(Queue.created_at.asc()).all()
    return jsonify({
        "now_serving": serving.ticket_number if serving else "---",
        "call_count": serving.call_count if serving else 0,
        "waiting": [t.ticket_number for t in waiting]
    })


@app.route('/tv')
def tv_display():
    return render_template('tv.html')


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)