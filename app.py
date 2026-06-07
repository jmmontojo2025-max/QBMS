import os
import random
import string
import json
import smtplib
import threading  # For background tasks
from datetime import date, datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from functools import wraps

import kwargs
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort, current_app
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, extract, or_, and_
from flask_wtf.csrf import CSRFProtect
from requests_oauthlib import OAuth1
from itsdangerous import URLSafeTimedSerializer

# Define Philippine Time (UTC+8)
PHT = timezone(timedelta(hours=8))


def get_pht_now():
    # Returns the current local PHT time as a database-safe naive object
    return datetime.now(PHT).replace(tzinfo=None)


app = Flask(__name__)
# IMPORTANT: Use a strong, random key from environment for production.
# The default here is for local development ONLY.
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "your_super_secret_dev_key_change_me_in_prod")
raw_db_url = os.environ.get("DATABASE_URL",
                            "postgresql://postgres.mguajchtxgunyfzotipa:Itadmin36155912030*@aws-1-ap-southeast-2.pooler.supabase.com:6543/postgres")
app.config['SQLALCHEMY_DATABASE_URI'] = raw_db_url.replace("postgres://", "postgresql://")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['WTF_CSRF_TIME_LIMIT'] = None

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

            # SUPER ADMIN BYPASS: Always allow super_admin
            if current_user.role == 'super_admin':
                return f(*args, **kwargs)

            # Check if user has one of the allowed roles
            if current_user.role not in roles:
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
    capacity = db.Column(db.Integer, default=20)
    kiosk_last_seen = db.Column(db.DateTime)
    tv_last_seen = db.Column(db.DateTime)

    @property
    def kiosk_online(self):
        if not self.kiosk_last_seen: return False
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


class Technician(db.Model):
    __tablename__ = 'technicians'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))
    branch = db.relationship('Location', backref='technicians')  # Relationship name is "branch"
    is_active = db.Column(db.Boolean, default=True)
    is_present = db.Column(db.Boolean, default=True)


class Advisor(db.Model):
    __tablename__ = 'advisors'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = db.Column(db.Boolean, default=True)


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
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'), nullable=True)
    plate_number = db.Column(db.String(50))
    guest_name = db.Column(db.String(150))
    service_type = db.Column(db.String(255))
    service_location = db.Column(db.String(50), default='In-Plant')
    status = db.Column(db.String(20), default='pending')
    scheduled_time = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    ref_id = db.Column(db.String(10), unique=True)
    queue_records = db.relationship('Queue', back_populates='booking', cascade="all, delete-orphan")
    job_order = db.Column(db.String(50), nullable=True)
    std_repair_hours = db.Column(db.Float, default=0.0)

    # DYNAMIC DATES & TIMES MIGRATION MAPPINGS
    date_of_entry = db.Column(db.Date, default=lambda: datetime.now(PHT).date())
    preferred_service_date = db.Column(db.Date, default=lambda: datetime.now(PHT).date())
    preferred_service_time = db.Column(db.String(10), default='08:00')

    # RELATIONS
    advisor_id = db.Column(db.Integer, db.ForeignKey('advisors.id'), nullable=True)
    advisor = db.relationship('Advisor', backref='bookings')

    location = db.relationship('Location', backref='bookings')

    def __init__(self, **kwargs):
        super(Booking, self).__init__(**kwargs)
        if not self.ref_id:
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
    materials_used = db.Column(db.Text)

    # Date of Work covering start and end
    date_of_work = db.Column(db.Date, default=lambda: datetime.now(PHT).date())

    # Relationships
    location = db.relationship('Location', backref='queue_entries')
    booking = db.relationship('Booking', back_populates='queue_records')

    # RELATIONSHIP
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
    action = db.Column(db.String(100))
    details = db.Column(db.Text)

    # ASSET METADATA
    ticket_number = db.Column(db.String(20))
    plate_number = db.Column(db.String(50))

    # NETWORK FORENSICS
    ip_address = db.Column(db.String(50))
    user_agent = db.Column(db.Text)
    device_type = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    performer = db.relationship('User', backref='logs')
    location = db.relationship('Location')


class RolePermission(db.Model):
    __tablename__ = 'role_permissions'
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(20))
    feature_key = db.Column(db.String(50))
    is_allowed = db.Column(db.Boolean, default=False)


# --- STAFF OPERATIONS ---

def require_staff_location():
    if current_user.is_authenticated and current_user.role not in ['staff', 'admin', 'super_admin', 'coordinator',
                                                                   'advisor']:
        abort(403)

    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    if current_user.role in ['staff', 'admin', 'super_admin', 'coordinator', 'advisor'] and 'loc_id' not in session:
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


@app.context_processor
def utility_processor():
    def has_perm(permission_name):
        if current_user.is_authenticated and current_user.role == 'super_admin':
            return True
        if not current_user.is_authenticated:
            return False
        return check_permission(permission_name)

    # Injected safe defaults to prevent global UndefinedErrors in shared layouts
    return dict(
        has_perm=has_perm,
        capacity_percent=0,
        current_occupancy=0,
        max_capacity=20,
        today_date=datetime.now(PHT).date(),
        busy_map={}  # SAFE FALLBACK EMPTY DICTIONARY
    )


def check_permission(feature_key):
    if not current_user.is_authenticated: return False
    if current_user.role == 'super_admin': return True

    user_role = current_user.role.lower().strip()

    if feature_key.lower().strip() in ['start-work', 'recall-ticket'] and user_role in ['admin', 'coordinator',
                                                                                        'advisor']:
        return True

    perm = RolePermission.query.filter_by(role=user_role, feature_key=feature_key.lower().strip()).first()
    return perm.is_allowed if perm else False


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
            app.logger.error(f"SMS API Error: {e}")


# --- GENERAL ROUTES ---

@app.route('/')
def root_redirect_to_login():
    if current_user.is_authenticated:
        if current_user.role == 'customer':
            return redirect(url_for('dashboard'))
        elif current_user.role in ['admin', 'coordinator', 'advisor', 'super_admin']:
            if 'loc_id' in session:
                return redirect(url_for('staff_panel'))
            else:
                return redirect(url_for('select_branch_for_staff'))
    return redirect(url_for('login'))


@app.route('/select-branch-for-staff')
@login_required
def select_branch_for_staff():
    """
    Renders the branch selection page.
    Removed session redirection trap so logged-in staff can switch terminals.
    """
    if current_user.role not in ['staff', 'admin', 'super_admin', 'coordinator', 'advisor']:
        flash("Management clearance required.", "danger")
        return redirect(url_for('login'))

    locations = Location.query.all()
    return render_template('location_select.html', locs=locations)


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

    current_user.current_loc_id = loc.id
    db.session.commit()

    if current_user.role in ['staff', 'admin', 'super_admin', 'coordinator', 'advisor']:
        return redirect(url_for('staff_panel'))

    return redirect(url_for('dashboard'))


@app.route('/change-branch')
@login_required
def change_branch():
    # Clear the location-specific keys from the session
    session.pop('loc_id', None)
    session.pop('location_name', None)
    session.pop('location_code', None)
    session.modified = True

    # Reset the database tracking for the user's active branch
    if current_user.is_authenticated:
        current_user.current_loc_id = None
        db.session.commit()

    return redirect(url_for('select_branch_for_staff'))


# --- STAFF OPERATIONS ---

# --- STAFF OPERATIONS ---

# --- STAFF OPERATIONS ---

