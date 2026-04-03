import os
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, Index, desc, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from flask_wtf.csrf import CSRFProtect
from functools import wraps
import csv
from io import StringIO
from flask import make_response
from datetime import timedelta
from flask_login import LoginManager

import smtplib
from email.mime.text import MIMEText

from sqlalchemy import extract

from sqlalchemy.exc import IntegrityError



# Create a global lock for ticket generation
ticket_lock = threading.Lock()

# --- INITIALIZATION & LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = '3cc4b579f785f434049ef47d012d9715be0f6eecd80049d6' # Required for CSRF
csrf = CSRFProtect(app)


login_manager = LoginManager(app)


# --- CONFIGURATION ---
class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "qbms_v1_secure_key_2024")
    # Using your provided Supabase URL
    raw_db_url = os.environ.get("DATABASE_URL",
                                "postgresql://postgres.mguajchtxgunyfzotipa:Itadmin36155912030*@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres")
    SQLALCHEMY_DATABASE_URI = raw_db_url.replace("postgres://", "postgresql://", 1) if raw_db_url.startswith(
        "postgres://") else raw_db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }


app.config.from_object(Config)
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'warning'


# --- CONSTANTS ---
class Status:
    PENDING = 'pending'
    CHECKED_IN = 'checked_in'
    WAITING = 'waiting'
    SERVING = 'serving'
    DONE = 'done'


class Roles:
    STAFF = 'staff'
    CUSTOMER = 'customer'


# --- MODELS ---

class ServiceCategory(db.Model):
    __tablename__ = 'service_categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)


class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    company_name = db.Column(db.String(150))
    role = db.Column(db.String(20), default=Roles.CUSTOMER, index=True)
    vehicles = db.relationship('Vehicle', backref='owner', lazy=True)
    bookings_list = db.relationship('Booking', back_populates='customer', lazy='dynamic')


class Vehicle(db.Model):
    __tablename__ = 'vehicles'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    plate_number = db.Column(db.String(50), nullable=False, index=True)
    model_description = db.Column(db.String(100))


class Booking(db.Model):
    __tablename__ = 'bookings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'))
    product_type = db.Column(db.String(100))
    service_type = db.Column(db.String(255))
    scheduled_time = db.Column(db.DateTime, nullable=False, index=True)
    status = db.Column(db.String(20), default=Status.PENDING)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    customer = db.relationship('User', back_populates='bookings_list')
    vehicle = db.relationship('Vehicle')
    queue_entry = db.relationship('Queue', backref='booking', uselist=False)

    @property
    def ref_id(self) -> int:
        return self.id + 5000


class Queue(db.Model):
    __tablename__ = 'queues'
    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(10), nullable=False)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=True)
    status = db.Column(db.String(20), default=Status.WAITING, index=True)
    call_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)


class Holiday(db.Model):
    __tablename__ = 'holidays'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)

class SystemSetting(db.Model):
    __tablename__ = 'system_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=True)
    description = db.Column(db.String(255))

# Helper to get settings easily
def get_setting(key, default=""):
    setting = SystemSetting.query.filter_by(key=key).first()
    return setting.value if setting else default


def send_notifications(booking):
    # 1. EMAIL LOGIC
    host_email = get_setting('MAIL_HOST_USER')
    host_pass = get_setting('MAIL_HOST_PASSWORD')

    if host_email and host_pass:
        try:
            msg = MIMEText(
                f"Hello {booking.customer.full_name}, your unit {booking.vehicle.plate_number} is scheduled for {booking.scheduled_time}.")
            msg['Subject'] = f"Coolaire Appointment: {booking.ref_id}"
            msg['From'] = host_email
            msg['To'] = booking.customer.email

            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(host_email, host_pass)
                server.send_message(msg)
        except Exception as e:
            logger.error(f"Email Failed: {e}")

    # 2. SMS LOGIC (Placeholder for API like Twilio/Infobip)
    sms_api_key = get_setting('SMS_API_KEY')
    if sms_api_key:
        # Example: requests.post('https://api.sms.com/send', data={'to': booking.customer.phone, ...})
        logger.info(f"SMS Triggered for {booking.customer.phone}")


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# --- HELPERS ---
def roles_required(*roles):
    def wrapper(f):
        @wraps(f)
        @login_required
        def decorated_view(*args, **kwargs):
            if current_user.role not in roles: abort(403)
            return f(*args, **kwargs)

        return decorated_view

    return wrapper


def get_daily_ticket_count(prefix: str) -> int:
    today = datetime.now(timezone.utc).date()
    return Queue.query.filter(Queue.ticket_number.like(f'{prefix}-%'), func.date(Queue.created_at) == today).count()


