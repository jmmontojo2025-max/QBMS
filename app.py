import os
import random
import string
import requests
import smtplib
import threading  # For background tasks
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, extract
from flask_wtf.csrf import CSRFProtect

from functools import wraps

from requests_oauthlib import OAuth1




app = Flask(__name__)
# IMPORTANT: Use a strong, random key from environment for production.
# The default here is for local development ONLY.
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "your_super_secret_dev_key_change_me_in_prod")
raw_db_url = os.environ.get("DATABASE_URL",
                            "postgresql://postgres.mguajchtxgunyfzotipa:Itadmin36155912030*@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres")
app.config['SQLALCHEMY_DATABASE_URI'] = raw_db_url.replace("postgres://", "postgresql://")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'


def roles_required(*roles):
    def wrapper(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))

            # This checks the 'role' column in your User table
            if current_user.role not in ['admin', 'staff', 'super_admin']:
                # This automatically sends them to your custom 403.html
                abort(403)
            return f(*args, **kwargs)

        return decorated_view

    return wrapper


class NetSuiteConnector:
    def __init__(self):
        settings = {s.key: s.value for s in SystemSetting.query.all()}
        self.account = settings.get('NS_ACCOUNT_ID', '').replace('_', '-')
        self.consumer_key = settings.get('NS_CONSUMER_KEY')
        self.consumer_secret = settings.get('NS_CONSUMER_SECRET')
        self.token_id = settings.get('NS_TOKEN_ID')
        self.token_secret = settings.get('NS_TOKEN_SECRET')
        self.base_url = f"https://{self.account.lower()}.restlets.api.netsuite.com/app/site/hosting/restlet.nl"

    def get_job_order(self, search_value):
        if not self.consumer_key: return None
        auth = OAuth1(self.consumer_key, self.consumer_secret, self.token_id, self.token_secret,
                      realm=self.account.replace('-', '_'), signature_method='HMAC-SHA256')
        params = {'script': 'customscript_bas_job_search', 'deploy': '1', 'searchValue': search_value}
        try:
            res = requests.get(self.base_url, auth=auth, params=params, timeout=10)
            return res.json() if res.status_code == 200 else None
        except: return None

class NotificationLog(db.Model):
    __tablename__ = 'notification_logs'
    id = db.Column(db.Integer, primary_key=True)
    queue_id = db.Column(db.Integer, db.ForeignKey('queues.id'))
    recipient = db.Column(db.Text)
    channel = db.Column(db.String(10))
    status = db.Column(db.String(20))
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Link back to queue for the UI
    queue = db.relationship('Queue', backref='logs')


# Add this model if you haven't already to keep track of customer units
class Vehicle(db.Model):
    __tablename__ = 'vehicles'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    plate_number = db.Column(db.String(50))
    model_description = db.Column(db.String(150))
    related_bookings = db.relationship('Booking', backref='associated_vehicle', cascade="all, delete-orphan")



# --- MODELS ---
class Location(db.Model):
    __tablename__ = 'locations'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True)
    code = db.Column(db.String(10), unique=True)

    @property
    def is_online(self):
        threshold = datetime.now(timezone.utc) - timedelta(minutes=2)
        active_user = User.query.filter(
            User.current_loc_id == self.id,
            User.role.in_(['staff', 'admin']),
            User.last_seen >= threshold
        ).first()
        return active_user is not None

# A branch is online if any staff/admin was seen in the last 2 minutes at this ID


class Technician(db.Model):
    __tablename__ = 'technicians'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))
    is_active = db.Column(db.Boolean, default=True)
    branch = db.relationship('Location', backref='technicians')
    is_present = db.Column(db.Boolean, default=True)


class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='customer')  # 'customer', 'staff', 'admin'
    full_name = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    company_name = db.Column(db.String(150))
    bookings = db.relationship('Booking', backref='customer', lazy=True, cascade="all, delete-orphan") # THIS IS THE EXISTING BACKREF
    last_seen = db.Column(db.DateTime)
    current_loc_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=True)
    is_approved = db.Column(db.Boolean, default=False)  # NEW: Default is False
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    tin_number = db.Column(db.String(20))  # Tax ID
    business_permit = db.Column(db.String(50))  # Legal Permit or Accreditation
    vehicles = db.relationship('Vehicle', backref='owner', lazy=True, cascade="all, delete-orphan")
    is_rejected = db.Column(db.Boolean, default=False) # NEW: Soft delete flag




class ServiceCategory(db.Model):
    __tablename__ = 'service_categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True)




class Booking(db.Model):
    __tablename__ = 'bookings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True) # Changed to nullable
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'), nullable=True)
    plate_number = db.Column(db.String(50))
    guest_name = db.Column(db.String(150)) # Added this
    service_type = db.Column(db.String(255))
    service_location = db.Column(db.String(50), default='In-Plant') # Added this
    status = db.Column(db.String(20), default='pending')
    scheduled_time = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    ref_id = db.Column(db.String(10), unique=True)
    queue_records = db.relationship('Queue', back_populates='booking', cascade="all, delete-orphan")


    # NO EXPLICIT 'customer' relationship here, it's created by the backref in User model
    # location = db.relationship('Location', backref='bookings_at_location') # This can stay or be removed if 'location' is sufficient from backref in Location model
    location = db.relationship('Location', backref='bookings') # Use existing backref from Location if available, or define here if not.

    def __init__(self, **kwargs):
        super(Booking, self).__init__(**kwargs)
        if not self.ref_id:
            # Generates a unique 4-digit number for Kiosk Check-in
            self.ref_id = ''.join(random.choices(string.digits, k=4))

queue_technicians = db.Table('queue_technicians',
    db.Column('queue_id', db.Integer, db.ForeignKey('queues.id'), primary_key=True),
    db.Column('technician_id', db.Integer, db.ForeignKey('technicians.id'), primary_key=True)
)


class Queue(db.Model):
    __tablename__ = 'queues'
    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(20))
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=True)
    status = db.Column(db.String(20), default='waiting')
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    call_count = db.Column(db.Integer, default=0)

    # Relationships
    location = db.relationship('Location', backref='queue_entries')
    booking = db.relationship('Booking', back_populates='queue_records')

    # NEW PLURAL RELATIONSHIP
    assigned_techs = db.relationship('Technician', secondary=queue_technicians, backref='tasks')


class SystemSetting(db.Model):
    __tablename__ = 'system_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True)
    value = db.Column(db.Text)


class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    action = db.Column(db.String(100))  # e.g., "Work Started", "Ticket Expired"
    details = db.Column(db.Text)        # e.g., "Assigned Tech A to Ticket BAS-101"
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Relationships
    performer = db.relationship('User', backref='logs')
    location = db.relationship('Location')


class RolePermission(db.Model):
    __tablename__ = 'role_permissions'
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(20))
    feature_key = db.Column(db.String(50))
    is_allowed = db.Column(db.Boolean, default=False)


# --- STAFF OPERATIONS (Require Staff/Admin Role and Selected Location) ---

# Helper to check staff access and location
def require_staff_location():
    # Allow Super Admin, Admin, and Staff
    if current_user.is_authenticated and current_user.role not in ['staff', 'admin', 'super_admin']:
        abort(403)

    if not current_user.is_authenticated:
        return redirect(url_for('login'))

        # If you are management but haven't picked a hub, go to the picker
    if current_user.role in ['staff', 'admin', 'super_admin'] and 'loc_id' not in session:
        flash("Hub initialization required.", "info")
        return redirect(url_for('select_branch_for_staff'))

    return None