@app.route('/staff')
@login_required
@roles_required('super_admin', 'admin', 'coordinator', 'advisor')
def staff_panel():
    if 'loc_id' not in session: return redirect(url_for('select_branch_for_staff'))
    loc_id = session.get('loc_id')

    current_location = db.session.get(Location, loc_id)

    # OPTIMIZED: Fetch all active queue entries, preloading relationships to eliminate N+1 latency
    active_queues = Queue.query.options(
        db.selectinload(Queue.assigned_techs),
        db.joinedload(Queue.booking).joinedload(Booking.advisor),
        db.joinedload(Queue.booking).joinedload(Booking.customer)
    ).filter(
        Queue.location_id == loc_id,
        Queue.status.in_(['waiting', 'serving', 'served'])
    ).all()

    # Calculate exact mathematical segments
    waiting = [q for q in active_queues if q.status == 'waiting']
    serving = [q for q in active_queues if q.status == 'serving']
    served = [q for q in active_queues if q.status == 'served']  # Fetch completed served tickets

    # Pre-calculate busy map supporting multiple bookings per technician
    busy_map = {}
    for q in serving:
        service_name = q.booking.service_type if q.booking else "General Service"
        plate_no = q.booking.plate_number if q.booking else "WALK-IN"
        for t_assigned in q.assigned_techs:
            if t_assigned.id not in busy_map:
                busy_map[t_assigned.id] = []
            busy_map[t_assigned.id].append({
                'ticket': q.ticket_number,
                'plate': plate_no,
                'service': service_name
            })

    # OPTIMIZED: Preload tasks and task details for all techs in the side panel
    all_techs = Technician.query.options(
        db.selectinload(Technician.tasks).joinedload(Queue.booking).joinedload(Booking.customer)
    ).filter_by(location_id=loc_id, is_active=True).order_by(Technician.name.asc()).all()

    available_techs = [t for t in all_techs if t.is_present]

    categories = ServiceCategory.query.order_by(ServiceCategory.name.asc()).all()
    max_capacity = current_location.capacity if current_location else 20

    # Occupancy is calculated as the sum of visible waiting and serving tickets
    current_occupancy = len(waiting) + len(serving)
    capacity_percent = int((current_occupancy / max_capacity) * 100) if max_capacity > 0 else 0

    today_pht = datetime.now(PHT).date()
    active_advisors = Advisor.query.filter_by(is_active=True).order_by(Advisor.name.asc()).all()

    context = {
        "current_location": current_location,
        "categories": categories,
        "waiting_tickets": waiting,
        "serving_list": serving,
        "served_list": served,  # Added served list context
        "technicians": available_techs,
        "roster": all_techs,
        "busy_map": busy_map,
        "max_capacity": max_capacity,
        "current_occupancy": current_occupancy,
        "capacity_percent": capacity_percent,
        "today_date": today_pht,
        "advisors": active_advisors,
        "title": "Live Console"
    }

    if request.headers.get('HX-Request'):
        return render_template('staff_content_only.html', **context)

    return render_template('staff.html', **context)


@app.route('/admin/workflow')
@login_required
def admin_workflow():
    if current_user.role not in ['admin', 'super_admin']:
        return "Unauthorized Access", 403

    locations = Location.query.order_by(Location.name.asc()).all()
    hub_data = []
    today = date.today()
    today_pht = datetime.now(PHT).date()

    for loc in locations:
        # OPTIMIZED: Preload nested relationships to eliminate N+1 latency loops
        tickets = Queue.query.options(
            db.joinedload(Queue.booking).joinedload(Booking.advisor),
            db.joinedload(Queue.booking).joinedload(Booking.customer),
            db.selectinload(Queue.assigned_techs)
        ).filter(
            Queue.location_id == loc.id
        ).filter(
            (Queue.status.in_(['waiting', 'serving', 'served'])) |
            ((Queue.status == 'done') & (func.date(Queue.created_at) == today))
        ).order_by(Queue.created_at.asc()).all()

        # OPTIMIZED: Preload active tasks inside roster cards to eliminate N+1 loops
        techs = Technician.query.options(
            db.selectinload(Technician.tasks).joinedload(Queue.booking).joinedload(Booking.customer)
        ).filter_by(location_id=loc.id).all()

        busy_map = {}
        for t in tickets:
            if t.status == 'serving':
                for tech in t.assigned_techs:
                    service_name = t.booking.service_type if t.booking else "General Service"
                    if tech.id not in busy_map:
                        busy_map[tech.id] = []
                    busy_map[tech.id].append({
                        'ticket': t.ticket_number,
                        'service': service_name
                    })

        # Calculate active tickets, excluding done and no-show states
        active_tickets = [t for t in tickets if t.status in ['waiting', 'serving']]

        hub_data.append({
            'info': loc,
            'tickets': tickets,
            'techs': techs,
            'busy_map': busy_map,
            'ticket_count': len(active_tickets)
        })

    categories = ServiceCategory.query.order_by(ServiceCategory.name.asc()).all()
    active_advisors = Advisor.query.filter_by(is_active=True).order_by(Advisor.name.asc()).all()

    return render_template(
        'admin_workflow.html',
        hub_data=hub_data,
        categories=categories,
        today_date=today_pht,
        advisors=active_advisors,
        title="Global Workflow Audit"
    )


# --- STAFF ADVISOR MANAGEMENT ---
@app.route('/staff/advisors', methods=['GET', 'POST'])
@login_required
@roles_required('super_admin', 'admin', 'coordinator', 'advisor')
def manage_advisors():
    """
    Roster management endpoint for registering, tracking and deactivating
    Service Advisors with audit logs.
    """
    if request.method == 'POST':
        name = request.form.get('advisor_name').strip()
        if name:
            new_adv = Advisor(name=name)
            db.session.add(new_adv)
            db.session.commit()

            # Record audit trail action
            log_action(
                action="Advisor Registered",
                details=f"Staff enrolled new Service Advisor: {name} with automatic timestamps."
            )
            flash(f"Advisor {name} enrolled successfully.", "success")
        return redirect(url_for('manage_advisors'))

    all_advisors = Advisor.query.filter_by(is_active=True).order_by(Advisor.created_at.desc()).all()
    return render_template('staff_advisors.html', advisors=all_advisors, title="Service Advisors")


@app.route('/staff/advisors/delete/<int:id>')
@login_required
@roles_required('super_admin', 'admin', 'coordinator')
def delete_advisor(id):
    adv = db.session.get(Advisor, id)
    if adv:
        adv_name = adv.name
        adv.is_active = False  # Soft deactivation
        db.session.commit()

        # Record audit trail action
        log_action(
            action="Advisor Deactivated",
            details=f"Staff deactivated advisor record: {adv_name}."
        )
        flash(f"Advisor {adv_name} deactivated.", "warning")
    return redirect(url_for('manage_advisors'))


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
    admin_loc_id = request.form.get('admin_loc_id')
    loc_id = admin_loc_id if admin_loc_id else session.get('loc_id')
    target_loc = db.session.get(Location, loc_id)
    loc_code = target_loc.code if target_loc else 'CCI'

    manifest_data = request.form.get('staff_manifest_data')
    manifest = []
    today_pht = datetime.now(PHT).date()

    if manifest_data:
        try:
            manifest = json.loads(manifest_data)
        except Exception as e:
            app.logger.error(f"Failed to parse manifest JSON: {e}")
            flash("Invalid manifest data format.", "danger")
            return redirect(request.referrer or url_for('staff_panel'))
    else:
        # Fallback parsing for manual single form posts
        plate = request.form.get('plate')
        client = request.form.get('client')
        service = request.form.get('service')
        site = request.form.get('site', 'In-Plant')
        jo = request.form.get('jo')
        srh = request.form.get('srh')
        pref_date = request.form.get('preferred_date')
        pref_time = request.form.get('preferred_time', '08:00')
        ent_date = request.form.get('entry_date')
        adv_id = request.form.get('advisor_id')

        if plate and client:
            manifest.append({
                'plate': plate,
                'client': client,
                'service': service or 'General Service',
                'site': site,
                'jo': jo,
                'srh': srh,
                'preferred_date': pref_date,
                'preferred_time': pref_time,
                'entry_date': ent_date,
                'advisor_id': adv_id
            })

    if not manifest:
        flash("No vehicle deployment entries found to process.", "warning")
        return redirect(request.referrer or url_for('staff_panel'))

    try:
        current_total = Queue.query.filter_by(location_id=loc_id).count()

        for i, item in enumerate(manifest):
            plate_clean = item['plate'].strip().upper()

            # Parse Preferred service Date
            pref_date_str = item.get('preferred_date') or item.get('preferred_service_date')
            if pref_date_str:
                try:
                    pref_date = datetime.strptime(pref_date_str, '%Y-%m-%d').date()
                except ValueError:
                    pref_date = today_pht
            else:
                pref_date = today_pht

            # Parse Entry Date (which is visible on entry form)
            entry_date_str = item.get('entry_date') or item.get('date_of_entry')
            if entry_date_str:
                try:
                    entry_date = datetime.strptime(entry_date_str, '%Y-%m-%d').date()
                except ValueError:
                    entry_date = today_pht
            else:
                entry_date = today_pht

            pref_time = item.get('preferred_time', '08:00')
            advisor_id = item.get('advisor_id')

            new_booking = Booking(
                user_id=None,
                location_id=loc_id,
                plate_number=plate_clean,
                guest_name=f"[PHONE] {item['client']}",
                service_type=item['service'],
                service_location=item['site'],
                job_order=item.get('jo'),
                std_repair_hours=float(item.get('srh', 0)) if item.get('srh') else 0.0,
                scheduled_time=get_pht_now(),
                status='arrived',
                date_of_entry=entry_date,
                preferred_service_date=pref_date,
                preferred_service_time=pref_time
            )

            # Map Advisor if assigned
            if advisor_id and advisor_id != 'none':
                new_booking.advisor_id = int(advisor_id)

            db.session.add(new_booking)
            db.session.commit()

            ticket_no = f"{loc_code}-{101 + current_total + i}"

            new_q = Queue(
                ticket_number=ticket_no,
                location_id=loc_id,
                booking_id=new_booking.id,
                status='waiting',
                created_at=get_pht_now()
            )
            db.session.add(new_q)

            # Record overridden entry date details to Immutable Audit Trail
            is_backtracked = entry_date != today_pht
            audit_action = "Backdated Manual Booking" if is_backtracked else "Manual Booking"
            log_action(
                action=audit_action,
                details=f"Staff manual deployment processed for Ticket {ticket_no}. Preferred slot: {pref_date} @ {pref_time}. Overridden Entry Date: {entry_date} (Real-time Transaction Entry: {today_pht}).",
                location_id=loc_id,
                ticket_number=ticket_no,
                plate_number=plate_clean
            )

        db.session.commit()
        flash(f"Successfully processed and deployed to {target_loc.name}.", "success")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Manual Check-In Error: {e}")
        flash("System error in manual booking execution.", "danger")

    return redirect(request.referrer or url_for('staff_panel'))