# --- ROUTES ---

@app.route('/')
def home():
    # If the user is already logged in, send them to their specific dashboard
    if current_user.is_authenticated:
        if current_user.role == Roles.STAFF:
            return redirect(url_for('staff_panel'))
        return redirect(url_for('dashboard'))

    # Otherwise, make Login the first screen they see
    return render_template('login.html')


# Move the Kiosk to its own dedicated URL
@app.route('/kiosk')
def kiosk():
    return render_template('kiosk.html')


@app.route('/dashboard')
@login_required
def dashboard():
    # 1. Get ALL current active bookings for the sidebar
    active_bookings = Booking.query.filter(
        Booking.user_id == current_user.id,
        Booking.status.in_([Status.PENDING, Status.CHECKED_IN])
    ).order_by(Booking.scheduled_time.asc()).all()

    # 2. Handle specific unit selection from sidebar
    selected_id = request.args.get('booking_id', type=int)
    booking = None
    if selected_id:
        booking = Booking.query.filter_by(id=selected_id, user_id=current_user.id).first()

    # Default to soonest if none selected
    if not booking and active_bookings:
        booking = active_bookings[0]

    # 3. Queue status for selected unit
    queue = None
    if booking:
        queue = Queue.query.filter_by(booking_id=booking.id).filter(Queue.status != Status.DONE).first()

    # 4. Service History (Status == DONE)
    history = Booking.query.filter_by(user_id=current_user.id, status=Status.DONE).order_by(
        Booking.scheduled_time.desc()).all()

    return render_template('dashboard.html',
                           booking=booking,
                           all_active_bookings=active_bookings,
                           queue=queue,
                           service_history=history)


@app.route('/book', methods=['GET', 'POST'])
@login_required
def book():
    if request.method == 'POST':
        v_id = request.form.get('vehicle_id')
        if v_id == 'new':
            new_v = Vehicle(
                user_id=current_user.id,
                plate_number=request.form.get('new_plate').upper(),
                model_description=request.form.get('new_model')
            )
            db.session.add(new_v)
            db.session.commit()
            v_id = new_v.id

        new_booking = Booking(
            user_id=current_user.id,
            vehicle_id=v_id,
            product_type=request.form.get('product'),
            service_type=", ".join(request.form.getlist('services')),
            scheduled_time=datetime.strptime(request.form.get('time'), '%Y-%m-%dT%H:%M')
        )
        db.session.add(new_booking)
        db.session.commit()
        flash(f"Appointment Set! Ref ID: {new_booking.ref_id}", "success")
        return redirect(url_for('dashboard'))

    # Load dynamic data for the form
    categories = ServiceCategory.query.order_by(ServiceCategory.name.asc()).all()
    vehicles = Vehicle.query.filter_by(user_id=current_user.id).all()
    holidays = [h.date.strftime('%Y-%m-%d') for h in Holiday.query.all()]
    return render_template('book.html', vehicles=vehicles, holidays=holidays, categories=categories)


@app.route('/check-in', methods=['POST'])
def check_in():
    ref_raw = request.form.get('booking_id', '')
    if not ref_raw.isdigit():
        flash("Invalid Reference ID", "danger")
        return redirect(url_for('home'))

    booking = db.session.get(Booking, int(ref_raw) - 5000)

    if booking and booking.status == Status.PENDING:
        # Retry logic: In case two people click at once, try a few times
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # 1. Calculate count inside the transaction
                today = datetime.now(timezone.utc).date()
                count = Queue.query.filter(
                    Queue.ticket_number.like('A-%'),
                    Queue.created_date == today
                ).count()

                new_ticket_no = f"A-{101 + count}"

                new_q = Queue(
                    ticket_number=new_ticket_no,
                    booking_id=booking.id,
                    created_date=today  # Critical for the constraint
                )
                booking.status = Status.CHECKED_IN

                db.session.add(new_q)
                db.session.commit()  # The DB will check the constraint here

                return redirect(url_for('print_ticket', q_id=new_q.id))

            except IntegrityError:
                db.session.rollback()
                # If we hit an IntegrityError, it means someone else took that number
                # Loop will run again, get the NEW count, and try again.
                continue

    flash("Reference ID not found or error occurred.", "danger")
    return redirect(url_for('home'))


