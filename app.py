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

import json  # Ensure this is at the top of app.py
from sqlalchemy import or_, and_, func

# Define Philippine Time (UTC+8)
PHT = timezone(timedelta(hours=8))

def get_pht_now():
    return datetime.now(PHT)

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
            if current_user.role not in ['admin', 'coordinator', 'advisor', 'super_admin']:
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
        except:
            return None


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
    capacity = db.Column(db.Integer, default=20)  # Add this line
    kiosk_last_seen = db.Column(db.DateTime)
    tv_last_seen = db.Column(db.DateTime)

    @property
    def kiosk_online(self):
        if not self.kiosk_last_seen: return False
        # Ensure both are UTC before comparing
        now = datetime.now(timezone.utc)
        last_seen = self.kiosk_last_seen.replace(
            tzinfo=timezone.utc) if self.kiosk_last_seen.tzinfo is None else self.kiosk_last_seen
        return (now - last_seen).total_seconds() < 120

    @property
    def tv_online(self):
        if not self.tv_last_seen: return False
        now = datetime.now(timezone.utc)
        last_seen = self.tv_last_seen.replace(
            tzinfo=timezone.utc) if self.tv_last_seen.tzinfo is None else self.tv_last_seen
        return (now - last_seen).total_seconds() < 120

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
    bookings = db.relationship('Booking', backref='customer', lazy=True,
                               cascade="all, delete-orphan")  # THIS IS THE EXISTING BACKREF
    last_seen = db.Column(db.DateTime)
    current_loc_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=True)
    is_approved = db.Column(db.Boolean, default=False)  # NEW: Default is False
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    tin_number = db.Column(db.String(20))  # Tax ID
    business_permit = db.Column(db.String(50))  # Legal Permit or Accreditation
    vehicles = db.relationship('Vehicle', backref='owner', lazy=True, cascade="all, delete-orphan")
    is_rejected = db.Column(db.Boolean, default=False)  # NEW: Soft delete flag


class ServiceCategory(db.Model):
    __tablename__ = 'service_categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True)


class Booking(db.Model):
    __tablename__ = 'bookings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)  # Changed to nullable
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'), nullable=True)
    plate_number = db.Column(db.String(50))
    guest_name = db.Column(db.String(150))  # Added this
    service_type = db.Column(db.String(255))
    service_location = db.Column(db.String(50), default='In-Plant')  # Added this
    status = db.Column(db.String(20), default='pending')
    scheduled_time = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    ref_id = db.Column(db.String(10), unique=True)
    queue_records = db.relationship('Queue', back_populates='booking', cascade="all, delete-orphan")
    job_order = db.Column(db.String(50), nullable=True)
    std_repair_hours = db.Column(db.Float, default=0.0)

    # NO EXPLICIT 'customer' relationship here, it's created by the backref in User model
    # location = db.relationship('Location', backref='bookings_at_location') # This can stay or be removed if 'location' is sufficient from backref in Location model
    location = db.relationship('Location',
                               backref='bookings')  # Use existing backref from Location if available, or define here if not.

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
    materials_used = db.Column(db.Text)  # Add this line

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
    details = db.Column(db.Text)  # e.g., "Assigned Tech A to Ticket BAS-101"
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
            if not current_user.is_authenticated: return redirect(url_for('login'))
            if not check_permission(feature_key): abort(403)
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
    if not current_user.is_authenticated: return False
    if current_user.role == 'super_admin': return True

    user_role = current_user.role.lower().strip()
    perm = RolePermission.query.filter_by(role=user_role, feature_key=feature_key.lower().strip()).first()
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
        elif current_user.role in ['admin', 'coordinator', 'advisor', 'super_admin']:
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
@roles_required('super_admin', 'admin', 'coordinator', 'advisor')  # Updated roles
def staff_panel():
    if 'loc_id' not in session: return redirect(url_for('select_branch_for_staff'))
    loc_id = session.get('loc_id')

    # 1. Fetch Fresh Hub Details (Crucial for Green/Red status)
    current_location = db.session.get(Location, loc_id)
    if current_location:
        db.session.refresh(current_location)  # Force pull latest timestamps from Supabase

    max_capacity = current_location.capacity if current_location else 20

    # FETCH SERVICES FOR DROPDOWNS
    categories = ServiceCategory.query.order_by(ServiceCategory.name.asc()).all()

    # 2. Workfloor & Queue Logic
    serving = Queue.query.options(db.joinedload(Queue.assigned_techs), db.joinedload(Queue.booking)).filter_by(
        location_id=loc_id, status='serving').all()
    waiting = Queue.query.filter_by(location_id=loc_id, status='waiting').order_by(Queue.created_at.asc()).all()

    # 3. BUILD THE BUSY MAP (Mapping Service to Tech via Ticket)
    busy_map = {}
    for q in serving:
        # Check if the queue has a booking (to get the service type)
        service_name = q.booking.service_type if q.booking else "General Service"
        plate_no = q.booking.plate_number if q.booking else "WALK-IN"

        for t_assigned in q.assigned_techs:
            # Map the technician ID to the specific details of the ticket they are holding
            busy_map[t_assigned.id] = {
                'ticket': q.ticket_number,
                'plate': plate_no,
                'service': service_name
            }

    # 4. Personnel
    all_techs = Technician.query.options(db.selectinload(Technician.tasks).joinedload(Queue.booking)).filter_by(
        location_id=loc_id, is_active=True).order_by(Technician.name.asc()).all()
    available_techs = [t for t in all_techs if t.is_present and t.id not in busy_map]

    # 5. Occupancy
    current_occupancy = len(serving) + len(waiting)
    capacity_percent = int((current_occupancy / max_capacity) * 100) if max_capacity > 0 else 0

    return render_template('staff.html',
                           current_location=current_location,  # <--- ADDED THIS
                           categories=categories,
                           waiting_tickets=waiting,
                           serving_list=serving,
                           technicians=available_techs,
                           roster=all_techs,
                           busy_map=busy_map,
                           max_capacity=max_capacity,
                           current_occupancy=current_occupancy,
                           capacity_percent=capacity_percent,
                           title="Live Console")