@app.route('/staff/start-work/<int:q_id>', methods=['POST'])
@login_required
@permission_required('start-work')
def start_work(q_id):
    loc_id = session.get('loc_id')
    tech_ids = request.form.getlist('technician_ids')
    q = db.session.get(Queue, q_id)

    jo_number = request.form.get('job_order')
    srh_value = request.form.get('std_repair_hours', 0)

    if q and q.location_id == loc_id and tech_ids:
        if q.booking:
            if jo_number: q.booking.job_order = jo_number
            if srh_value: q.booking.std_repair_hours = float(srh_value)

        techs = Technician.query.filter(Technician.id.in_(tech_ids)).all()
        q.assigned_techs = techs
        q.status = 'serving'
        db.session.commit()

        log_action(
            action="Dispatch",
            details=f"Ticket {q.ticket_number} sent to floor.",
            ticket_number=q.ticket_number,
            plate_number=q.booking.plate_number if q.booking else 'WALK-IN'
        )

        if q.booking and q.booking.customer:
            notify_customer(q.booking.customer, q.booking.plate_number, 'serving', q.id, q.ticket_number)

    return redirect(url_for('staff_panel'))


@app.route('/staff/recall-ticket/<int:q_id>')
@login_required
@permission_required('recall-ticket')
def recall_ticket(q_id):
    q = db.session.get(Queue, q_id)
    if q:
        q.call_count += 1
        db.session.commit()
        log_action(
            action="TV Recall",
            details=f"Ticket {q.ticket_number} called again on monitor.",
            ticket_number=q.ticket_number,
            plate_number=q.booking.plate_number if q.booking else 'WALK-IN'
        )
    return redirect(url_for('staff_panel'))


# --- CUSTOMER PORTAL LOGIN ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        if current_user.role == 'customer':
            return redirect(url_for('dashboard'))
        return redirect(url_for('staff_panel'))

    if request.method == 'POST':
        username = request.form.get('username').strip()
        password = request.form.get('password')

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            if user.role != 'customer':
                flash("Access Restricted: This portal is for Fleet Partners only.", "danger")
                return redirect(url_for('login'))

            if not user.is_approved:
                flash("Account Pending: Your registration is currently being verified by our team.", "warning")
                return redirect(url_for('login'))

            login_user(user)
            user.last_seen = datetime.now(timezone.utc)
            db.session.commit()

            flash(f"Welcome back, {user.full_name}!", "success")
            return redirect(url_for('dashboard'))

        flash("Login Failed: Please check your username and password.", "danger")

    return render_template('login.html')


@app.route('/staff/users/approve/<int:user_id>')
@login_required
def approve_user(user_id):
    if current_user.role not in ['admin', 'super_admin']:
        abort(403)

    u = db.session.get(User, user_id)
    if u:
        u.is_approved = True
        db.session.commit()
        notify_customer(user=u, plate_number="N/A", status_type='account_approved')
        flash(f"Access granted for {u.full_name}.", "success")
    return redirect(url_for('staff_users'))


@app.route('/staff/login', methods=['GET', 'POST'])
def staff_login():
    if current_user.is_authenticated and current_user.role in ['admin', 'coordinator', 'advisor', 'super_admin']:
        return redirect(url_for('select_branch_for_staff'))

    if request.method == 'POST':
        u = User.query.filter_by(username=request.form.get('username')).first()
        if u and check_password_hash(u.password_hash, request.form.get('password')):
            if u.role in ['admin', 'coordinator', 'advisor', 'super_admin']:
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

        existing_username = User.query.filter_by(username=username).first()
        if existing_username:
            flash("Registration Failed: This username is already taken.", "danger")
            return redirect(url_for('register'))

        existing_email = User.query.filter_by(email=email).first()
        if existing_email:
            flash("Registration Failed: An account with this email already exists.", "danger")
            return redirect(url_for('register'))

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
            is_approved=False
        )

        try:
            db.session.add(new_user)
            db.session.commit()

            try:
                notify_customer(
                    user=new_user,
                    plate_number="N/A",
                    status_type='registration_pending'
                )
            except Exception as mail_err:
                app.logger.error(f"Initial Reg Email Failed: {mail_err}")

            flash("Registration successful! Please check your email for the next steps.", "success")
            return redirect(url_for('login'))

        except Exception as e:
            db.session.rollback()
            return "Database Error", 500

    return render_template('register.html')


@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role in ['super_admin', 'admin', 'staff', 'coordinator', 'advisor']:
        return redirect(url_for('staff_panel'))

    bookings = Booking.query.options(db.selectinload(Booking.queue_records)).filter_by(user_id=current_user.id) \
        .order_by(Booking.scheduled_time.desc()).all()

    now = datetime.now(timezone.utc)
    forecast_results = db.session.query(
        Location.name,
        func.count(Booking.id).label('unit_count')
    ).join(Booking).filter(
        Booking.user_id == current_user.id,
        Booking.scheduled_time >= now,
        Booking.status == 'pending'
    ).group_by(Location.name).all()

    branch_forecast = {name: count for name, count in forecast_results}

    booking_id = request.args.get('booking_id')
    active_booking = None

    if booking_id:
        active_booking = Booking.query.filter_by(id=booking_id, user_id=current_user.id).first()
        if not active_booking:
            flash("Security Alert: Unauthorized asset tracking attempt.", "danger")
            return redirect(url_for('dashboard'))

    return render_template('dashboard.html',
                           service_history=bookings,
                           all_active_bookings=bookings,
                           booking=active_booking,
                           branch_forecast=branch_forecast,
                           title="Fleet Dashboard")