@app.route('/print-ticket/<int:q_id>')
def print_ticket(q_id):
    ticket = db.session.get(Queue, q_id)
    if not ticket: abort(404)
    return render_template('print_ticket.html', ticket=ticket)


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            hashed_pw = generate_password_hash(request.form.get('password'), method='scrypt')
            new_user = User(
                username=request.form.get('username').strip(),
                email=request.form.get('email').strip().lower(),
                password_hash=hashed_pw,
                full_name=request.form.get('full_name'),
                phone=request.form.get('phone'),
                company_name=request.form.get('company_name')
            )
            db.session.add(new_user)
            db.session.commit()
            flash("Account created! Please login.", "success")
            return redirect(url_for('login'))
        except IntegrityError:
            db.session.rollback()
            flash("Error: Username or Email already exists.", "danger")
    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password_hash, request.form.get('password')):
            login_user(user)

            # REDIRECTION LOGIC BASED ON ROLE
            if user.role == Roles.STAFF:
                return redirect(url_for('staff_panel'))  # Go to Sidebar Console
            else:
                return redirect(url_for('dashboard'))  # Go to Customer Portal

        flash("Invalid Credentials", "danger")
    return render_template('login.html')


@app.route('/staff')
@roles_required(Roles.STAFF)
def staff_panel():
    waiting = Queue.query.filter_by(status=Status.WAITING).order_by(Queue.created_at.asc()).all()
    serving = Queue.query.filter_by(status=Status.SERVING).first()
    return render_template('staff.html', waiting_tickets=waiting, now_serving=serving, title="Live Console")



@app.route('/call-next', methods=['POST'])
@roles_required(Roles.STAFF)
def call_next():
    try:
        # Update current serving to DONE
        current_serving = Queue.query.filter_by(status=Status.SERVING).first()
        if current_serving:
            current_serving.status = Status.DONE
            if current_serving.booking:
                current_serving.booking.status = Status.DONE  # Moves to history

        specific_id = request.form.get('specific_id')
        target = db.session.get(Queue, specific_id) if specific_id else Queue.query.filter_by(
            status=Status.WAITING).order_by(Queue.created_at.asc()).first()

        if target:
            target.status = Status.SERVING
            target.call_count = (target.call_count or 0) + 1

        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
    return redirect(url_for('staff_panel'))


@app.route('/walk-in', methods=['POST'])
def walk_in():
    try:
        with ticket_lock:
            count = get_daily_ticket_count('W')
            new_ticket = Queue(ticket_number=f"W-{101 + count}")
            db.session.add(new_ticket)
            db.session.commit()

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"status": "success", "ticket": new_ticket.ticket_number})
        return redirect(url_for('print_ticket', q_id=new_ticket.id))
    except Exception:
        db.session.rollback()
        return jsonify({"status": "error"}), 500


@app.route('/api/get-latest-queue')
def get_latest_queue():
    serving = Queue.query.filter_by(status=Status.SERVING).first()
    waiting_list = db.session.query(Queue.ticket_number).filter_by(status=Status.WAITING).order_by(
        Queue.created_at.asc()).limit(10).all()
    return jsonify({
        "now_serving": serving.ticket_number if serving else "---",
        "call_count": serving.call_count if serving else 0,
        "waiting": [t[0] for t in waiting_list]
    })


@app.route('/staff/analytics')
@roles_required(Roles.STAFF)
def staff_analytics():
    # 1. Daily Volume
    today = datetime.now(timezone.utc).date()
    daily_count = Queue.query.filter(func.date(Queue.created_at) == today).count()

    # 2. Monthly Efficiency (Avg Service Time placeholder)
    # 3. Forecast Logic: Group by day of week for the last 30 days
    last_month = datetime.now(timezone.utc) - timedelta(days=30)
    busy_days = db.session.query(
        func.extract('dow', Booking.scheduled_time).label('day'),
        func.count(Booking.id).label('count')
    ).filter(Booking.scheduled_time >= last_month).group_by('day').all()

    # 4. Status Breakdown
    status_counts = db.session.query(Booking.status, func.count(Booking.id)).group_by(Booking.status).all()

    return render_template('staff_analytics.html',
                           daily_count=daily_count,
                           busy_days=busy_days,
                           status_counts=status_counts)


@app.route('/staff/reports/export')
@roles_required(Roles.STAFF)
def export_reports():
    si = StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'Customer', 'Vehicle', 'Service', 'Status', 'Date'])

    bookings = Booking.query.all()
    for b in bookings:
        cw.writerow(
            [b.ref_id, b.customer.full_name, b.vehicle.plate_number, b.service_type, b.status, b.scheduled_time])

    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=qbms_report.csv"
    output.headers["Content-type"] = "text/csv"
    return output