@app.route('/admin/workflow')
@login_required
@roles_required('super_admin', 'admin')
def admin_workflow():
    all_locations = Location.query.order_by(Location.name.asc()).all()
    categories = ServiceCategory.query.order_by(ServiceCategory.name.asc()).all()
    hub_data = []

    for loc in all_locations:
        active_tickets = Queue.query.options(db.joinedload(Queue.assigned_techs), db.joinedload(Queue.booking)).filter(
            Queue.location_id == loc.id,
            or_(Queue.status == 'serving',
                and_(Queue.status == 'done', or_(Queue.materials_used == None, Queue.materials_used == '')))
        ).order_by(Queue.created_at.asc()).all()

        loc_techs = Technician.query.filter_by(location_id=loc.id, is_active=True).order_by(Technician.name.asc()).all()

        # Build a busy map for this specific hub
        busy_map = {}
        for q in active_tickets:
            for t in q.assigned_techs:
                busy_map[t.id] = {
                    'ticket': q.ticket_number,
                    'service': q.booking.service_type if q.booking else "Service"
                }

        hub_data.append({
            'info': loc,
            'tickets': active_tickets,
            'techs': loc_techs,
            'busy_map': busy_map,  # Detailed status mapping
            'ticket_count': len(active_tickets)
        })

    return render_template('admin_workflow.html', hub_data=hub_data, categories=categories, title="Global Workflow")


@app.route('/staff/save-materials/<int:q_id>', methods=['POST'])
@login_required
def save_materials(q_id):
    loc_id = session.get('loc_id')
    q = db.session.get(Queue, q_id)

    if q and q.location_id == loc_id:
        materials = request.form.get('materials_list')
        q.materials_used = materials
        db.session.commit()
        flash(f"Materials list updated for Ticket {q.ticket_number}", "success")

    return redirect(request.referrer or url_for('staff_panel'))