def permission_required(feature_key):
    def wrapper(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))

            if not check_permission(feature_key):
                abort(403)

            return f(*args, **kwargs)

        return decorated_view

    return wrapper


# This makes the permission check available in all HTML templates
@app.context_processor
def inject_permissions():
    def has_perm(feature):
        if not current_user.is_authenticated: return False
        if current_user.role == 'super_admin': return True  # Super Admin sees all

        perm = RolePermission.query.filter_by(
            role=current_user.role,
            feature_key=feature
        ).first()
        return perm.is_allowed if perm else False

    return dict(has_perm=check_permission)


def check_permission(feature_key):
    """The only function that checks the Matrix."""
    if not current_user.is_authenticated:
        return False

    # 1. Super Admin Bypass
    if current_user.role == 'super_admin':
        return True

    # 2. Case-Insensitive Check
    # This ensures 'Staff' and 'staff' both work
    user_role = current_user.role.lower().strip()

    perm = RolePermission.query.filter_by(
        role=user_role,
        feature_key=feature_key.lower().strip()
    ).first()

    return perm.is_allowed if perm else False


# --- HELPERS ---
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def send_sms(phone, message):
    api_key_setting = SystemSetting.query.filter_by(key='SMS_API_KEY').first()
    if api_key_setting and api_key_setting.value and phone:
        try:
            requests.post("https://semaphore.co/api/v4/messages", data={
                'apikey': api_key_setting.value, 'number': phone, 'message': message, 'sendername': 'COOLAIRE'
            }, timeout=5)
        except Exception as e:
            app.logger.error(f"SMS API Error: {e}")  # Use app.logger for production
            print(f"SMS API Error: {e}")  # For local debugging


# --- GENERAL ROUTES (Entry Points and Core Navigation) ---

# The root path should now always go to the customer login form.
@app.route('/')
def root_redirect_to_login():
    if current_user.is_authenticated:
        if current_user.role == 'customer':
            return redirect(url_for('dashboard'))
        # ADD 'super_admin' here
        elif current_user.role in ['staff', 'admin', 'super_admin']:
            if 'loc_id' in session:
                return redirect(url_for('staff_panel'))
            else:
                return redirect(url_for('select_branch_for_staff'))
    return redirect(url_for('login'))


# This route is specifically for staff to select their branch *after* logging in.
@app.route('/select-branch-for-staff')
@login_required
def select_branch_for_staff():
    # ADD 'super_admin' to this check
    if current_user.role not in ['staff', 'admin', 'super_admin']:
        flash("Management clearance required.", "danger")
        return redirect(url_for('login'))

    if 'loc_id' in session:
        return redirect(url_for('staff_panel'))

    locations = Location.query.all()
    return render_template('location_select.html', locs=locations)


# This route sets the branch in the session for the logged-in user.
@app.route('/set-branch/<int:loc_id>')
@login_required
def set_branch(loc_id):
    loc = db.session.get(Location, loc_id)
    if not loc:
        flash("Invalid hub selected.", "danger")
        return redirect(url_for('select_branch_for_staff'))

    session['loc_id'] = loc.id
    session['location_name'] = loc.name
    session['location_code'] = loc.code
    session.modified = True

    # Update Database tracking
    current_user.current_loc_id = loc.id
    db.session.commit()

    # Ensure Super Admin goes to the correct panel
    if current_user.role in ['staff', 'admin', 'super_admin']:
        return redirect(url_for('staff_panel'))

    return redirect(url_for('dashboard'))



# --- STAFF OPERATIONS ---

@app.route('/staff')
@login_required
@roles_required('super_admin', 'admin', 'staff')
def staff_panel():
    if 'loc_id' not in session:
        return redirect(url_for('select_branch_for_staff'))

    loc_id = session.get('loc_id')

    # Optimization: Only run cleanup once an hour per session to prevent hangs
    last_cleanup = session.get('last_cleanup')
    now = datetime.now(timezone.utc)

    if not last_cleanup or (now - datetime.fromisoformat(last_cleanup)).total_seconds() > 3600:
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        stale = Queue.query.filter(Queue.location_id == loc_id, Queue.status == 'waiting',
                                   Queue.created_at < today_start).all()
        for t in stale:
            t.status = 'expired'
            if t.booking: t.booking.status = 'missed'
        db.session.commit()
        session['last_cleanup'] = now.isoformat()

    # Optimized Data Fetching
    waiting = Queue.query.filter_by(location_id=loc_id, status='waiting').order_by(Queue.created_at.asc()).all()
    serving = Queue.query.options(
        db.joinedload(Queue.assigned_techs),
        db.selectinload(Queue.logs)
    ).filter_by(location_id=loc_id, status='serving').all()

    all_techs = Technician.query.filter_by(location_id=loc_id, is_active=True).order_by(Technician.name.asc()).all()
    available_techs = [t for t in all_techs if t.is_present]

    return render_template('staff.html',
                           waiting_tickets=waiting,
                           serving_list=serving,
                           technicians=available_techs,
                           roster=all_techs,
                           title="Live Console")


@app.route('/staff/start-work/<int:q_id>', methods=['POST'])
@login_required
@permission_required('start-work') # Add this!
def start_work(q_id):
    loc_id = session.get('loc_id')
    tech_ids = request.form.getlist('technician_ids')
    q = db.session.get(Queue, q_id)

    if q and q.location_id == loc_id and tech_ids:
        techs = Technician.query.filter(Technician.id.in_(tech_ids)).all()
        q.assigned_techs = techs
        q.status = 'serving'
        q.start_time = datetime.now(timezone.utc)
        db.session.commit()

        log_action("Dispatch", f"Ticket {q.ticket_number} sent to floor. Team: {', '.join([t.name for t in techs])}")
        if q.booking: notify_customer(q.booking.customer, q.booking.plate_number, 'serving', q.id, q.ticket_number)

    return redirect(url_for('staff_panel'))


@app.route('/staff/recall-ticket/<int:q_id>')
@login_required
@permission_required('recall-ticket') # Add this!
def recall_ticket(q_id):
    q = db.session.get(Queue, q_id)
    if q:
        q.call_count += 1
        db.session.commit()
        log_action("TV Recall", f"Ticket {q.ticket_number} called again on monitor.")
    return redirect(url_for('staff_panel'))


# --- CUSTOMER PORTAL LOGIN (Strictly Customers Only) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    # 1. HANDLE USERS ALREADY LOGGED IN
    if current_user.is_authenticated:
        # If staff/admin/super_admin is already logged in and hits this page
        if current_user.role in ['staff', 'admin', 'super_admin']:
            if 'loc_id' in session:
                return redirect(url_for('staff_panel'))
            else:
                return redirect(url_for('select_branch_for_staff'))
        # If regular customer is already logged in
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username').strip()
        password = request.form.get('password')

        user = User.query.filter_by(username=username).first()

        # 2. VALIDATE CREDENTIALS
        if user and check_password_hash(user.password_hash, password):

            # SECURITY GATE A: If the user is STAFF/ADMIN/SUPER_ADMIN, BLOCK THEM HERE
            # Management must use /staff/login to prevent session mixing
            if user.role in ['staff', 'admin', 'super_admin']:
                flash("Node Access Restricted: Management and Staff must use the Internal Terminal to sign in.",
                      "danger")
                return redirect(url_for('login'))

            # SECURITY GATE B: Prevent Bogus/Unverified Customer Access
            if not user.is_approved:
                app.logger.warning(f"Access Denied: Unverified node '{username}' attempted login.")
                flash(
                    "Identity Verification Pending: Your account is currently under review by our security team. Access is restricted until business credentials (TIN/Permit) are verified.",
                    "warning")
                return redirect(url_for('login'))

            # 3. SUCCESSFUL CUSTOMER LOGIN
            login_user(user)

            # Update telemetry
            user.last_seen = datetime.now(timezone.utc)
            db.session.commit()

            flash(f"Welcome back, {user.full_name}!", "success")
            return redirect(url_for('dashboard'))

        # 4. FAILED LOGIN
        flash("Authentication Failed: Invalid Node ID or Security Key.", "danger")

    return render_template('login.html')