@app.route('/book', methods=['GET', 'POST'])
@login_required
def book():
    if request.method == 'POST':
        manifest_data = request.form.get('manifest_data')
        if not manifest_data:
            flash("No assets added to manifest.", "warning")
            return redirect(url_for('book'))

        manifest = json.loads(manifest_data)
        new_bookings_for_email = []

        try:
            for item in manifest:
                v_id = item.get('vehicle_id')
                plate_clean = item['plate_display'].split(' ')[0].strip().upper()

                if v_id == 'new':
                    new_v = Vehicle(
                        user_id=current_user.id,
                        plate_number=plate_clean,
                        model_description=item['new_model']
                    )
                    db.session.add(new_v)
                    db.session.flush()
                    v_id = new_v.id

                target_time = datetime.fromisoformat(item['time'])
                new_booking = Booking(
                    user_id=current_user.id,
                    location_id=item['location_id'],
                    vehicle_id=v_id,
                    plate_number=plate_clean,
                    service_type=item['product'],
                    service_location=item['service_location'],
                    scheduled_time=target_time,
                    status='pending',
                    date_of_entry=datetime.now(PHT).date(),
                    preferred_service_date=target_time.date()
                )
                db.session.add(new_booking)

                new_bookings_for_email.append({
                    'plate': plate_clean,
                    'hub': item['location_name'],
                    'service': item['product'],
                    'time': item['time_display']
                })

            db.session.commit()

            if new_bookings_for_email:
                try:
                    notify_customer(
                        user=current_user,
                        plate_number="Multiple Assets",
                        status_type='booking_confirmation',
                        booking_list=new_bookings_for_email
                    )
                except Exception as mail_err:
                    app.logger.error(f"Booking Email Failed: {mail_err}")

            flash(f"Successfully deployed {len(manifest)} assets. A confirmation email has been sent.", "success")
            return redirect(url_for('dashboard'))

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Booking error: {e}")
            flash("Error processing manifest.", "danger")

    categories = ServiceCategory.query.all()
    locations = Location.query.all()
    user_vehicles = Vehicle.query.filter_by(user_id=current_user.id).all()
    return render_template('book.html', categories=categories, locations=locations, vehicles=user_vehicles)


@app.route('/staff/locations', methods=['GET', 'POST'])
@login_required
@permission_required('locations')
def staff_locations():
    if request.method == 'POST':
        name = request.form.get('name').strip()
        code = request.form.get('code').strip().upper()
        capacity = request.form.get('capacity', 20)

        new_location = Location(name=name, code=code, capacity=int(capacity))
        db.session.add(new_location)
        db.session.commit()
        flash(f"Branch '{name}' added with capacity {capacity}.", "success")
        return redirect(url_for('staff_locations'))

    all_locations = Location.query.order_by(Location.name).all()
    return render_template('staff_locations.html', locations=all_locations, title="Hub Network")


@app.route('/staff/locations/edit/<int:loc_id>', methods=['GET', 'POST'])
@login_required
@permission_required('locations_edit')
def edit_location(loc_id):
    loc = db.session.get(Location, loc_id)
    if request.method == 'POST':
        loc.name = request.form.get('name')
        loc.code = request.form.get('code').upper()
        loc.capacity = int(request.form.get('capacity', 20))
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


@app.route('/staff/complete-work/<int:q_id>', methods=['POST'])
@login_required
def complete_work(q_id):
    q = db.session.get(Queue, q_id)
    if not q:
        flash("Error: Ticket not found.", "danger")
        return redirect(request.referrer or url_for('staff_panel'))

    start_str = request.form.get('manual_start')
    end_str = request.form.get('manual_end')
    jo_number = request.form.get('job_order')
    srh_value = request.form.get('std_repair_hours')
    scope_of_work = request.form.get('scope_of_work')
    work_date_str = request.form.get('date_of_work')

    try:
        pht_now = datetime.now(PHT)
        today_pht = pht_now.date()

        # Parse Work Date
        if work_date_str:
            try:
                work_date = datetime.strptime(work_date_str, '%Y-%m-%d').date()
            except ValueError:
                work_date = today_pht
        else:
            work_date = today_pht

        if q.booking:
            if jo_number:
                q.booking.job_order = jo_number
            if srh_value:
                try:
                    q.booking.std_repair_hours = float(srh_value)
                except ValueError:
                    pass

        # === STEP 1: STAFF COMPLETION ===
        if not scope_of_work:
            if not start_str or not end_str:
                flash("Error: Start and End times are required from Coordinators/Advisors.", "danger")
                return redirect(request.referrer or url_for('staff_panel'))

            # Combine start and end times with the selected "Date of Work"
            q.date_of_work = work_date
            q.start_time = datetime.combine(work_date, datetime.strptime(start_str, '%H:%M').time())
            q.end_time = datetime.combine(work_date, datetime.strptime(end_str, '%H:%M').time())

            q.status = 'served'
            if q.booking:
                q.booking.status = 'served'

            q.call_count += 1

            log_action(
                action="Staff Times Logged & Called",
                details=f"Staff logged times for Ticket {q.ticket_number}. Date of Work: {work_date}, Start: {start_str}, End: {end_str}. Called on TV for releasing.",
                location_id=q.location_id,
                ticket_number=q.ticket_number,
                plate_number=q.booking.plate_number if q.booking else 'WALK-IN'
            )
            flash(f"Times saved for {q.ticket_number}. Called on TV monitor for release.", "success")

        # === STEP 2: ADMIN COMPLETION ===
        else:
            q.internal_notes = scope_of_work
            if hasattr(q, 'scope_of_work'):
                q.scope_of_work = scope_of_work
            if q.booking:
                if hasattr(q.booking, 'scope_of_work'):
                    q.booking.scope_of_work = scope_of_work
                elif hasattr(q.booking, 'internal_notes'):
                    q.booking.internal_notes = scope_of_work
                q.booking.status = 'done'

            q.status = 'done'

            log_action(
                action="Ticket Completed",
                details=f"Ticket {q.ticket_number} ({q.booking.plate_number if q.booking else 'WALK-IN'}) completed & released. SOW: {scope_of_work}",
                location_id=q.location_id,
                ticket_number=q.ticket_number,
                plate_number=q.booking.plate_number if q.booking else 'WALK-IN'
            )

            if q.booking and q.booking.customer:
                notify_customer(q.booking.customer, q.booking.plate_number, 'done', q.id, q.ticket_number)

            flash(f"Ticket {q.ticket_number} successfully completed and released.", "success")

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Complete Work Error: {e}")
        flash("System Error: Could not process ticket completion.", "danger")

    return redirect(request.referrer or url_for('staff_panel'))


@app.route('/staff/records')
@login_required
@permission_required('records')
def staff_records():
    loc_id = session.get('loc_id')
    if not loc_id:
        flash("Please select a branch first.", "warning")
        return redirect(url_for('select_branch_for_staff'))

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
    if redirect_response: return redirect_response

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

    categories = ServiceCategory.query.order_by(ServiceCategory.name).all()
    return render_template('kiosk.html', categories=categories)


@app.route('/check-in', methods=['POST'])
@csrf.exempt
def check_in():
    loc_id = session.get('loc_id')
    loc_code = session.get('location_code', 'CCI')
    ref_code = request.form.get('booking_id')

    booking = Booking.query.filter_by(ref_id=ref_code, status='pending').first()

    if booking:
        total_count = Queue.query.filter_by(location_id=loc_id).count()
        ticket_no = f"{loc_code}-{101 + total_count}"

        new_q = Queue(
            ticket_number=ticket_no,
            location_id=loc_id,
            booking_id=booking.id,
            status='waiting',
            created_at=get_pht_now()
        )
        booking.status = 'arrived'
        db.session.add(new_q)
        db.session.commit()

        if booking.customer:
            try:
                notify_customer(
                    user=booking.customer,
                    plate_number=booking.plate_number,
                    status_type='arrived_kiosk',
                    queue_id=new_q.id,
                    ticket_number=ticket_no
                )
            except Exception as e:
                app.logger.error(f"Kiosk Arrival Notification Failed: {e}")

        return jsonify({"status": "success", "ticket": ticket_no, "q_id": new_q.id})

    return jsonify({"status": "error", "message": "Invalid Reference Code"}), 404