@app.route('/staff/manual-checkin', methods=['POST'])
@login_required
def staff_manual_checkin():
    # 1. Determine Location: Check if an Admin-specified loc_id was sent,
    # otherwise fallback to the session loc_id (for regular staff).
    admin_loc_id = request.form.get('admin_loc_id')
    loc_id = admin_loc_id if admin_loc_id else session.get('loc_id')

    # Fetch the specific location object to get its Code (for the ticket prefix)
    target_loc = db.session.get(Location, loc_id)
    loc_code = target_loc.code if target_loc else 'CCI'

    manifest_data = request.form.get('staff_manifest_data')
    if not manifest_data:
        return redirect(request.referrer or url_for('staff_panel'))

    manifest = json.loads(manifest_data)

    try:
        for item in manifest:
            # 2. Create Booking with JO and SRH
            new_booking = Booking(
                user_id=None,
                location_id=loc_id,
                plate_number=item['plate'].strip().upper(),
                guest_name=f"[PHONE] {item['client']}",
                service_type=item['service'],
                service_location=item['site'],
                job_order=item.get('jo'),
                std_repair_hours=float(item.get('srh', 0)) if item.get('srh') else 0.0,
                status='arrived'
            )
            db.session.add(new_booking)
            db.session.flush()

            # 3. Generate Ticket
            today = datetime.now(timezone.utc).date()
            count = Queue.query.filter_by(location_id=loc_id).filter(func.date(Queue.created_at) == today).count()
            ticket_no = f"{loc_code}-M-{101 + count}"

            # 4. Add to Queue
            new_q = Queue(ticket_number=ticket_no, location_id=loc_id, booking_id=new_booking.id, status='waiting')
            db.session.add(new_q)

        db.session.commit()
        flash(f"Successfully deployed to {target_loc.name}.", "success")
    except Exception as e:
        db.session.rollback()
        flash("System error in bulk check-in.", "danger")

    return redirect(request.referrer or url_for('staff_panel'))


@app.route('/staff/start-work/<int:q_id>', methods=['POST'])
@login_required
@permission_required('start-work')
def start_work(q_id):
    loc_id = session.get('loc_id')
    tech_ids = request.form.getlist('technician_ids')
    q = db.session.get(Queue, q_id)

    # New inputs from the dispatch form
    jo_number = request.form.get('job_order')
    srh_value = request.form.get('std_repair_hours', 0)

    if q and q.location_id == loc_id and tech_ids:
        # Update booking details if they exist
        if q.booking:
            if jo_number: q.booking.job_order = jo_number
            if srh_value: q.booking.std_repair_hours = float(srh_value)

        techs = Technician.query.filter(Technician.id.in_(tech_ids)).all()
        q.assigned_techs = techs
        q.status = 'serving'
        db.session.commit()

        log_action("Dispatch", f"Ticket {q.ticket_number} sent to floor.")

        if q.booking and q.booking.customer:
            notify_customer(q.booking.customer, q.booking.plate_number, 'serving', q.id, q.ticket_number)

    return redirect(url_for('staff_panel'))


@app.route('/staff/recall-ticket/<int:q_id>')
@login_required
@permission_required('recall-ticket')  # Add this!
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
    if current_user.role != ['admin', 'super_admin']: abort(403)
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
    if current_user.is_authenticated and current_user.role in ['admin', 'coordinator', 'advisor', 'super_admin']:
        return redirect(url_for('select_branch_for_staff'))

    if request.method == 'POST':
        u = User.query.filter_by(username=request.form.get('username')).first()
        if u and check_password_hash(u.password_hash, request.form.get('password')):
            if u.role in ['admin', 'coordinator', 'advisor', 'super_admin']:  # Updated
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
            is_approved=False  # Locked until staff verifies in Verify Center
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
        # Get the JSON manifest from the hidden field
        manifest_data = request.form.get('manifest_data')
        if not manifest_data:
            flash("No assets added to manifest.", "warning")
            return redirect(url_for('book'))

        manifest = json.loads(manifest_data)

        try:
            for item in manifest:
                # 1. Handle New Vehicle Provisioning
                v_id = item.get('vehicle_id')
                if v_id == 'new':
                    new_v = Vehicle(
                        user_id=current_user.id,
                        plate_number=item['new_plate'].strip().upper(),
                        model_description=item['new_model']
                    )
                    db.session.add(new_v)
                    db.session.flush()
                    v_id = new_v.id

                # 2. Create Booking
                new_booking = Booking(
                    user_id=current_user.id,
                    location_id=item['location_id'],
                    vehicle_id=v_id,
                    plate_number=item['plate_display'].split(' ')[0],
                    service_type=item['product'],
                    service_location=item['service_location'],
                    scheduled_time=datetime.fromisoformat(item['time']),
                    status='pending'
                )
                db.session.add(new_booking)

            db.session.commit()
            flash(f"Successfully deployed {len(manifest)} assets.", "success")
            return redirect(url_for('dashboard'))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Booking error: {e}")
            flash("Error processing manifest.", "danger")

    categories = ServiceCategory.query.all()
    locations = Location.query.all()
    # Pull user's vehicles for the dropdown
    user_vehicles = Vehicle.query.filter_by(user_id=current_user.id).all()
    return render_template('book.html', categories=categories, locations=locations, vehicles=user_vehicles)