@app.route('/staff/users/approve/<int:user_id>')
@login_required
def approve_user(user_id):
    if current_user.role != ['admin','super_admin']: abort(403)
    u = db.session.get(User, user_id)
    if u:
        u.is_approved = True
        db.session.commit()
        # OPTIONAL: notify_customer(u, "N/A", "approved")
        flash(f"Access granted for {u.full_name}.", "success")
    return redirect(url_for('staff_users'))


# --- STAFF TERMINAL LOGIN (Strictly Staff Only) ---
@app.route('/staff/login', methods=['GET', 'POST'])
def staff_login():
    if current_user.is_authenticated and current_user.role in ['staff', 'admin', 'super_admin']:
        return redirect(url_for('select_branch_for_staff'))

    if request.method == 'POST':
        u = User.query.filter_by(username=request.form.get('username')).first()
        if u and check_password_hash(u.password_hash, request.form.get('password')):
            if u.role in ['staff', 'admin', 'super_admin']:
                login_user(u)
                return redirect(url_for('select_branch_for_staff'))
            else:
                flash("Access Denied: Only staff nodes can use this terminal.", "danger")
    return render_template('login_staff.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')

        # 1. Check if Username already exists
        existing_username = User.query.filter_by(username=username).first()
        if existing_username:
            flash("Registration Failed: This username is already taken.", "danger")
            return redirect(url_for('register'))

        # 2. Check if Email already exists (This fixes your specific error)
        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            flash("Registration Failed: An account with this email already exists.", "danger")
            return redirect(url_for('register'))

        # 3. If everything is clear, hash the password and create the user
        hashed_pw = generate_password_hash(password)
        new_user = User(
            username=username,
            password_hash=hashed_pw,
            full_name=request.form.get('full_name'),
            phone=request.form.get('phone'),
            email=email,
            company_name=request.form.get('company_name'),
            tin_number=request.form.get('tin_number'),
            business_permit=request.form.get('business_permit'),
            role='customer',
            is_approved=False # Locked until staff verifies in Verify Center
        )

        try:
            db.session.add(new_user)
            db.session.commit()

            # >>> ADD THIS TRIGGER HERE <<<
            try:
                notify_customer(
                    user=new_user,
                    plate_number="N/A",
                    status_type='registration_pending'
                )
            except Exception as mail_err:
                app.logger.error(f"Initial Reg Email Failed: {mail_err}")
            # >>> END OF TRIGGER <<<

            flash("Registration successful! Please check your email for the next steps.", "success")
            return redirect(url_for('login'))

        except Exception as e:
            db.session.rollback()
            return "Database Error", 500

    return render_template('register.html')


@app.route('/dashboard')
@login_required
def dashboard():
    # 1. ROLE SECURITY: Redirect any Staff/Admin nodes to the Command Console
    # This ensures Super Admins and Staff don't see the Customer UI
    if current_user.role in ['super_admin', 'admin', 'staff']:
        return redirect(url_for('staff_panel'))

    # 2. DATA RETRIEVAL: Fetch full deployment history for this specific client
    # We use selectinload for queue_records to match our recent model update (Performance)
    bookings = Booking.query.options(db.selectinload(Booking.queue_records)).filter_by(user_id=current_user.id) \
        .order_by(Booking.scheduled_time.desc()).all()

    # 3. FORECASTING ENGINE: Group upcoming 'Pending' arrivals by Hub Location
    # This helps the client see their scheduled load for the week
    now = datetime.now(timezone.utc)
    forecast_results = db.session.query(
        Location.name,
        func.count(Booking.id).label('unit_count')
    ).join(Booking).filter(
        Booking.user_id == current_user.id,
        Booking.scheduled_time >= now,
        Booking.status == 'pending'
    ).group_by(Location.name).all()

    # Convert results to a dictionary for the UI cards
    branch_forecast = {name: count for name, count in forecast_results}

    # 4. TELEMETRY FOCUS: Check if the user is tracking a specific asset
    booking_id = request.args.get('booking_id')
    active_booking = None

    if booking_id:
        # SECURITY FIX: Ensure the requested booking ID actually belongs to THIS customer
        active_booking = Booking.query.filter_by(id=booking_id, user_id=current_user.id).first()

        # If ID is invalid or belongs to another company, ignore it
        if not active_booking:
            flash("Security Alert: Unauthorized asset tracking attempt.", "danger")
            return redirect(url_for('dashboard'))

    # 5. RENDER
    return render_template('dashboard.html',
                           service_history=bookings,
                           all_active_bookings=bookings,
                           booking=active_booking,
                           branch_forecast=branch_forecast,
                           title="Fleet Dashboard")


from datetime import datetime, timezone


@app.route('/book', methods=['GET', 'POST'])
@login_required
def book():
    if request.method == 'POST':
        # ... logic for vehicle selection ...
        new_booking = Booking(
            user_id=current_user.id,
            location_id=request.form.get('location_id'),
            plate_number=request.form.get('plate_number'),
            service_type=request.form.get('product'),
            service_location=request.form.get('service_location'),  # Choice from Radio buttons
            scheduled_time=datetime.fromisoformat(request.form.get('time')),
            status='pending'
        )
        db.session.add(new_booking)
        db.session.commit()
        return redirect(url_for('dashboard'))

    categories = ServiceCategory.query.all()
    locations = Location.query.all()
    return render_template('book.html', categories=categories, locations=locations)


@app.route('/staff/locations', methods=['GET', 'POST'])
@login_required
@permission_required('locations')
def staff_locations():
    # Handle POST request to add a new location
    if request.method == 'POST':
        # MODIFIED: Allow both 'admin' and 'staff' to add locations
        # if current_user.role not in ['admin', 'staff']: # Original condition
        #    flash("You do not have administrative privileges to add locations.", "danger")
        #    return redirect(url_for('staff_locations'))

        name = request.form.get('name').strip()
        code = request.form.get('code').strip().upper()

        if not name or not code:
            flash("Branch Name and Code are required.", "danger")
        else:
            existing_location = Location.query.filter((Location.name == name) | (Location.code == code)).first()
            if existing_location:
                flash("A branch with this name or code already exists.", "danger")
            else:
                new_location = Location(name=name, code=code)
                db.session.add(new_location)
                db.session.commit()
                flash(f"Branch '{name}' ({code}) added successfully!", "success")
        return redirect(url_for('staff_locations'))

    # Handle GET request to display all locations
    all_locations = Location.query.order_by(Location.name).all()
    # Pass the current user's role to the template
    return render_template('staff_locations.html', locations=all_locations, title="Manage Branches", current_user_role=current_user.role)


@app.route('/staff/locations/edit/<int:loc_id>', methods=['GET', 'POST'])
@login_required
@permission_required('locations_edit')