@app.route('/walk-in', methods=['POST'])
@csrf.exempt
def walk_in():
    loc_id = session.get('loc_id')
    loc_code = session.get('location_code', 'CCI')
    guest_name = request.form.get('customer_name')
    plate_number = request.form.get('plate_number', '').strip().upper()
    service_type = request.form.get('service_type')

    try:
        new_booking = Booking(
            user_id=None,
            location_id=loc_id,
            plate_number=plate_number,
            guest_name=guest_name,
            service_type=service_type,
            service_location='In-Plant',
            status='arrived',
            scheduled_time=get_pht_now(),
            ref_id='W-' + ''.join(random.choices(string.digits, k=4)),
            date_of_entry=datetime.now(PHT).date(),
            preferred_service_date=datetime.now(PHT).date()
        )
        db.session.add(new_booking)
        db.session.flush()

        total_count = Queue.query.filter_by(location_id=loc_id).count()
        ticket_no = f"{loc_code}-{101 + total_count}"

        new_q = Queue(
            ticket_number=ticket_no,
            location_id=loc_id,
            booking_id=new_booking.id,
            status='waiting',
            created_at=get_pht_now()
        )
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
        return redirect(url_for('login'))

    loc_id = session.get('loc_id')
    loc = db.session.get(Location, loc_id)
    if not loc:
        flash("Selected location not found.", "danger")
        return redirect(url_for('login'))

    return render_template('tv.html', location=loc)


# --- ANALYTICS API ---

@app.route('/staff/analytics')
@login_required
@permission_required('analytics')
def staff_analytics():
    loc_id = session.get('loc_id')
    now = datetime.now(timezone.utc)
    today = now.date()

    daily_count = Queue.query.filter(
        Queue.location_id == loc_id,
        func.date(Queue.created_at) == today
    ).count()

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

    this_month_count = Queue.query.filter(
        Queue.location_id == loc_id,
        extract('month', Queue.created_at) == now.month
    ).count()

    last_month_count = Queue.query.filter(
        Queue.location_id == loc_id,
        extract('month', Queue.created_at) == (now.month - 1 if now.month > 1 else 12)
    ).count()

    momentum = int(((this_month_count - last_month_count) / last_month_count * 100)) if last_month_count > 0 else 100

    sources = db.session.query(
        Booking.service_location,
        func.count(Queue.id)
    ).join(Queue).filter(Queue.location_id == loc_id).group_by(Booking.service_location).all()

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

    lookback_date = datetime.now(timezone.utc) - timedelta(days=30)

    results = db.session.query(
        extract('dow', Queue.created_at).label('day_of_week'),
        func.count(Queue.id).label('arrival_count')
    ).filter(
        Queue.location_id == loc_id,
        Queue.created_at >= lookback_date
    ).group_by('day_of_week').all()

    forecast_data = [0] * 7
    for day_index, count in results:
        idx = int(day_index)
        forecast_data[idx] = round(count / 4.2, 1)

    return jsonify({"forecast": [0, 0, 0, 0, 0, 0, 0]})


@app.route('/api/get-latest-queue')
def get_latest_queue():
    loc_id = request.args.get('loc_id') or session.get('loc_id')
    if not loc_id:
        return jsonify({"now_serving": "---", "waiting": []})

    pht_now = datetime.now(PHT).replace(tzinfo=None)
    threshold = pht_now - timedelta(minutes=15)

    # Join with Booking table to filter out Out-Plant release entries
    latest_release = Queue.query.join(Queue.booking).filter(
        Queue.location_id == loc_id,
        Queue.status == 'served',
        Booking.service_location != 'Out-Plant'
    ).order_by(Queue.end_time.desc()).first()

    if not latest_release:
        latest_release = Queue.query.join(Queue.booking).filter(
            Queue.location_id == loc_id,
            Queue.status == 'done',
            Queue.end_time >= threshold,
            Booking.service_location != 'Out-Plant'
        ).order_by(Queue.end_time.desc()).first()

    # Filter the queue list to include only In-Plant active items
    active_queue = Queue.query.join(Queue.booking).filter(
        Queue.location_id == loc_id,
        Queue.status.in_(['waiting', 'serving', 'served']),
        Booking.service_location != 'Out-Plant'
    ).order_by(Queue.created_at.asc()).limit(5).all()

    return jsonify({
        "now_serving": latest_release.ticket_number if latest_release else "---",
        "now_serving_plate": latest_release.booking.plate_number if (latest_release and latest_release.booking) else "",
        "call_count": latest_release.call_count if latest_release else 0,
        "waiting": [
            {
                "ticket": t.ticket_number,
                "plate": t.booking.plate_number if t.booking else "WALK-IN",
                "status": t.status
            } for t in active_queue
        ]
    })


@app.route('/staff/users', methods=['GET', 'POST'])
@login_required
@permission_required('users')
def staff_users():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        company_name = request.form.get('company_name')

        if not all([username, password, role, full_name, email]):
            flash("Enrollment Error: All fields are required to provision a new node.", "danger")
            return redirect(url_for('staff_users'))

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Conflict Error: Username or Email is already registered in the Global Directory.", "danger")
            return redirect(url_for('staff_users'))

        hashed_pw = generate_password_hash(password)
        new_user = User(
            username=username,
            password_hash=hashed_pw,
            role=role,
            full_name=full_name,
            email=email,
            company_name=company_name if role == 'customer' else "Coolaire Consolidated Inc.",
            is_approved=True
        )

        try:
            db.session.add(new_user)
            db.session.commit()

            log_action("Identity Provisioned", f"Super Admin created new {role} node: {username}")
            flash(f"Success: Identity for {full_name} has been provisioned as {role.upper()}.", "success")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Enrollment Error: {e}")
            flash("System Error: Could not save record to database.", "danger")

        return redirect(url_for('staff_users'))

    all_users = User.query.order_by(User.role.asc(), User.full_name.asc()).all()

    return render_template(
        'staff_users.html',
        users=all_users,
        title="Identity Registry",
        now_utc=datetime.now(timezone.utc)
    )


@app.before_request
def update_last_seen():
    if current_user.is_authenticated and current_user.role in ['admin', 'coordinator', 'advisor', 'super_admin']:
        now = datetime.now(timezone.utc)
        last_update = current_user.last_seen

        should_update = False
        if not last_update:
            should_update = True
        else:
            if last_update.tzinfo is None:
                last_update = last_update.replace(tzinfo=timezone.utc)

            if (now - last_update).total_seconds() > 60:
                should_update = True

        if should_update:
            try:
                current_user.last_seen = now
                if 'loc_id' in session:
                    current_user.current_loc_id = session.get('loc_id')
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Telemetry Update Error: {e}")


@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()


# --- STAFF TECHNICIAN MANAGEMENT & TRANSFERS ---

@app.route('/staff/technicians', methods=['GET', 'POST'])
@login_required
@permission_required('technicians')
def staff_technicians():
    """
    Global Technician Registry: Manages all technicians across all physical locations
    without forcing session hub switches.
    """
    if request.method == 'POST':
        name = request.form.get('tech_name')
        target_location_id = request.form.get('location_id')

        if name and target_location_id:
            new_tech = Technician(name=name, location_id=int(target_location_id))
            db.session.add(new_tech)
            db.session.commit()

            # Log audit trail action
            target_loc = db.session.get(Location, int(target_location_id))
            log_action("Personnel Onboarded",
                       f"Staff onboarded technician {name} to hub: {target_loc.name if target_loc else 'Unassigned'}")

            flash(f"Technician {name} successfully onboarded to system.", "success")
        return redirect(url_for('staff_technicians'))

    # Query ALL technicians across ALL branches (preloading assignment details)
    # Preload the customer details within technician active tasks
    techs = Technician.query.options(
        db.joinedload(Technician.branch),
        db.selectinload(Technician.tasks).joinedload(Queue.booking).joinedload(Booking.customer)
    ).order_by(Technician.location_id.asc(), Technician.name.asc()).all()

    # Query ALL locations for select/assignment dropdowns
    all_locations = Location.query.order_by(Location.name.asc()).all()

    return render_template(
        'staff_technicians.html',
        technicians=techs,
        locations=all_locations,
        title="Manage Technicians"
    )