@app.route('/staff/locations', methods=['GET', 'POST'])
@login_required
@permission_required('locations')
def staff_locations():
    if request.method == 'POST':
        name = request.form.get('name').strip()
        code = request.form.get('code').strip().upper()
        capacity = request.form.get('capacity', 20)  # Get capacity from form

        new_location = Location(name=name, code=code, capacity=int(capacity))
        db.session.add(new_location)
        db.session.commit()
        flash(f"Branch '{name}' added with capacity {capacity}.", "success")
        return redirect(url_for('staff_locations'))

    all_locations = Location.query.order_by(Location.name).all()
    return render_template('staff_locations.html', locations=all_locations)


@app.route('/staff/locations/edit/<int:loc_id>', methods=['GET', 'POST'])
@login_required
@permission_required('locations_edit')
def edit_location(loc_id):
    loc = db.session.get(Location, loc_id)
    if request.method == 'POST':
        loc.name = request.form.get('name')
        loc.code = request.form.get('code').upper()
        loc.capacity = int(request.form.get('capacity', 20))  # Update capacity
        db.session.commit()
        flash("Branch updated successfully.", "success")
        return redirect(url_for('staff_locations'))
    return render_template('staff_location_edit.html', location=loc)


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
    current_occupancy = len(serving)
    techs = Technician.query.filter_by(location_id=loc_id, is_active=True).all()

    app.logger.info(f"Rendering staff.html for {current_user.username} (Location: {session.get('loc_name')})")
    return render_template('staff.html', waiting_tickets=waiting, serving_list=serving, technicians=techs,
                           title="Live Console")


@app.route('/staff/complete-work/<int:q_id>', methods=['POST'])  # MUST BE POST
@login_required
def complete_work(q_id):
    # Capture values from the HTML form
    start_str = request.form.get('manual_start')
    end_str = request.form.get('manual_end')

    # NEW: Capture JO and SRH at the point of completion
    jo_number = request.form.get('job_order')
    srh_value = request.form.get('std_repair_hours')

    if not start_str or not end_str:
        flash("Error: Start and End times are required.", "danger")
        return redirect(url_for('staff_panel'))

    q = db.session.get(Queue, q_id)
    if q:
        today = datetime.now(timezone.utc).date()
        # Convert the HH:MM strings into actual database time objects
        q.start_time = datetime.combine(today, datetime.strptime(start_str, '%H:%M').time())
        q.end_time = datetime.combine(today, datetime.strptime(end_str, '%H:%M').time())

        # Save the JO and SRH to the booking record
        if q.booking:
            if jo_number: q.booking.job_order = jo_number
            if srh_value: q.booking.std_repair_hours = float(srh_value)
            q.booking.status = 'done'

        q.status = 'done'
        db.session.commit()

        # Notify customer
        if q.booking and q.booking.customer:
            notify_customer(q.booking.customer, q.booking.plate_number, 'done', q.id, q.ticket_number)

        flash(f"Ticket {q.ticket_number} marked complete.", "success")

    return redirect(url_for('staff_panel'))