def edit_location(loc_id):

    location_to_edit = db.session.get(Location, loc_id)
    if not location_to_edit:
        flash("Location not found.", "danger")
        return redirect(url_for('staff_locations'))

    if request.method == 'POST':
        name = request.form.get('name').strip()
        code = request.form.get('code').strip().upper()

        if not name or not code:
            flash("Branch Name and Code are required.", "danger")
        else:
            existing_location = Location.query.filter(
                ((Location.name == name) | (Location.code == code)) & (Location.id != loc_id)
            ).first()
            if existing_location:
                flash("Another branch with this name or code already exists.", "danger")
            else:
                location_to_edit.name = name
                location_to_edit.code = code
                db.session.commit()
                flash(f"Branch '{name}' ({code}) updated successfully.", "success")
        return redirect(url_for('staff_locations'))

    # GET request: Display the edit form
    return render_template('staff_location_edit.html', location=location_to_edit, title="Edit Branch")

@app.route('/staff/locations/delete/<int:loc_id>')
@login_required
@permission_required('locations')

def delete_location(loc_id):

    location_to_delete = db.session.get(Location, loc_id)
    if not location_to_delete:
        flash("Location not found.", "danger")
        return redirect(url_for('staff_locations'))

    try:
        has_bookings = Booking.query.filter_by(location_id=loc_id).first()
        has_queues = Queue.query.filter_by(location_id=loc_id).first()
        has_technicians = Technician.query.filter_by(location_id=loc_id).first()

        if has_bookings or has_queues or has_technicians:
            flash("Cannot delete branch: It is linked to existing bookings, queues, or technicians.", "danger")
            return redirect(url_for('staff_locations'))

        db.session.delete(location_to_delete)
        db.session.commit()
        flash(f"Branch '{location_to_delete.name}' deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error deleting location {loc_id}: {e}")
        flash("An error occurred while deleting the branch.", "danger")

    return redirect(url_for('staff_locations'))


@login_required
def staff_panel():
    app.logger.info(f"Accessing staff_panel for {current_user.username}")
    redirect_response = require_staff_location()
    if redirect_response:
        app.logger.warning(f"Redirecting {current_user.username} from staff_panel: {redirect_response.location}")
        return redirect_response

    loc_id = session.get('loc_id')  # Guaranteed to be set by now
    app.logger.info(f"Staff Panel loc_id: {loc_id} for {current_user.username}")

    waiting = Queue.query.filter_by(location_id=loc_id, status='waiting').order_by(Queue.created_at.asc()).all()
    serving = Queue.query.filter_by(location_id=loc_id, status='serving').all()
    techs = Technician.query.filter_by(location_id=loc_id, is_active=True).all()

    app.logger.info(f"Rendering staff.html for {current_user.username} (Location: {session.get('loc_name')})")
    return render_template('staff.html', waiting_tickets=waiting, serving_list=serving, technicians=techs,
                           title="Live Console")


@app.route('/staff/complete-work/<int:q_id>')
@login_required
@permission_required('complete-work')


def complete_work(q_id):
    loc_id = session.get('loc_id')
    q = db.session.get(Queue, q_id)

    if q and q.location_id == loc_id:
        q.status = 'done'
        q.end_time = datetime.now(timezone.utc)
        if q.booking:
            q.booking.status = 'done'
        db.session.commit()
        log_action("Work Completed", f"Ticket {q.ticket_number} marked as ready for release.")

        # AUTOMATIC NOTIFICATION
        if q.booking and q.booking.customer:
            # Look for this line:
            notify_customer(
                user=q.booking.customer,
                plate_number=q.booking.plate_number,
                status_type='done',
                queue_id=q.id,  # <--- CHANGE THIS from q.ticket_number to q.id
                ticket_number=q.ticket_number  # Pass this separately if needed
            )

    return redirect(url_for('staff_panel'))


@app.route('/staff/records')
@login_required
@permission_required('records') # <--- Use the Matrix Key
def staff_records():

    loc_id = session.get('loc_id')
    if not loc_id:
        flash("Please select a branch first.", "warning")
        return redirect(url_for('select_branch_for_staff'))

    # This pulls every ticket ever created for this branch, newest first
    all_records = Queue.query.filter_by(location_id=loc_id) \
        .order_by(Queue.created_at.desc()).all()

    return render_template('staff_records.html',
                           records=all_records,
                           title="Master Service Ledger")


@app.route('/staff/settings', methods=['GET', 'POST'])
@login_required
@permission_required('settings')

def staff_settings():
    redirect_response = require_staff_location()
    if redirect_response: return redirect_response  # This route is general settings, not loc_id specific, but still staff-only.

    if request.method == 'POST':
        keys = ['SMS_API_KEY', 'MAIL_HOST_USER', 'MAIL_HOST_PASSWORD']
        for key in keys:
            val = request.form.get(key)
            if val is not None:
                setting = SystemSetting.query.filter_by(key=key).first()
                if setting:
                    setting.value = val
                else:
                    db.session.add(SystemSetting(key=key, value=val))
        db.session.commit()
        flash("System configuration updated successfully.", "success")
        return redirect(url_for('staff_settings'))

    settings_dict = {
        'SMS_API_KEY': '',
        'MAIL_HOST_USER': '',
        'MAIL_HOST_PASSWORD': ''
    }
    existing_settings = SystemSetting.query.all()
    for s in existing_settings:
        settings_dict[s.key] = s.value

    return render_template('staff_settings.html', settings=settings_dict, title="System Config")


# --- STAFF SERVICE CATEGORY MANAGEMENT ---
@app.route('/staff/categories', methods=['GET', 'POST'])
@login_required
@permission_required('staff_categories')
def staff_categories():
    redirect_response = require_staff_location()
    if redirect_response: return redirect_response

    if request.method == 'POST':
        name = request.form.get('category_name')
        if name:
            exists = ServiceCategory.query.filter_by(name=name).first()
            if not exists:
                db.session.add(ServiceCategory(name=name))
                db.session.commit()
                flash(f"Category '{name}' added.", "success")
            else:
                flash("Category already exists.", "warning")
        return redirect(url_for('staff_categories'))

    cats = ServiceCategory.query.all()
    return render_template('staff_categories.html', categories=cats, title="Service Types")


@app.route('/staff/categories/delete/<int:id>')
@login_required
@permission_required('staff_categories')
def delete_category(id):
    redirect_response = require_staff_location()
    if redirect_response: return redirect_response

    cat = db.session.get(ServiceCategory, id)
    if cat:
        try:
            db.session.delete(cat)
            db.session.commit()
            flash("Category deleted successfully.", "success")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error deleting category {id}: {e}")
            flash("Could not delete category. It might be linked to existing bookings.", "danger")
    return redirect(url_for('staff_categories'))


# --- KIOSK & TV (Public-facing, require location in session) ---

@app.route('/kiosk')
def kiosk():
    loc_id_param = request.args.get('loc_id')
    if loc_id_param:
        loc = db.session.get(Location, int(loc_id_param))
        if loc:
            session['loc_id'] = loc.id
            session['location_name'] = loc.name
            session['location_code'] = loc.code
            session.modified = True

    if 'loc_id' not in session:
        return redirect(url_for('select_branch_for_staff'))

    # Get categories for the Walk-in Modal
    categories = ServiceCategory.query.order_by(ServiceCategory.name).all()
    return render_template('kiosk.html', categories=categories)