@app.route('/staff/technicians/edit/<int:id>', methods=['POST'])
@login_required
@permission_required('technicians')
@csrf.exempt
def edit_technician(id):
    tech = db.session.get(Technician, id)
    if not tech:
        flash("Technician not found.", "danger")
        return redirect(url_for('staff_technicians'))

    new_name = request.form.get('tech_name')
    new_location_id = request.form.get('location_id')

    if new_name:
        tech.name = new_name.strip()

    if new_location_id:
        try:
            tech.location_id = int(new_location_id)
        except ValueError:
            pass

    try:
        db.session.commit()
        flash(f"Technician updates deployed successfully.", "success")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error executing technician re-assignment: {e}")
        flash("System Error: Could not save re-assignment changes.", "danger")

    return redirect(url_for('staff_technicians'))


@app.route('/staff/technicians/delete/<int:id>')
@login_required
@permission_required('technicians')
def delete_technician(id):
    """
    Offboards and deletes a technician from the global database registry.
    Generates an immutable security audit log entry.
    """
    tech = db.session.get(Technician, id)
    if not tech:
        flash("Error: Technician record not found.", "danger")
        return redirect(url_for('staff_technicians'))

    # Allow deletion if the technician belongs to the current hub OR if the user is admin/super_admin (Global Registry Bypass)
    is_admin = current_user.role in ['admin', 'super_admin']
    if tech.location_id == session.get('loc_id') or is_admin:
        tech_name = tech.name
        branch_name = tech.branch.name if tech.branch else "Floating / Unassigned"

        try:
            # Delete record
            db.session.delete(tech)
            db.session.commit()

            # LOG TRANSACTION TO SECURITY AUDIT TRAIL FOR SECURE PERSONNEL COMPLIANCE
            log_action(
                action="Personnel Offboarded",
                details=f"Technician {tech_name} was offboarded and deleted from branch hub: {branch_name}."
            )

            flash(f"Technician {tech_name} has been successfully offboarded.", "success")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error offboarding technician {id}: {e}")
            flash("System Error: Could not execute personnel offboarding.", "danger")
    else:
        flash("Unauthorized access: You do not have permission to offboard this technician.", "danger")

    return redirect(url_for('staff_technicians'))


@app.route('/staff/expire-ticket/<int:q_id>')
@login_required
def expire_ticket(q_id):
    loc_id = session.get('loc_id')
    q = db.session.get(Queue, q_id)

    if q and q.location_id == loc_id:
        q.status = 'expired'
        if q.booking:
            q.booking.status = 'missed'

        db.session.commit()

        log_action(
            action="Ticket Expired",
            details=f"Marked Ticket {q.ticket_number} ({q.booking.plate_number if q.booking else 'WALK-IN'}) as Expired/No-Show.",
            ticket_number=q.ticket_number,
            plate_number=q.booking.plate_number if q.booking else 'WALK-IN'
        )

        flash(f"Ticket {q.ticket_number} marked as Expired/No-Show.", "warning")

    return redirect(url_for('staff_panel'))


@app.route('/staff/revert-ticket/<int:q_id>')
@login_required
def revert_ticket(q_id):
    q = db.session.get(Queue, q_id)
    if not q:
        flash("Error: Ticket not found.", "danger")
        return redirect(request.referrer or url_for('staff_panel'))

    is_admin = current_user.role in ['admin', 'super_admin']
    if q.location_id == session.get('loc_id') or is_admin:
        q.status = 'waiting'

        q.start_time = None
        q.end_time = None
        q.assigned_techs = []

        if q.booking:
            q.booking.status = 'arrived'

        db.session.commit()

        log_action(
            action="Ticket Reverted",
            details=f"Ticket {q.ticket_number} returned to the waiting queue.",
            location_id=q.location_id,
            ticket_number=q.ticket_number,
            plate_number=q.booking.plate_number if q.booking else 'WALK-IN'
        )

        flash(f"Ticket {q.ticket_number} successfully returned to the waiting queue.", "success")
    else:
        flash("Unauthorized access: You do not have permission to modify this node.", "danger")

    return redirect(request.referrer or url_for('staff_panel'))


def notify_customer(user, plate_number, status_type, queue_id=None, ticket_number=None, booking_list=None,
                    reset_url=None):
    if user is None:
        return

    loc_name = session.get('location_name', 'Coolaire Service Center')
    user_id = user.id
    app_instance = current_app._get_current_object()

    def run_notifications(app_ctx, u_id, l_name, r_url, q_id):
        with app_ctx.app_context():
            db_user = db.session.get(User, u_id)
            if not db_user:
                return

            settings = {s.key: s.value for s in SystemSetting.query.all()}

            subject = "Coolaire System Update"
            msg_text = ""
            info_box_html = ""

            if status_type == 'booking_confirmation' and booking_list:
                subject = "Deployment Confirmed: Service Schedule"
                msg_text = "Your fleet deployment manifest has been successfully processed. Below are your scheduled service slots:"

                rows = ""
                for b in booking_list:
                    rows += f"""
                    <tr>
                        <td style="padding: 10px; border-bottom: 1px solid #eee;">{b['plate']}</td>
                        <td style="padding: 10px; border-bottom: 1px solid #eee;">{b['hub']}</td>
                        <td style="padding: 10px; border-bottom: 1px solid #eee;">{b['service']}</td>
                        <td style="padding: 10px; border-bottom: 1px solid #eee;">{b['time']}</td>
                    </tr>"""

                info_box_html = f"""
                <table style="width: 100%; border-collapse: collapse; margin-top: 20px; font-size: 13px;">
                    <thead>
                        <tr style="background: #f8fafc; text-align: left;">
                            <th style="padding: 10px;">Asset</th>
                            <th style="padding: 10px;">Hub</th>
                            <th style="padding: 10px;">Service</th>
                            <th style="padding: 10px;">Schedule</th>
                        </tr>
                    </thead>
                    <tbody>{rows}</tbody>
                </table>"""

            elif status_type == 'registration_pending':
                subject = "Coolaire Registration: Pending"
                msg_text = f"Hello {db_user.full_name}, we have received your application for {db_user.company_name}. Access is currently under review."

            elif status_type == 'account_approved':
                subject = "Coolaire Account: Active"
                msg_text = f"Great news! Your account for {db_user.company_name} is now active. You may now log in to the Partner Portal."

            elif status_type == 'arrived_kiosk':
                subject = f"Arrival Registered: {plate_number}"
                msg_text = f"Your driver has checked in unit {plate_number} at the main kiosk. Your asset is now queued for workshop dispatch."
                info_box_html = f"<p><b>Hub:</b> {l_name}<br><b>Ticket:</b> {ticket_number}</p>"

            elif status_type == 'serving':
                subject = f"Service Started: {plate_number}"
                msg_text = f"Update: Your unit {plate_number} is now being serviced."
                info_box_html = f"<p><b>Hub:</b> {l_name}<br><b>Ticket:</b> {ticket_number}</p>"

            elif status_type == 'done':
                subject = f"Service Complete: {plate_number}"
                msg_text = f"Service for unit {plate_number} is finished. Please proceed to the release bay."

            elif status_type == 'password_reset':
                subject = "Secure Password Reset Request"
                msg_text = f"We received a request to reset your Coolaire Partner Portal password."
                info_box_html = f"""
                <div style="text-align: center; margin-top: 30px;">
                    <a href="{r_url}" style="background: #002d72; color: white; padding: 12px 25px; text-decoration: none; border-radius: 8px; font-weight: bold; display: inline-block;">
                        RESET MY PASSWORD
                    </a>
                    <p style="font-size: 11px; color: #999; margin-top: 15px;">This link will expire in 30 minutes.</p>
                </div>"""
            else:
                return

            mail_user = settings.get('MAIL_HOST_USER')
            mail_pass = settings.get('MAIL_HOST_PASSWORD')
            mail_server = settings.get('MAIL_SERVER', 'mail.coolaireconsolidated.com')
            mail_port = int(settings.get('MAIL_PORT', 465))

            log_status = 'failed'
            error_msg = None

            if mail_user and mail_pass and db_user.email:
                try:
                    msg = MIMEMultipart('related')
                    msg['From'] = f"Coolaire Fleet <{mail_user}>"
                    msg['To'] = db_user.email
                    msg['Subject'] = subject

                    html = f"""
                    <html>
                        <body style="font-family: sans-serif; color: #333; line-height: 1.6; padding: 20px; background: #f4f4f4;">
                            <div style="max-width: 600px; margin: auto; background: #fff; padding: 30px; border-radius: 10px; border: 1px solid #ddd;">
                                <h2 style="color: #002d72; margin-top: 0;">System Update</h2>
                                <p>Dear <strong>{db_user.full_name}</strong>,</p>
                                <p>{msg_text}</p>
                                {info_box_html}
                                <p style="margin-top: 30px; font-size: 12px; color: #777;">
                                    This is an automated message. Please do not reply.<br>
                                    &copy; {datetime.now().year} Coolaire Consolidated Inc.
                                </p>
                            </div>
                        </body>
                    </html>"""
                    msg.attach(MIMEText(html, 'html'))

                    with smtplib.SMTP_SSL(mail_server, mail_port) as server:
                        server.login(mail_user, mail_pass)
                        server.sendmail(mail_user, db_user.email, msg.as_string())
                    log_status = 'success'
                except Exception as e:
                    log_status = 'failed'
                    error_msg = str(e)
                    print(f"!!! SMTP Error: {e}")
            else:
                log_status = 'failed'
                error_msg = "SMTP Configuration missing or recipient email empty."

            try:
                new_log = NotificationLog(
                    queue_id=q_id,
                    recipient=db_user.email if db_user.email else "N/A",
                    channel="email",
                    status=log_status,
                    error_message=error_msg
                )
                db.session.add(new_log)
                db.session.commit()
            except Exception as log_err:
                db.session.rollback()
                print(f"!!! Failed to save Notification Log: {log_err}")

    threading.Thread(target=run_notifications, args=(app_instance, user_id, loc_name, reset_url, queue_id)).start()