@app.route('/staff/analytics/data')
@roles_required(Roles.STAFF)
def analytics_data():
    # 1. Forecast: Average bookings per day of week (Requirement 9)
    # 0=Sunday, 1=Monday...
    dow_counts = db.session.query(
        extract('dow', Booking.scheduled_time).label('dow'),
        func.count(Booking.id)
    ).group_by('dow').all()

    # 2. Volume: Monthly Growth (Requirement 7)
    monthly_stats = db.session.query(
        func.to_char(Booking.scheduled_time, 'Month').label('month'),
        func.count(Booking.id)
    ).group_by('month').all()

    return jsonify({
        "forecast": {str(int(d[0])): d[1] for d in dow_counts},
        "monthly": {d[0].strip(): d[1] for d in monthly_stats}
    })


@app.route('/staff/settings/update', methods=['POST'])
@roles_required(Roles.STAFF)
def update_settings():
    for key in ['MAIL_HOST_USER', 'MAIL_HOST_PASSWORD', 'SMS_API_KEY', 'SMS_SENDER_ID']:
        val = request.form.get(key)
        setting = SystemSetting.query.filter_by(key=key).first()
        if not setting:
            setting = SystemSetting(key=key, value=val)
            db.session.add(setting)
        else:
            setting.value = val
    db.session.commit()
    flash("System Credentials Updated Successfully", "success")
    return redirect(url_for('staff_settings'))

@app.route('/staff/records')
@roles_required(Roles.STAFF)
def staff_records():
    bookings = Booking.query.order_by(Booking.scheduled_time.desc()).all()
    return render_template('staff_records.html', bookings=bookings, title="Service Records")


@app.route('/staff/settings')
@roles_required(Roles.STAFF)
def staff_settings():
    # Pass empty settings or load from DB
    settings = {'MAIL_HOST_USER': '', 'MAIL_HOST_PASSWORD': '', 'SMS_API_KEY': ''}
    return render_template('staff_settings.html', settings=settings, title="System Settings")

@app.route('/staff/users', methods=['GET', 'POST'])
@roles_required(Roles.STAFF)
def manage_users():
    if request.method == 'POST':
        try:
            hashed_pw = generate_password_hash(request.form.get('password'), method='scrypt')
            new_user = User(
                username=request.form.get('username').strip(),
                email=request.form.get('email').strip().lower(),
                password_hash=hashed_pw,
                full_name=request.form.get('full_name'),
                role=request.form.get('role'), # 'staff' or 'customer'
                company_name=request.form.get('company_name', 'Coolaire Staff')
            )
            db.session.add(new_user)
            db.session.commit()
            flash(f"New {new_user.role} account created for {new_user.full_name}", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Error: Username or Email already exists.", "danger")
        return redirect(url_for('manage_users'))

    all_users = User.query.order_by(User.role.desc()).all()
    return render_template('staff_users.html', users=all_users, title="User Management")
@app.route('/staff/categories', methods=['GET', 'POST'])
@roles_required(Roles.STAFF)
def manage_categories():
    if request.method == 'POST':
        name = request.form.get('category_name').strip()
        if name:
            try:
                new_cat = ServiceCategory(name=name)
                db.session.add(new_cat)
                db.session.commit()
                flash(f"Category '{name}' added successfully.", "success")
            except IntegrityError:
                db.session.rollback()
                flash("Category already exists.", "warning")
        return redirect(url_for('manage_categories'))

    categories = ServiceCategory.query.order_by(ServiceCategory.name.asc()).all()
    return render_template('staff_categories.html', categories=categories, title="Service Config")

@app.route('/staff/categories/delete/<int:id>')
@roles_required(Roles.STAFF)
def delete_category(id):
    cat = db.session.get(ServiceCategory, id)
    if cat:
        db.session.delete(cat)
        db.session.commit()
        flash("Category removed.", "info")
    return redirect(url_for('manage_categories'))


@app.route('/api/analytics/forecast')
@roles_required(Roles.STAFF)
def get_forecast_data():
    # We calculate the average number of bookings per day of the week
    # 0=Sunday, 1=Monday, etc.
    stats = db.session.query(
        func.extract('dow', Booking.scheduled_time).label('day_of_week'),
        func.count(Booking.id).label('count')
    ).group_by('day_of_week').all()

    # Convert to a format the Chart can read [Sun, Mon, Tue, Wed, Thu, Fri, Sat]
    # We divide the total count by 4 (assuming we look at 4 weeks of data) to get 'Average'
    forecast_map = {int(day): (count / 4) for day, count in stats}
    ordered_data = [forecast_map.get(i, 0) for i in range(7)]

    return jsonify({"forecast": ordered_data})

@app.route('/display')
def public_display():
    return render_template('display.html') # This is the TV code we updated earlier

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
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))