@app.route('/check-in', methods=['POST'])
@csrf.exempt
def check_in():
    loc_id = session.get('loc_id')
    ref_code = request.form.get('booking_id')

    # Look for the booking by the 4-digit code
    booking = Booking.query.filter_by(ref_id=ref_code, status='pending').first()

    if booking:
        today = datetime.now(timezone.utc).date()
        count = Queue.query.filter_by(location_id=loc_id).filter(func.date(Queue.created_at) == today).count()

        # Format: MKT-A-101
        ticket_no = f"{session.get('location_code', 'CCI')}-A-{101 + count}"

        new_q = Queue(
            ticket_number=ticket_no,
            location_id=loc_id,
            booking_id=booking.id,
            status='waiting'
        )
        booking.status = 'arrived'
        db.session.add(new_q)
        db.session.commit()

        return jsonify({"status": "success", "ticket": ticket_no, "q_id": new_q.id})

    return jsonify({"status": "error", "message": "Invalid Reference Code"}), 404


@app.route('/walk-in', methods=['POST'])
@csrf.exempt
def walk_in():
    loc_id = session.get('loc_id')
    guest_name = request.form.get('customer_name')
    plate_number = request.form.get('plate_number', '').strip().upper()
    service_type = request.form.get('service_type')

    if not all([guest_name, plate_number, service_type]):
        return jsonify({"status": "error", "message": "All fields are required"}), 400

    try:
        # Create Booking as In-Plant Guest
        new_booking = Booking(
            user_id=None,
            location_id=loc_id,
            plate_number=plate_number,
            guest_name=guest_name,
            service_type=service_type,
            service_location='In-Plant', # Walk-ins are ALWAYS In-Plant
            status='arrived',
            ref_id='W-' + ''.join(random.choices(string.digits, k=4))
        )
        db.session.add(new_booking)
        db.session.flush()

        # Generate Ticket
        today = datetime.now(timezone.utc).date()
        count = Queue.query.filter_by(location_id=loc_id).filter(func.date(Queue.created_at) == today).count()
        ticket_no = f"{session.get('location_code', 'CCI')}-W-{101 + count}"

        new_q = Queue(
            ticket_number=ticket_no,
            location_id=loc_id,
            booking_id=new_booking.id,
            status='waiting'
        )
        db.session.add(new_q)
        db.session.commit()

        return jsonify({"status": "success", "ticket": ticket_no, "q_id": new_q.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500



# THIS IS THE ROUTE THAT TRIGGERS THE PRINTER TEMPLATE
@app.route('/print-ticket/<int:q_id>')
def print_ticket_view(q_id):
    queue_item = db.session.get(Queue, q_id)
    if not queue_item:
        return "Ticket not found", 404
    return render_template('print_ticket.html', ticket=queue_item)


@app.route('/tv')
def tv_display():
    if 'loc_id' not in session:
        flash("TV display requires a branch to be selected.", "warning")
        # Placeholder: redirect to login or a dedicated *public* branch selector
        return redirect(url_for('login'))

    loc_id = session.get('loc_id')  # Now guaranteed to be set
    loc = db.session.get(Location, loc_id)
    if not loc:  # Should not happen if loc_id in session is valid
        flash("Selected location not found.", "danger")
        return redirect(url_for('login'))  # Redirect as fallback

    return render_template('tv.html', location=loc)


# --- ANALYTICS API (Staff/Admin Only, Location Dependent) ---

@app.route('/staff/analytics')
@login_required
@permission_required('analytics')

def staff_analytics():
    loc_id = session.get('loc_id')
    now = datetime.now(timezone.utc)

    # 1. Monthly Momentum
    this_month = Queue.query.filter(Queue.location_id == loc_id,
                                    extract('month', Queue.created_at) == now.month).count()
    last_month = Queue.query.filter(Queue.location_id == loc_id,
                                    extract('month', Queue.created_at) == (now.month - 1)).count()
    momentum = int(((this_month - last_month) / last_month * 100)) if last_month > 0 else 100

    # 2. Tech Efficiency
    tech_stats = db.session.query(
        Technician.name,
        func.count(queue_technicians.c.queue_id).label('total_jobs')
    ).join(queue_technicians).join(Queue).filter(Queue.location_id == loc_id, Queue.status == 'done').group_by(
        Technician.name).all()

    return render_template('staff_analytics.html',
                           daily_count=this_month,  # Simplified for example
                           monthly_count=this_month,
                           momentum=momentum,
                           tech_stats=tech_stats,
                           forecast=5,  # Logic for upcoming bookings
                           title="Business Intelligence")


@app.route('/api/analytics/forecast')
@login_required
def analytics_forecast():
    loc_id = session.get('loc_id')
    if not loc_id:
        return jsonify({"forecast": [0, 0, 0, 0, 0, 0, 0]})

    # Define the lookback period (e.g., last 30 days) to calculate averages
    lookback_date = datetime.now(timezone.utc) - timedelta(days=30)

    # Query: Count arrivals grouped by Day of Week (0-6)
    # extract('dow') returns 0 for Sunday, 1 for Monday, etc.
    results = db.session.query(
        extract('dow', Queue.created_at).label('day_of_week'),
        func.count(Queue.id).label('arrival_count')
    ).filter(
        Queue.location_id == loc_id,
        Queue.created_at >= lookback_date
    ).group_by('day_of_week').all()

    # Initialize a list for Sun-Sat (7 days)
    # We divide the total count by 4.2 (approx weeks in 30 days) to get the "Average Expectation"
    forecast_data = [0] * 7
    for day_index, count in results:
        # Convert to integer index and calculate average
        idx = int(day_index)
        # Round to 1 decimal place for the chart
        forecast_data[idx] = round(count / 4.2, 1)

    return jsonify({"forecast": forecast_data})


@app.route('/api/get-latest-queue')
def get_latest_queue():
    # Priority 1: URL Argument (Best for TVs/Kiosks)
    # Priority 2: Session
    loc_id = request.args.get('loc_id') or session.get('loc_id')

    if not loc_id:
        return jsonify({"now_serving": "---", "waiting": []})

    serving = Queue.query.filter_by(location_id=loc_id, status='serving').order_by(Queue.start_time.desc()).first()
    waiting = Queue.query.filter_by(location_id=loc_id, status='waiting').order_by(Queue.created_at.asc()).limit(
        5).all()

    return jsonify({
        "now_serving": serving.ticket_number if serving else "---",
        "call_count": serving.call_count if serving else 0,
        "waiting": [t.ticket_number for t in waiting]
    })


from datetime import datetime, timezone  # Ensure these are imported at the top


@app.route('/staff/users', methods=['GET', 'POST'])
@login_required
@permission_required('users')

def staff_users():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')  # super_admin, admin, staff, customer
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        company_name = request.form.get('company_name')

        # 1. Validation: Ensure all fields are filled
        if not all([username, password, role, full_name, email]):
            flash("Enrollment Error: All fields are required to provision a new node.", "danger")
            return redirect(url_for('staff_users'))

        # 2. Check for Duplicate Identity
        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Conflict Error: Username or Email is already registered in the Global Directory.", "danger")
            return redirect(url_for('staff_users'))

        # 3. Create the New User object
        hashed_pw = generate_password_hash(password)
        new_user = User(
            username=username,
            password_hash=hashed_pw,
            role=role,
            full_name=full_name,
            email=email,
            company_name=company_name if role == 'customer' else "Coolaire Consolidated Inc.",
            is_approved=True  # Users created manually by Super Admin are auto-approved
        )

        try:
            db.session.add(new_user)
            db.session.commit()

            # 4. Audit the action
            log_action("Identity Provisioned", f"Super Admin created new {role} node: {username}")

            flash(f"Success: Identity for {full_name} has been provisioned as {role.upper()}.", "success")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Enrollment Error: {e}")
            flash("System Error: Could not save record to database.", "danger")

        return redirect(url_for('staff_users'))

    # GET Logic: Fetch all nodes for the Directory Table
    all_users = User.query.order_by(User.role.asc(), User.full_name.asc()).all()

    return render_template(
        'staff_users.html',
        users=all_users,
        title="Identity Registry",
        now_utc=datetime.now(timezone.utc)
    )


# Removed the redundant /staff/settings/update route.
# The POST logic for settings is handled directly in staff_settings.

@app.before_request
def update_last_seen():
    if current_user.is_authenticated and current_user.role in ['staff', 'admin', 'super_admin']:
        now = datetime.now(timezone.utc)

        # Check if we actually need to update (Throttling)
        # Prevents database "Hangs" by limiting updates to once per minute
        last_update = current_user.last_seen

        if not last_update or (now - last_update.replace(tzinfo=timezone.utc)).total_seconds() > 60:
            try:
                # Use a separate execution to avoid locking the whole user object if possible
                current_user.last_seen = now
                if 'loc_id' in session:
                    current_user.current_loc_id = session.get('loc_id')

                db.session.commit()
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Background Telemetry Error: {e}")


# ADD THIS: Ensures database connections are released after every request
@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()


@app.route('/staff/technicians', methods=['GET', 'POST'])
@login_required
@permission_required('technicians')
def staff_technicians():

    loc_id = session.get('loc_id')
    if not loc_id:
        flash("Please select a branch first.", "warning")
        return redirect(url_for('select_branch_for_staff'))

    if request.method == 'POST':
        name = request.form.get('tech_name')
        if name:
            new_tech = Technician(name=name, location_id=loc_id)
            db.session.add(new_tech)
            db.session.commit()
            flash(f"Technician {name} added to this hub.", "success")
        return redirect(url_for('staff_technicians'))

    # Only show technicians assigned to the CURRENT branch in session
    techs = Technician.query.filter_by(location_id=loc_id).all()
    return render_template('staff_technicians.html', technicians=techs, title="Manage Technicians")

@app.route('/staff/technicians/delete/<int:id>')
@login_required
@permission_required('technicians')
def delete_technician(id):
    tech = db.session.get(Technician, id)
    # Security check: Ensure tech belongs to current staff's branch
    if tech and tech.location_id == session.get('loc_id'):
        db.session.delete(tech)
        db.session.commit()
        flash("Technician removed.", "success")
    return redirect(url_for('staff_technicians'))


@app.route('/staff/expire-ticket/<int:q_id>')
@login_required
def expire_ticket(q_id):
    loc_id = session.get('loc_id')
    q = db.session.get(Queue, q_id)

    if q and q.location_id == loc_id:
        q.status = 'expired'
        # If it was linked to a booking, reset the booking status too
        if q.booking:
            q.booking.status = 'missed'

        db.session.commit()
        flash(f"Ticket {q.ticket_number} marked as Expired/No-Show.", "warning")

    return redirect(url_for('staff_panel'))


@app.route('/staff/revert-ticket/<int:q_id>')
@login_required
def revert_ticket(q_id):
    loc_id = session.get('loc_id')
    q = db.session.get(Queue, q_id)

    # Security: Ensure ticket exists and belongs to this branch
    if q and q.location_id == loc_id:
        # Move back to waiting
        q.status = 'waiting'

        # If it was a booking, move it back to pending
        if q.booking:
            q.booking.status = 'pending'

        db.session.commit()
        flash(f"Ticket {q.ticket_number} has been restored to the active queue.", "success")
    else:
        flash("Error: Could not restore ticket.", "danger")

    return redirect(url_for('staff_records'))


import threading
from flask import current_app


def notify_customer(user, plate_number, status_type, queue_id=None, ticket_number=None):
    """
    Unified engine to send SMS and Email automatically in the background.
    Prevents the staff dashboard from freezing during SMTP/SMS API calls.
    """
    # 1. Capture necessary data BEFORE starting the thread.
    # Flask 'session' and 'request' are NOT available inside a background thread.
    loc_name = session.get('location_name', 'Coolaire Service Center')
    root_url = request.url_root
    user_id = user.id  # Pass ID to fetch a fresh object in the thread

    # Get the actual app instance to pass into the thread
    app_instance = current_app._get_current_object()

    def run_notifications(app_ctx, u_id, l_name, base_url):
        with app_ctx.app_context():
            # Fetch fresh user record within this thread's database session
            db_user = db.session.get(User, u_id)
            if not db_user:
                return

            # Fetch Settings
            settings = {s.key: s.value for s in SystemSetting.query.all()}

            # 2. Define Messages and Context based on status_type
            is_account_msg = False
            if status_type == 'account_approved':
                subject = "Account Activated: Access Granted to Coolaire Portal"
                msg_text = f"Welcome to Coolaire, {db_user.full_name}! Your identity verification is complete. You can now log in to the Partner Portal."
                is_account_msg = True
            elif status_type == 'registration_pending':
                subject = "Registration Received: Pending Verification"
                msg_text = f"Thank you for registering, {db_user.full_name}. We have received your application for {db_user.company_name}. You will receive another email once your account is activated."
                is_account_msg = True
            elif status_type == 'booked':
                subject = f"Deployment Confirmed: Unit {plate_number}"
                msg_text = f"Coolaire: Your deployment for unit {plate_number} has been logged. Please arrive at the hub on your scheduled window."
            elif status_type == 'serving':
                subject = f"Service Started: Unit {plate_number}"
                msg_text = f"Coolaire Update: Your unit {plate_number} (Ticket {ticket_number}) is now being serviced."
            elif status_type == 'done':
                subject = f"Service Complete: Unit {plate_number}"
                msg_text = f"Coolaire Update: Great news! Service for unit {plate_number} is complete. Please proceed to the release bay."
            elif status_type == 'expired':
                subject = f"Appointment Update: Unit {plate_number}"
                msg_text = f"Coolaire Update: We noticed you weren't able to make it for unit {plate_number}. Your ticket has been marked as expired/no-show."
            else:
                return

            # --- PART A: SEND SMS ---
            sms_key = settings.get('SMS_API_KEY')
            if sms_key and db_user.phone:
                sms_log = NotificationLog(queue_id=queue_id, recipient=db_user.phone, channel='sms')
                try:
                    requests.post("https://semaphore.co/api/v4/messages", data={
                        'apikey': sms_key, 'number': db_user.phone, 'message': msg_text, 'sendername': 'COOLAIRE'
                    }, timeout=10)
                    sms_log.status = 'success'
                except Exception as e:
                    sms_log.status = 'failed'
                    sms_log.error_message = str(e)
                db.session.add(sms_log)

            # --- PART B: SEND EMAIL ---
            mail_user = settings.get('MAIL_HOST_USER', 'appointments@coolaireconsolidated.com')
            mail_pass = settings.get('MAIL_HOST_PASSWORD')
            mail_server = settings.get('MAIL_SERVER', 'mail.coolaireconsolidated.com')
            mail_port = int(settings.get('MAIL_PORT', 465))

            if mail_user and mail_pass and db_user.email:
                email_log = NotificationLog(queue_id=queue_id, recipient=db_user.email, channel='email')
                try:
                    msg = MIMEMultipart('related')
                    msg['From'] = f"Coolaire Appointments <{mail_user}>"
                    msg['To'] = db_user.email
                    msg['Subject'] = subject

                    login_url = f"{base_url}login"

                    # 3. Dynamic Info Box Styling
                    if is_account_msg:
                        info_box_html = f"""
                        <div style="margin-top: 30px; padding: 20px; background: #f8fafc; border-radius: 6px; border-left: 4px solid #76b82a;">
                            <p style="margin: 0; font-size: 14px;"><strong>Account Type:</strong> Client Portal Access</p>
                            <p style="margin: 5px 0 0 0; font-size: 14px;"><strong>Company:</strong> {db_user.company_name}</p>
                            <p style="margin: 15px 0 0 0; font-size: 14px;">
                                <a href="{login_url}" style="background: #002d72; color: white; padding: 10px 20px; text-decoration: none; border-radius: 4px; font-weight: bold; display: inline-block;">
                                    LOG IN TO PORTAL
                                </a>
                            </p>
                        </div>"""
                    else:
                        info_box_html = f"""
                        <div style="margin-top: 30px; padding: 20px; background: #f8fafc; border-radius: 6px; border-left: 4px solid #002d72;">
                            <p style="margin: 0; font-size: 14px;"><strong>Asset Plate:</strong> {plate_number}</p>
                            <p style="margin: 5px 0 0 0; font-size: 14px;"><strong>Location:</strong> {l_name}</p>
                            {f'<p style="margin: 5px 0 0 0; font-size: 14px;"><strong>Reference:</strong> {ticket_number}</p>' if ticket_number else ''}
                        </div>"""

                    # 4. Final HTML Template
                    html = f"""
                    <html>
                        <body style="font-family: 'Segoe UI', Arial, sans-serif; color: #1a1a1a; line-height: 1.6; margin: 0; padding: 0;">
                            <div style="max-width: 600px; margin: auto; border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden;">
                                <div style="background: #002d72; padding: 30px; text-align: center;">
                                    <img src="cid:logo" alt="Coolaire Logo" style="height: 60px; width: auto;">
                                    <p style="color: #76b82a; margin: 10px 0 0 0; font-weight: bold; text-transform: uppercase; font-size: 11px; letter-spacing: 1px;">
                                        Number one cold chain supplier in the Philippines
                                    </p>
                                </div>
                                <div style="padding: 40px 30px;">
                                    <h2 style="color: #002d72; margin-top: 0; font-size: 20px;">System Notification</h2>
                                    <p>Dear <strong>{db_user.full_name}</strong>,</p>
                                    <p style="font-size: 16px; color: #334155;">{msg_text}</p>
                                    {info_box_html}
                                </div>
                                <div style="background: #f1f5f9; padding: 20px; text-align: center; font-size: 11px; color: #64748b;">
                                    This is an automated notification from the Coolaire QBMS System.<br>
                                    &copy; {datetime.now(timezone.utc).year} Coolaire Consolidated Inc.
                                </div>
                            </div>
                        </body>
                    </html>"""
                    msg.attach(MIMEText(html, 'html'))

                    # 5. Embed Logo
                    logo_path = os.path.join(app_ctx.root_path, 'static', 'logo.png')
                    if os.path.exists(logo_path):
                        with open(logo_path, 'rb') as f:
                            img = MIMEImage(f.read())
                            img.add_header('Content-ID', '<logo>')
                            msg.attach(img)

                    # 6. Execute Send
                    with smtplib.SMTP_SSL(mail_server, mail_port) as server:
                        server.login(mail_user, mail_pass)
                        server.sendmail(mail_user, db_user.email, msg.as_string())
                    email_log.status = 'success'

                except Exception as e:
                    email_log.status = 'failed'
                    email_log.error_message = str(e)

                db.session.add(email_log)

            # Final commit for the logs within the thread
            db.session.commit()

    # Start the thread and return control to the main app immediately
    threading.Thread(target=run_notifications, args=(app_instance, user_id, loc_name, root_url)).start()


@app.route('/staff/notifications')
@login_required
@permission_required('notifications') # <--- Use the Matrix Key
def staff_notifications():
    loc_id = session.get('loc_id')

    # Use outerjoin so we see "Account Notifications" as well as "Ticket Notifications"
    # We filter for logs linked to this hub OR logs with no hub (Global/Account)
    logs = NotificationLog.query.outerjoin(Queue).filter(
        (Queue.location_id == loc_id) | (NotificationLog.queue_id == None)
    ).order_by(NotificationLog.created_at.desc()).limit(100).all()

    return render_template('staff_notifications.html', logs=logs, title="Messaging Audit")


@app.route('/staff/verify-center')
@login_required
@permission_required('verify_center')
def verify_center():

    # Get only users who are NOT yet approved
    pending = User.query.filter_by(is_approved=False).order_by(User.created_at.asc()).all()
    # Get recently approved for reference
    history = User.query.filter_by(is_approved=True).order_by(User.created_at.desc()).limit(10).all()

    return render_template('staff_verify_center.html',
                           pending=pending,
                           history=history,
                           title="Identity Verification Center")


@app.route('/staff/verify-action/<int:user_id>/<string:action>')
@login_required
@permission_required('verify_center')
def verify_action(user_id, action):


    user_to_verify = db.session.get(User, user_id)
    if not user_to_verify:
        flash("System Error: The requested user record no longer exists.", "danger")
        return redirect(url_for('verify_center'))

    if action == 'approve':
        log_action("Identity Approved",
                   f"Access granted to {user_to_verify.full_name} ({user_to_verify.company_name}).")
        # 2. AUTHORIZE: Set approved to True and ensure they are removed from archive
        user_to_verify.is_approved = True
        user_to_verify.is_rejected = False
        db.session.commit()

        # 3. AUTOMATIC NOTIFICATION: Use the 'account_approved' logic
        try:
            notify_customer(
                user=user_to_verify,
                plate_number="N/A",
                status_type='account_approved'
            )
            flash(f"Success: Access GRANTED and Activation Email sent to {user_to_verify.full_name}.", "success")
        except Exception as e:
            app.logger.error(f"Approval Notification Failed for {user_to_verify.email}: {e}")
            flash(f"Account approved for {user_to_verify.full_name}, but the activation email failed to send. Please check SMTP settings.", "warning")

    elif action == 'reject':
        # 4. SOFT DELETE: Move bogus/competitor identity to Archive instead of purging
        user_to_verify.is_approved = False
        user_to_verify.is_rejected = True
        db.session.commit()

        app.logger.info(f"Identity Blocked: {user_to_verify.full_name} was rejected by {current_user.username}")
        flash(f"Identity REJECTED: {user_to_verify.full_name} has been moved to the Rejected Archive.", "warning")

    # Redirect logic: If rejecting, stay in verify center.
    # If approving from the Archive, this will take you back to the center.
    return redirect(url_for('verify_center'))


@app.route('/staff/archive')
@login_required
@permission_required('staff_archived')

def staff_archive():

    # View users marked as rejected
    rejected_users = User.query.filter_by(is_rejected=True).order_by(User.created_at.desc()).all()
    return render_template('staff_archive.html', users=rejected_users, title="Rejected Identity Archive")


@app.route('/staff/archive/purge/<int:user_id>')
@login_required
@permission_required('staff_archived')
def purge_user(user_id):
    if current_user.role != 'admin': abort(403)
    u = db.session.get(User, user_id)
    if u:
        db.session.delete(u)  # This is the PERMANENT delete
        db.session.commit()
        flash("Record permanently purged from the system.", "danger")
    return redirect(url_for('staff_archive'))

def log_action(action, details):
    """ Records an entry into the audit trail for the current hub """
    try:
        new_log = AuditLog(
            location_id=session.get('loc_id'),
            user_id=current_user.id if current_user.is_authenticated else None,
            action=action,
            details=details
        )
        db.session.add(new_log)
        db.session.commit()
    except Exception as e:
        app.logger.error(f"Audit Log Failed: {e}")
        db.session.rollback()


@app.route('/staff/audit-trail')
@login_required
@permission_required('audit')
def staff_audit_trail():

    loc_id = session.get('loc_id')
    # Fetch logs for THIS location only
    logs = AuditLog.query.filter_by(location_id=loc_id).order_by(AuditLog.created_at.desc()).limit(500).all()

    return render_template('staff_audit_trail.html', logs=logs, title="Hub Audit Trail")


@app.route('/staff/global-bookings')
@login_required
@permission_required('global_bookings')
def global_bookings():
    # 2. DATA RETRIEVAL: Fetch ALL bookings in the organization
    # We use .options(db.joinedload(...)) to pull Location, Customer, and Vehicle info in
    # one single query. This is critical for BAS-Node performance.
    try:
        all_bookings = Booking.query.options(
            db.joinedload(Booking.location),
            db.joinedload(Booking.customer),         # 'customer' is the backref from User.bookings
            db.joinedload(Booking.associated_vehicle) # 'associated_vehicle' is the backref from Vehicle.related_bookings
        ).order_by(Booking.scheduled_time.desc()).all()

    except Exception as e:
        app.logger.error(f"Global Ledger Query Error: {e}")
        flash("System Error: Could not retrieve global deployment data.", "danger")
        all_bookings = []

    # 3. RENDER: Pass the data to your forecasting template
    return render_template(
        'staff_global_bookings.html',
        bookings=all_bookings,
        title="Global Deployment Ledger"
    )

@app.route('/staff/technician/toggle/<int:tech_id>')
@login_required
@permission_required('technicians')

def toggle_tech_presence(tech_id):

    loc_id = session.get('loc_id')
    tech = db.session.get(Technician, tech_id)

    if tech and tech.location_id == loc_id:
        tech.is_present = not tech.is_present  # Flip status
        db.session.commit()

        status_text = "PRESENT" if tech.is_present else "ABSENT"
        log_action("Attendance Change", f"Technician {tech.name} marked as {status_text}")
        flash(f"{tech.name} is now marked as {status_text}.", "info")

    return redirect(url_for('staff_panel'))

@app.route('/staff/save-notes/<int:q_id>', methods=['POST'])
@login_required
def save_job_notes(q_id):
    q = db.session.get(Queue, q_id)
    if q and q.location_id == session.get('loc_id'):
        q.internal_notes = request.form.get('notes')
        db.session.commit()
        flash("Notes updated.", "success")
    return redirect(url_for('staff_panel'))


def roles_required(*roles):
    def wrapper(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))

            # SUPER ADMIN BYPASS: Always allow super_admin
            if current_user.role == 'super_admin':
                return f(*args, **kwargs)

            # Check if user has one of the allowed roles
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return decorated_view
    return wrapper


# CUSTOM ERROR HANDLER FOR 403
@app.errorhandler(403)
def forbidden_error(error):
    return render_template('errors/403.html'), 403

@app.route('/api/netsuite/verify/<string:search_val>')
@login_required
def api_verify_netsuite(search_val):
    ns = NetSuiteConnector()
    data = ns.get_job_order(search_val)
    if data and data.get('status') == 'success':
        return jsonify(data)
    return jsonify({"status": "error", "message": "No record found"})

@app.route('/staff/sync-job-order', methods=['POST'])
@login_required
def sync_job_order():
    jo_number = request.form.get('jo_number')
    loc_id = session.get('loc_id')
    ns = NetSuiteConnector()
    ns_data = ns.get_job_order(jo_number)

    if ns_data and ns_data.get('status') == 'success':
        # Check if already exists
        existing = Booking.query.filter_by(ref_id=ns_data['jo_number']).first()
        if existing:
            flash(f"Job Order {jo_number} is already synced.", "info")
            return redirect(url_for('staff_panel'))

        # Auto-match client or create placeholder
        customer = User.query.filter(User.company_name.ilike(ns_data['client_name'])).first()
        if not customer:
            flash(f"Client {ns_data['client_name']} not found in QBMS. Please register them first.", "danger")
            return redirect(url_for('staff_panel'))

        new_booking = Booking(
            user_id=customer.id, location_id=loc_id, plate_number=ns_data['plate_number'],
            service_type=ns_data['erp_status'], status='pending', ref_id=ns_data['jo_number']
        )
        db.session.add(new_booking)
        db.session.commit()
        log_action("ERP Sync", f"Imported JO {jo_number} from NetSuite.")
        flash(f"Successfully imported JO {jo_number}", "success")
    else:
        flash("Could not find that JO# in NetSuite.", "danger")
    return redirect(url_for('staff_panel'))

@app.route('/staff/permissions', methods=['GET', 'POST'])
@login_required
@permission_required('settings')
def manage_permissions():
    roles = ['admin', 'staff', 'customer']
    features = [
        ('analytics', 'Site Analytics'),
        ('notifications', 'Messaging Audit'),
        ('records', 'Service History'),
        ('audit', 'Security Audit Trail'),
        ('verify_center', 'Identity Verification'),
        ('users', 'User Registry'),
        ('settings', 'System Settings'),
        ('global_bookings', 'Global Ledger'),
        ('technicians', 'Manage Technicians'), # Add this
        ('locations', 'Manage Branches')      # Add this
    ]

    if request.method == 'POST':
        # Clear old and save new
        for role in roles:
            for feat_key, feat_name in features:
                allowed = request.form.get(f"{role}_{feat_key}") == 'on'
                perm = RolePermission.query.filter_by(role=role, feature_key=feat_key).first()
                if perm:
                    perm.is_allowed = allowed
                else:
                    db.session.add(RolePermission(role=role, feature_key=feat_key, is_allowed=allowed))
        db.session.commit()
        flash("Permission Matrix Updated Successfully.", "success")
        return redirect(url_for('manage_permissions'))

    # Load existing permissions into a nested dict for the UI
    current_perms = {}
    for p in RolePermission.query.all():
        if p.role not in current_perms: current_perms[p.role] = {}
        current_perms[p.role][p.feature_key] = p.is_allowed

    return render_template('staff_permissions.html',
                           roles=roles,
                           features=features,
                           current_perms=current_perms,
                           title="Access Control Matrix")







@app.route('/logout')
def logout():
    logout_user()
    session.clear()  # Clear session completely on logout
    # After logout, return to the customer login page as the entry point.
    return redirect(url_for('login'))


if __name__ == '__main__':
    with app.app_context():
        # 1. Create all tables based on your Models
        db.create_all()

        try:
            # 2. Use a safer way to check for missing columns
            inspector = db.inspect(db.engine)
            existing_columns = [c['name'] for c in inspector.get_columns('bookings')]

            # Check and add 'plate_number'
            if 'plate_number' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN plate_number VARCHAR(50)'))
                db.session.commit()
                print("--- Database Updated: Added plate_number ---")

            # Check and add 'guest_name' (for Walk-ins)
            if 'guest_name' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN guest_name VARCHAR(150)'))
                db.session.commit()
                print("--- Database Updated: Added guest_name ---")

            # Check and add 'service_location' (In-Plant/Out-Plant)
            if 'service_location' not in existing_columns:
                db.session.execute(
                    db.text("ALTER TABLE bookings ADD COLUMN service_location VARCHAR(50) DEFAULT 'In-Plant'"))
                db.session.commit()
                print("--- Database Updated: Added service_location ---")

        except Exception as e:
            print(f"--- Database Migration Note: {e} ---")
            db.session.rollback()

    # 3. Run the app on Port 5000
    app.run(debug=True, port=5000)