@app.route('/staff/records')
@login_required
@permission_required('records')  # <--- Use the Matrix Key
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

    try:
        new_booking = Booking(
            user_id=None,  # Explicitly None for Walk-ins
            location_id=loc_id,
            plate_number=plate_number,
            guest_name=guest_name,
            service_type=service_type,
            service_location='In-Plant',
            status='arrived',
            ref_id='W-' + ''.join(random.choices(string.digits, k=4))
        )
        db.session.add(new_booking)
        db.session.flush()

        today = datetime.now(timezone.utc).date()
        count = Queue.query.filter_by(location_id=loc_id).filter(func.date(Queue.created_at) == today).count()
        ticket_no = f"{session.get('location_code', 'CCI')}-W-{101 + count}"

        new_q = Queue(ticket_number=ticket_no, location_id=loc_id, booking_id=new_booking.id, status='waiting')
        db.session.add(new_q)
        db.session.commit()

        return jsonify({"status": "success", "ticket": ticket_no, "q_id": new_q.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/print-ticket/<int:q_id>')
def print_ticket_view(q_id):
    queue_item = db.session.get(Queue, q_id)
    if not queue_item: return "Ticket not found", 404
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
    today = now.date()

    # 1. TOTAL THROUGHPUT (Today) - Includes all types
    daily_count = Queue.query.filter(
        Queue.location_id == loc_id,
        func.date(Queue.created_at) == today
    ).count()

    # 2. SERVICE VELOCITY (Avg minutes from Start to End)
    completed_jobs = Queue.query.filter(
        Queue.location_id == loc_id,
        Queue.status == 'done',
        Queue.start_time != None,
        Queue.end_time != None
    ).all()

    total_mins = 0
    for job in completed_jobs:
        diff = job.end_time - job.start_time
        total_mins += diff.total_seconds() / 60

    avg_wait = int(total_mins / len(completed_jobs)) if completed_jobs else 0

    # 3. MONTHLY MOMENTUM
    this_month_count = Queue.query.filter(
        Queue.location_id == loc_id,
        extract('month', Queue.created_at) == now.month
    ).count()

    last_month_count = Queue.query.filter(
        Queue.location_id == loc_id,
        extract('month', Queue.created_at) == (now.month - 1 if now.month > 1 else 12)
    ).count()

    momentum = int(((this_month_count - last_month_count) / last_month_count * 100)) if last_month_count > 0 else 100

    # 4. SOURCE BREAKDOWN (Online vs Walk-in vs Phone)
    # Online: booking.user_id exists and no [PHONE] prefix
    # Phone: booking.guest_name starts with [PHONE]
    # Walk-in: booking.ref_id starts with W-
    sources = db.session.query(
        Booking.service_location,  # Placeholder or use status logic
        func.count(Queue.id)
    ).join(Queue).filter(Queue.location_id == loc_id).group_by(Booking.service_location).all()

    # 5. TECH STATS
    tech_stats = db.session.query(
        Technician.name,
        func.count(queue_technicians.c.queue_id).label('total_jobs')
    ).join(queue_technicians).join(Queue).filter(
        Queue.location_id == loc_id,
        Queue.status == 'done'
    ).group_by(Technician.name).order_by(db.desc('total_jobs')).limit(5).all()

    return render_template('staff_analytics.html',
                           daily_count=daily_count,
                           monthly_count=this_month_count,
                           avg_wait=avg_wait,
                           momentum=momentum,
                           tech_stats=tech_stats,
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
    loc_id = request.args.get('loc_id') or session.get('loc_id')
    if not loc_id:
        return jsonify({"now_serving": "---", "waiting": []})

    # 1. Get the most recently COMPLETED unit (status='done')
    # within the last 15 minutes
    threshold = datetime.now(timezone.utc) - timedelta(minutes=15)

    latest_release = Queue.query.filter(
        Queue.location_id == loc_id,
        Queue.status == 'done',
        Queue.end_time >= threshold
    ).order_by(Queue.end_time.desc()).first()

    # 2. Get the units still in the facility (either 'waiting' or currently 'serving')
    # This keeps the "Up Next" list populated with units still on the floor
    active_queue = Queue.query.filter(
        Queue.location_id == loc_id,
        Queue.status.in_(['waiting', 'serving'])
    ).order_by(Queue.created_at.asc()).limit(5).all()

    return jsonify({
        "now_serving": latest_release.ticket_number if latest_release else "---",
        "now_serving_plate": latest_release.booking.plate_number if (latest_release and latest_release.booking) else "",
        "call_count": latest_release.call_count if latest_release else 0,
        "waiting": [
            {
                "ticket": t.ticket_number,
                "plate": t.booking.plate_number if t.booking else "WALK-IN",
                "is_on_floor": t.status == 'serving'
            } for t in active_queue
        ]
    })


from datetime import datetime, timezone  # Ensure these are imported at the top


@app.route('/staff/users', methods=['GET', 'POST'])
@login_required
@permission_required('users')
def staff_users():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')  # super_admin, admin, coordinator, advisor, customer
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
    """
    Updates staff telemetry (Last Seen and Current Hub).
    Throttled to once every 60 seconds to optimize DB performance and prevent hangs.
    """
    # 1. Only track Management (Staff/Admin/Super Admin)
    if current_user.is_authenticated and current_user.role in ['admin', 'coordinator', 'advisor', 'super_admin']:
        now = datetime.now(timezone.utc)

        # 2. Retrieve the last time the DB was updated for this user
        last_update = current_user.last_seen

        # 3. THROTTLING LOGIC
        # We only proceed if last_seen is empty or if more than 60 seconds have passed
        should_update = False
        if not last_update:
            should_update = True
        else:
            # Handle timezone safety (ensure both are UTC for comparison)
            if last_update.tzinfo is None:
                last_update = last_update.replace(tzinfo=timezone.utc)

            if (now - last_update).total_seconds() > 60:
                should_update = True

        if should_update:
            try:
                # Update the timestamp
                current_user.last_seen = now

                # Update the location ID currently assigned in the session
                if 'loc_id' in session:
                    current_user.current_loc_id = session.get('loc_id')

                # Perform the DB commit
                db.session.commit()
            except Exception as e:
                # Safety rollback to prevent DB locking
                db.session.rollback()
                app.logger.error(f"Telemetry Update Error: {e}")


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
    if q and q.location_id == loc_id:
        q.status = 'waiting'
        if q.booking: q.booking.status = 'pending'
        db.session.commit()
        flash(f"Ticket {q.ticket_number} returned to queue.", "success")
    return redirect(url_for('staff_records'))


import threading
from flask import current_app


def notify_customer(user, plate_number, status_type, queue_id=None, ticket_number=None):
    """
    Unified engine to send SMS and Email automatically in the background.
    Safely ignores Walk-ins (user=None) and prevents the dashboard from freezing.
    """
    # 1. CRITICAL: If user is None (Walk-in Guest), exit immediately
    if user is None:
        return

    # 2. CAPTURE CONTEXT: Threads cannot access 'session' or 'request' objects.
    # We capture all required data into local variables before starting the thread.
    loc_name = session.get('location_name', 'Coolaire Service Center')
    root_url = request.url_root
    user_id = user.id  # Pass ID to fetch a fresh object inside the thread session

    # Get the actual Flask app instance to pass into the thread
    app_instance = current_app._get_current_object()

    def run_notifications(app_ctx, u_id, l_name, base_url):
        with app_ctx.app_context():
            # Fetch fresh user record within this thread's database session
            db_user = db.session.get(User, u_id)
            if not db_user:
                return

            # Fetch System Settings for APIs and SMTP
            settings = {s.key: s.value for s in SystemSetting.query.all()}

            # 3. DEFINE MESSAGE CONTENT BASED ON STATUS
            subject = "Coolaire System Update"
            msg_text = ""
            is_account_msg = False

            if status_type == 'registration_pending':
                subject = "Coolaire Registration: Pending Verification"
                msg_text = f"Hello {db_user.full_name}, we have received your application for {db_user.company_name}. Access is currently under review."
                is_account_msg = True
            elif status_type == 'account_approved':
                subject = "Coolaire Account: Access Granted"
                msg_text = f"Great news, {db_user.full_name}! Your account for {db_user.company_name} is now active. You may now use the Partner Portal."
                is_account_msg = True
            elif status_type == 'serving':
                subject = f"Service Started: {plate_number}"
                msg_text = f"Coolaire Update: Your unit {plate_number} (Ticket {ticket_number}) is now being serviced on the floor."
            elif status_type == 'done':
                subject = f"Service Complete: {plate_number}"
                msg_text = f"Coolaire Update: Great news! Service for unit {plate_number} is finished. Please proceed to the release bay."
            elif status_type == 'expired':
                subject = "Appointment Status: Expired"
                msg_text = f"Coolaire Update: We noticed you weren't able to make it for unit {plate_number}. The ticket has been marked as no-show."
            else:
                return

            # --- PART A: SMS (SEMAPHORE) ---
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

            # --- PART B: EMAIL (SMTP) ---
            mail_user = settings.get('MAIL_HOST_USER')
            mail_pass = settings.get('MAIL_HOST_PASSWORD')
            mail_server = settings.get('MAIL_SERVER', 'mail.coolaireconsolidated.com')
            mail_port = int(settings.get('MAIL_PORT', 465))

            if mail_user and mail_pass and db_user.email:
                email_log = NotificationLog(queue_id=queue_id, recipient=db_user.email, channel='email')
                try:
                    msg = MIMEMultipart('related')
                    msg['From'] = f"Coolaire Service <{mail_user}>"
                    msg['To'] = db_user.email
                    msg['Subject'] = subject

                    # Style the info box based on message type
                    info_box_html = f"""
                    <div style="margin-top: 20px; padding: 15px; background: #f8fafc; border-left: 4px solid #002d72; border-radius: 4px;">
                        <p style="margin: 0; font-size: 14px;"><strong>Target Asset:</strong> {plate_number}</p>
                        <p style="margin: 5px 0 0 0; font-size: 14px;"><strong>Location:</strong> {l_name}</p>
                        {f'<p style="margin: 5px 0 0 0; font-size: 14px;"><strong>Reference:</strong> {ticket_number}</p>' if ticket_number else ''}
                    </div>"""

                    html = f"""
                    <html>
                        <body style="font-family: sans-serif; color: #333; line-height: 1.6;">
                            <div style="max-width: 600px; margin: auto; border: 1px solid #eee; border-radius: 10px; overflow: hidden;">
                                <div style="background: #002d72; padding: 25px; text-align: center;">
                                    <img src="cid:logo" alt="Coolaire" style="height: 50px;">
                                </div>
                                <div style="padding: 30px;">
                                    <h2 style="color: #002d72; margin-top: 0;">System Notification</h2>
                                    <p>Dear <strong>{db_user.full_name}</strong>,</p>
                                    <p>{msg_text}</p>
                                    {info_box_html}
                                    <p style="margin-top: 30px; font-size: 13px; color: #666;">If you have any questions, please contact our service team.</p>
                                </div>
                                <div style="background: #f1f1f1; padding: 15px; text-align: center; font-size: 11px; color: #888;">
                                    &copy; {datetime.now().year} Coolaire Consolidated Inc.
                                </div>
                            </div>
                        </body>
                    </html>"""
                    msg.attach(MIMEText(html, 'html'))

                    # Embed Logo
                    logo_path = os.path.join(app_ctx.root_path, 'static', 'logo.png')
                    if os.path.exists(logo_path):
                        with open(logo_path, 'rb') as f:
                            img = MIMEImage(f.read())
                            img.add_header('Content-ID', '<logo>')
                            msg.attach(img)

                    # SMTP Execution
                    with smtplib.SMTP_SSL(mail_server, mail_port) as server:
                        server.login(mail_user, mail_pass)
                        server.sendmail(mail_user, db_user.email, msg.as_string())
                    email_log.status = 'success'

                except Exception as e:
                    email_log.status = 'failed'
                    email_log.error_message = str(e)

                db.session.add(email_log)

            db.session.commit()

    # Start the background task
    threading.Thread(target=run_notifications, args=(app_instance, user_id, loc_name, root_url)).start()


@app.route('/staff/notifications')
@login_required
@permission_required('notifications')  # <--- Use the Matrix Key
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
            flash(
                f"Account approved for {user_to_verify.full_name}, but the activation email failed to send. Please check SMTP settings.",
                "warning")

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
            db.joinedload(Booking.customer),  # 'customer' is the backref from User.bookings
            db.joinedload(Booking.associated_vehicle)
            # 'associated_vehicle' is the backref from Vehicle.related_bookings
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
        tech.is_present = not tech.is_present
        db.session.commit()
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
    roles = ['admin', 'coordinator', 'advisor']
    features = [
        ('analytics', 'Site Analytics'),
        ('notifications', 'Messaging Audit'),
        ('records', 'Service History'),
        ('audit', 'Security Audit Trail'),
        ('verify_center', 'Identity Verification'),
        ('users', 'User Registry'),
        ('settings', 'System Settings'),
        ('global_bookings', 'Global Ledger'),
        ('technicians', 'Manage Technicians'),  # Add this
        ('locations', 'Manage Branches')  # Add this
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


@app.route('/api/device-heartbeat')
def device_heartbeat():
    loc_id = request.args.get('loc_id')
    device_type = request.args.get('type')

    if loc_id and device_type:
        loc = db.session.get(Location, int(loc_id))
        if loc:
            # FORCE UTC
            now = datetime.now(timezone.utc)
            if device_type == 'kiosk':
                loc.kiosk_last_seen = now
            elif device_type == 'tv':
                loc.tv_last_seen = now

            db.session.commit()
            # print(f"DEBUG: Heartbeat received for {loc.name} {device_type}") # Check your terminal
            return jsonify({"status": "ok", "time": now.isoformat()})

    return jsonify({"status": "error"}), 400


@app.route('/staff/reports')
@login_required
@roles_required('super_admin', 'admin', 'coordinator')
def staff_reports():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    # 1. PIVOTED MONTHLY QUERY (One row per month)
    service_query = db.session.query(
        func.to_char(Booking.scheduled_time, 'YYYY-MM').label('month'),
        func.count(Booking.id).filter(Booking.service_location == 'In-Plant').label('in_plant_count'),
        func.count(Booking.id).filter(Booking.service_location == 'Out-Plant').label('out_plant_count'),
        func.count(Booking.id).label('grand_total')
    ).filter(Booking.status == 'done')

    # 2. Tech Performance Query
    tech_query = db.session.query(
        Technician.name,
        func.count(Queue.id).label('total_completed')
    ).join(queue_technicians, Technician.id == queue_technicians.c.technician_id) \
     .join(Queue, Queue.id == queue_technicians.c.queue_id) \
     .filter(Queue.status == 'done')

    # 3. Company Audit Query
    company_query = db.session.query(
        User.company_name,
        Booking.service_location,
        func.count(Booking.id).label('total_units')
    ).join(User, Booking.user_id == User.id).filter(Booking.status == 'done')

    # Apply Filters
    if start_date and end_date:
        service_query = service_query.filter(Booking.scheduled_time.between(start_date, end_date))
        tech_query = tech_query.filter(Queue.created_at.between(start_date, end_date))
        company_query = company_query.filter(Booking.scheduled_time.between(start_date, end_date))

    # EXECUTE (Prevents NameError)
    service_stats = service_query.group_by('month').order_by(db.desc('month')).all()
    tech_performance = tech_query.group_by(Technician.name).order_by(db.desc('total_completed')).all()
    company_audit = company_query.group_by(User.company_name, Booking.service_location).order_by(User.company_name.asc()).all()

    return render_template('staff_reports.html',
                           service_stats=service_stats,
                           tech_performance=tech_performance,
                           company_audit=company_audit,
                           start_date=start_date, end_date=end_date, title="Operations Performance")


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
            # 2. Inspect the 'bookings' table
            inspector = db.inspect(db.engine)
            existing_columns = [c['name'] for c in inspector.get_columns('bookings')]

            if 'kiosk_last_seen' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE locations ADD COLUMN kiosk_last_seen TIMESTAMP'))
            if 'tv_last_seen' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE locations ADD COLUMN tv_last_seen TIMESTAMP'))
            db.session.commit()

            # Existing Migration: plate_number
            if 'plate_number' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN plate_number VARCHAR(50)'))
                db.session.commit()
                print("--- Database Updated: Added plate_number ---")

            # Existing Migration: guest_name
            if 'guest_name' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN guest_name VARCHAR(150)'))
                db.session.commit()
                print("--- Database Updated: Added guest_name ---")

            # Existing Migration: service_location
            if 'service_location' not in existing_columns:
                db.session.execute(
                    db.text("ALTER TABLE bookings ADD COLUMN service_location VARCHAR(50) DEFAULT 'In-Plant'"))
                db.session.commit()
                print("--- Database Updated: Added service_location ---")

            # --- NEW MIGRATIONS START HERE ---

            # New Migration: job_order
            if 'job_order' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN job_order VARCHAR(50)'))
                db.session.commit()
                print("--- Database Updated: Added job_order ---")

            # New Migration: std_repair_hours
            if 'std_repair_hours' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN std_repair_hours FLOAT DEFAULT 0.0'))
                db.session.commit()
                print("--- Database Updated: Added std_repair_hours ---")

            # --- NEW MIGRATIONS END HERE ---

        except Exception as e:
            print(f"--- Database Migration Note: {e} ---")
            db.session.rollback()

    # 3. Run the app
    app.run(debug=True, port=5000)