@app.route('/staff/notifications')
@login_required
@permission_required('notifications')
def staff_notifications():
    loc_id = session.get('loc_id')

    logs = NotificationLog.query.outerjoin(Queue).filter(
        (Queue.location_id == loc_id) | (NotificationLog.queue_id == None)
    ).order_by(NotificationLog.created_at.desc()).limit(100).all()

    return render_template('staff_notifications.html', logs=logs, title="Messaging Audit")


@app.route('/staff/verify-center')
@login_required
@permission_required('verify_center')
def verify_center():
    pending = User.query.filter_by(is_approved=False).order_by(User.created_at.asc()).all()
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
        user_to_verify.is_approved = True
        user_to_verify.is_rejected = False
        db.session.commit()

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
        user_to_verify.is_approved = False
        user_to_verify.is_rejected = True
        db.session.commit()

        app.logger.info(f"Identity Blocked: {user_to_verify.full_name} was rejected by {current_user.username}")
        flash(f"Identity REJECTED: {user_to_verify.full_name} has been moved to the Rejected Archive.", "warning")

    return redirect(url_for('verify_center'))


@app.route('/staff/archive')
@login_required
@permission_required('staff_archived')
def staff_archive():
    rejected_users = User.query.filter_by(is_rejected=True).order_by(User.created_at.desc()).all()
    return render_template('staff_archive.html', users=rejected_users, title="Rejected Identity Archive")


@app.route('/staff/archive/purge/<int:user_id>')
@login_required
@permission_required('staff_archived')
def purge_user(user_id):
    if current_user.role != 'admin': abort(403)
    u = db.session.get(User, user_id)
    if u:
        db.session.delete(u)
        db.session.commit()
        flash("Record permanently purged from the system.", "danger")
    return redirect(url_for('staff_archive'))


def log_action(action, details, location_id=None, ticket_number=None, plate_number=None):
    """ Records an entry into the immutable security audit trail """
    ip = '127.0.0.1'
    ua = 'System Automation'
    device = 'BAS Server'

    if request:
        try:
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            if ip and ',' in ip:
                ip = ip.split(',')[0].strip()

            ua = request.headers.get('User-Agent', 'Unknown')

            ua_lower = ua.lower()
            if 'mobile' in ua_lower or 'android' in ua_lower or 'iphone' in ua_lower:
                device = 'Mobile Device'
            elif 'tablet' in ua_lower or 'ipad' in ua_lower:
                device = 'Tablet Terminal'
            else:
                device = 'Workshop Terminal'
        except Exception:
            pass

    try:
        new_log = AuditLog(
            location_id=location_id or session.get('loc_id'),
            user_id=current_user.id if current_user.is_authenticated else None,
            action=action,
            details=details,
            ticket_number=ticket_number,
            plate_number=plate_number,
            ip_address=ip,
            user_agent=ua,
            device_type=device
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
    logs = AuditLog.query.options(
        db.joinedload(AuditLog.location),
        db.joinedload(AuditLog.performer)
    ).order_by(AuditLog.created_at.desc()).limit(500).all()

    return render_template('staff_audit_trail.html', logs=logs, title="Security Audit Trail")


@app.route('/staff/global-bookings')
@login_required
@permission_required('global_bookings')
def global_bookings():
    try:
        all_bookings = Booking.query.options(
            db.joinedload(Booking.location),
            db.joinedload(Booking.customer),
            db.joinedload(Booking.associated_vehicle)
        ).order_by(Booking.scheduled_time.desc()).all()

    except Exception as e:
        app.logger.error(f"Global Ledger Query Error: {e}")
        flash("System Error: Could not retrieve global deployment data.", "danger")
        all_bookings = []

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

        state_str = "ACTIVE" if tech.is_present else "OFF-DUTY"
        log_action(
            action="Roster State Toggled",
            details=f"Staff changed roster state of Technician {tech.name} to {state_str}."
        )

    return redirect(url_for('staff_panel'))


@app.route('/staff/save-notes/<int:q_id>', methods=['POST'])
@login_required
def save_job_notes(q_id):
    q = db.session.get(Queue, q_id)
    if q and q.location_id == session.get('loc_id'):
        notes_content = request.form.get('notes')
        q.internal_notes = notes_content
        db.session.commit()

        log_action(
            action="Office Notes Updated",
            details=f"Staff updated internal diagnostic/billing notes on Ticket {q.ticket_number}.",
            ticket_number=q.ticket_number,
            plate_number=q.booking.plate_number if q.booking else 'WALK-IN'
        )

        flash("Notes updated.", "success")
    return redirect(url_for('staff_panel'))


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
        existing = Booking.query.filter_by(ref_id=ns_data['jo_number']).first()
        if existing:
            flash(f"Job Order {jo_number} is already synced.", "info")
            return redirect(url_for('staff_panel'))

        customer = User.query.filter(User.company_name.ilike(ns_data['client_name'])).first()
        if not customer:
            flash(f"Client {ns_data['client_name']} not found in QBMS. Please register them first.", "danger")
            return redirect(url_for('staff_panel'))

        new_booking = Booking(
            user_id=customer.id, location_id=loc_id, plate_number=ns_data['plate_number'],
            service_type=ns_data['erp_status'], status='pending', ref_id=ns_data['jo_number'],
            date_of_entry=datetime.now(PHT).date(),
            preferred_service_date=datetime.now(PHT).date()
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
        ('technicians', 'Manage Technicians'),
        ('locations', 'Manage Branches'),
        ('start-work', 'Dispatch Work / Start Floor Job'),
        ('recall-ticket', 'Recall Tickets on TV Monitor')
    ]

    if request.method == 'POST':
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
            now = datetime.now(timezone.utc)
            if device_type == 'kiosk':
                loc.kiosk_last_seen = now
            elif device_type == 'tv':
                loc.tv_last_seen = now

            db.session.commit()
            return jsonify({"status": "ok", "time": now.isoformat()})

    return jsonify({"status": "error"}), 400


@app.route('/staff/reports')
@login_required
@roles_required('super_admin', 'admin', 'coordinator')
def staff_reports():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    service_query = db.session.query(
        func.to_char(Booking.scheduled_time, 'YYYY-MM').label('month'),
        func.count(Booking.id).filter(Booking.service_location == 'In-Plant').label('in_plant_count'),
        func.count(Booking.id).filter(Booking.service_location == 'Out-Plant').label('out_plant_count'),
        func.count(Booking.id).label('grand_total')
    ).filter(Booking.status == 'done')

    tech_query = db.session.query(
        Technician.name,
        func.count(Queue.id).label('total_completed')
    ).join(queue_technicians, Technician.id == queue_technicians.c.technician_id) \
        .join(Queue, Queue.id == queue_technicians.c.queue_id) \
        .filter(Queue.status == 'done')

    company_query = db.session.query(
        User.company_name,
        Booking.service_location,
        func.count(Booking.id).label('total_units')
    ).join(User, Booking.user_id == User.id).filter(Booking.status == 'done')

    if start_date and end_date:
        service_query = service_query.filter(Booking.scheduled_time.between(start_date, end_date))
        tech_query = tech_query.filter(Queue.created_at.between(start_date, end_date))
        company_query = company_query.filter(Booking.scheduled_time.between(start_date, end_date))

    service_stats = service_query.group_by('month').order_by(db.desc('month')).all()
    tech_performance = tech_query.group_by(Technician.name).order_by(db.desc('total_completed')).all()
    company_audit = company_query.group_by(User.company_name, Booking.service_location).order_by(
        User.company_name.asc()).all()

    return render_template('staff_reports.html',
                           service_stats=service_stats,
                           tech_performance=tech_performance,
                           company_audit=company_audit,
                           start_date=start_date, end_date=end_date, title="Operations Performance")


@app.route('/staff/users/edit/<int:user_id>', methods=['POST'])
@login_required
@permission_required('users')
def edit_user(user_id):
    u = db.session.get(User, user_id)
    if not u:
        flash("User not found.", "danger")
        return redirect(url_for('staff_users'))

    u.full_name = request.form.get('full_name')
    u.company_name = request.form.get('company_name')

    try:
        db.session.commit()
        log_action("Identity Updated", f"Updated details for {u.username}")
        flash(f"Credentials for {u.full_name} updated.", "success")
    except Exception as e:
        db.session.rollback()
        flash("Update failed.", "danger")

    return redirect(url_for('staff_users'))


def get_serializer():
    return URLSafeTimedSerializer(app.config['SECRET_KEY'])


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        user = User.query.filter_by(email=email).first()

        if user:
            s = get_serializer()
            token = s.dumps(user.email, salt='password-reset-salt')
            reset_url = url_for('reset_password', token=token, _external=True)

            try:
                notify_customer(
                    user=user,
                    plate_number="N/A",
                    status_type='password_reset',
                    reset_url=reset_url
                )
                flash("A reset link has been sent to your email.", "success")
            except Exception as e:
                app.logger.error(f"Reset Email Error: {e}")
                flash("Failed to send email. Please contact support.", "danger")
        else:
            flash("If that email is registered, a link has been sent.", "info")

        return redirect(url_for('login'))

    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    s = get_serializer()
    try:
        email = s.loads(token, salt='password-reset-salt', max_age=1800)
    except:
        flash("The reset link is invalid or has expired.", "danger")
        return redirect(url_for('login'))

    if request.method == 'POST':
        user = User.query.filter_by(email=email).first()
        new_password = request.form.get('password')

        if user and new_password:
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash("Your password has been updated. You may now login.", "success")
            return redirect(url_for('login'))

    return render_template('password_reset.html', token=token)


@app.route('/logout')
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('login'))


if __name__ == '__main__':
    with app.app_context():
        db.create_all()

        try:
            inspector = db.inspect(db.engine)
            existing_columns = [c['name'] for c in inspector.get_columns('bookings')]

            if 'kiosk_last_seen' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE locations ADD COLUMN kiosk_last_seen TIMESTAMP'))
            if 'tv_last_seen' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE locations ADD COLUMN tv_last_seen TIMESTAMP'))
            db.session.commit()

            if 'plate_number' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN plate_number VARCHAR(50)'))
                db.session.commit()
                print("--- Database Updated: Added plate_number ---")

            if 'guest_name' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN guest_name VARCHAR(150)'))
                db.session.commit()
                print("--- Database Updated: Added guest_name ---")

            if 'service_location' not in existing_columns:
                db.session.execute(
                    db.text("ALTER TABLE bookings ADD COLUMN service_location VARCHAR(50) DEFAULT 'In-Plant'"))
                db.session.commit()
                print("--- Database Updated: Added service_location ---")

            if 'job_order' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN job_order VARCHAR(50)'))
                db.session.commit()
                print("--- Database Updated: Added job_order ---")

            if 'std_repair_hours' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN std_repair_hours FLOAT DEFAULT 0.0'))
                db.session.commit()
                print("--- Database Updated: Added std_repair_hours ---")

            # --- DYNAMIC COLUMNS FOR THE SERVICE & ENTRY DATES ---
            if 'date_of_entry' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN date_of_entry DATE'))
                db.session.commit()
                print("--- Database Updated: Added date_of_entry ---")

            if 'preferred_service_date' not in existing_columns:
                db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN preferred_service_date DATE'))
                db.session.commit()
                print("--- Database Updated: Added preferred_service_date ---")

            # --- NEW MIGRATIONS FOR THE DATE OF WORK COLUMN ---
            queue_cols = [c['name'] for c in inspector.get_columns('queues')]
            if 'date_of_work' not in queue_cols:
                db.session.execute(db.text('ALTER TABLE queues ADD COLUMN date_of_work DATE'))
                db.session.commit()
                print("--- Database Updated: Added date_of_work ---")

            # --- AUDIT LOG COLUMNS ---
            audit_cols = [c['name'] for c in inspector.get_columns('audit_logs')]
            if 'ip_address' not in audit_cols:
                db.session.execute(db.text('ALTER TABLE audit_logs ADD COLUMN ip_address VARCHAR(50)'))
                db.session.commit()
                print("--- Database Updated: Added ip_address to audit_logs ---")

            if 'user_agent' not in audit_cols:
                db.session.execute(db.text('ALTER TABLE audit_logs ADD COLUMN user_agent TEXT'))
                db.session.commit()
                print("--- Database Updated: Added user_agent to audit_logs ---")

            if 'device_type' not in audit_cols:
                db.session.execute(db.text('ALTER TABLE audit_logs ADD COLUMN device_type VARCHAR(50)'))
                db.session.commit()
                print("--- Database Updated: Added device_type to audit_logs ---")

            if 'ticket_number' not in audit_cols:
                db.session.execute(db.text('ALTER TABLE audit_logs ADD COLUMN ticket_number VARCHAR(20)'))
                db.session.commit()
                print("--- Database Updated: Added ticket_number to audit_logs ---")

            if 'plate_number' not in audit_cols:
                db.session.execute(db.text('ALTER TABLE audit_logs ADD COLUMN plate_number VARCHAR(50)'))
                db.session.commit()
                print("--- Database Updated: Added plate_number to audit_logs ---")

            # --- PREFERRED TIME COLUMN MIGRATION ---
            if 'preferred_service_time' not in existing_columns:
                db.session.execute(
                    db.text("ALTER TABLE bookings ADD COLUMN preferred_service_time VARCHAR(10) DEFAULT '08:00'"))
                db.session.commit()
                print("--- Database Updated: Added preferred_service_time ---")

            # --- ADVISOR MIGRATIONS ---
            if 'advisor_id' not in existing_columns:
                db.session.execute(db.text(
                    "ALTER TABLE bookings ADD COLUMN advisor_id INTEGER REFERENCES advisors(id) ON DELETE SET NULL"))
                db.session.commit()
                print("--- Database Updated: Added advisor_id to bookings ---")

        except Exception as e:
            print(f"--- Database Migration Note: {e} ---")
            db.session.rollback()

    app.run(debug=True, port=5000)