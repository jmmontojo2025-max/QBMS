import os
import requests
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, extract
from flask_wtf.csrf import CSRFProtect
from datetime import timedelta
from sqlalchemy import func
import random
import string



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


# Add this model if you haven't already to keep track of customer units
class Vehicle(db.Model):
    __tablename__ = 'vehicles'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    plate_number = db.Column(db.String(50))
    model_description = db.Column(db.String(150))



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
    bookings = db.relationship('Booking', backref='customer', lazy=True)
    last_seen = db.Column(db.DateTime)
    current_loc_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=True)


class ServiceCategory(db.Model):
    __tablename__ = 'service_categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True)


import random
import string


class Booking(db.Model):
    __tablename__ = 'bookings'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'))
    vehicle_id = db.Column(db.Integer, db.ForeignKey('vehicles.id'))
    plate_number = db.Column(db.String(50))
    service_type = db.Column(db.String(255))
    status = db.Column(db.String(20), default='pending')
    scheduled_time = db.Column(db.DateTime)

    # THIS LINE MATCHES THE SQL WE JUST RAN
    ref_id = db.Column(db.String(10), unique=True)

    # THIS FUNCTION AUTOMATICALLY CREATES THE CODE FOR NEW BOOKINGS
    def __init__(self, **kwargs):
        super(Booking, self).__init__(**kwargs)
        if not self.ref_id:
            # Generates a unique 4-digit number for Kiosk Check-in
            self.ref_id = ''.join(random.choices(string.digits, k=4))

    def __init__(self, **kwargs):
        super(Booking, self).__init__(**kwargs)
        if not self.ref_id:
            # Generates a 4-digit unique code for Kiosk check-in
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
    booking = db.relationship('Booking', backref='queue_entry')

    # NEW PLURAL RELATIONSHIP
    assigned_techs = db.relationship('Technician', secondary=queue_technicians, backref='tasks')


class SystemSetting(db.Model):
    __tablename__ = 'system_settings'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True)
    value = db.Column(db.Text)



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
    # If a user is already authenticated, direct them based on role
    if current_user.is_authenticated:
        if current_user.role == 'customer':
            return redirect(url_for('dashboard'))
        elif current_user.role in ['staff', 'admin']:
            # Staff/Admin logged in: check if they have a branch selected
            if 'loc_id' in session:
                return redirect(url_for('staff_panel'))
            else:
                flash("Please select your operating branch.", "info")
                return redirect(url_for('select_branch_for_staff'))

    # Unauthenticated users go straight to customer login
    return redirect(url_for('login'))


# This route is specifically for staff to select their branch *after* logging in.
@app.route('/select-branch-for-staff')
@login_required  # Only authenticated users (staff/admin) can access this.
def select_branch_for_staff():
    if current_user.role not in ['staff', 'admin']:
        flash("You do not have permission to access the Staff Branch Selector.", "danger")
        return redirect(url_for('login'))

    # If a branch is already selected, send them to staff panel
    if 'loc_id' in session:
        return redirect(url_for('staff_panel'))

    locations = Location.query.all()
    return render_template('location_select.html', locs=locations)


# This route sets the branch in the session for the logged-in user.
@app.route('/set-branch/<int:loc_id>')
@login_required
def set_branch(loc_id):
    # 1. Logging for the console
    app.logger.info(f"User {current_user.username} (Role: {current_user.role}) attempting to set branch {loc_id}")

    # 2. Fetch the location from the database
    loc = db.session.get(Location, loc_id)

    # 3. Error Handling if the branch ID doesn't exist
    if not loc:
        flash("Invalid branch selected.", "danger")
        app.logger.warning(f"Invalid branch ID {loc_id} attempted by {current_user.username}")
        if current_user.role in ['staff', 'admin']:
            return redirect(url_for('select_branch_for_staff'))
        return redirect(url_for('login'))

    # 4. UPDATE SESSION KEYS (This fixes the "Branch Not Set" issue)
    session['loc_id'] = loc.id
    session['location_name'] = loc.name  # Used by {{ session.get('location_name') }}
    session['location_code'] = loc.code  # Used by {{ session.get('location_code') }}

    # Optional: Backup keys for older parts of your code
    session['loc_name'] = loc.name
    session['loc_code'] = loc.code

    # 5. Ensure the session cookie is saved to the browser
    session.modified = True

    app.logger.info(f"Branch successfully set to: {loc.name}")
    flash(f"Operating branch set to {loc.name}.", "success")

    # 6. Redirect based on user role
    if current_user.role in ['staff', 'admin']:
        return redirect(url_for('staff_panel'))

    return redirect(url_for('dashboard'))


# --- STAFF OPERATIONS ---

@app.route('/staff')
@login_required
def staff_panel():
    """Main terminal for branch operations with auto-cleanup logic."""
    # 1. Security check: Only staff and admin
    if current_user.role not in ['staff', 'admin']:
        abort(403)

    # 2. Check if a branch is set in the session
    if 'loc_id' not in session:
        flash("Please select a branch first.", "warning")
        return redirect(url_for('select_branch_for_staff'))

    loc_id = session.get('loc_id')

    # 3. AUTO-CLEANUP: Expire stale tickets from previous days
    # This ensures that 'Waiting' tickets from yesterday don't clutter today's live queue.
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    stale_tickets = Queue.query.filter(
        Queue.location_id == loc_id,
        Queue.status == 'waiting',
        Queue.created_at < today_start
    ).all()

    if stale_tickets:
        for ticket in stale_tickets:
            ticket.status = 'expired'
            # Also update the associated booking if it exists
            if ticket.booking:
                ticket.booking.status = 'missed'
        db.session.commit()
        app.logger.info(f"Cleaned up {len(stale_tickets)} stale tickets for location {loc_id}")

    # 4. Fetch live data for the dashboard
    waiting = Queue.query.filter_by(location_id=loc_id, status='waiting').order_by(Queue.created_at.asc()).all()
    serving = Queue.query.filter_by(location_id=loc_id, status='serving').all()
    techs = Technician.query.filter_by(location_id=loc_id, is_active=True).all()

    # 5. Render the template
    return render_template('staff.html',
                           waiting_tickets=waiting,
                           serving_list=serving,
                           technicians=techs,
                           title="Live Console")


# --- CUSTOMER PORTAL LOGIN (Strictly Customers Only) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    # If customer is already logged in, send to dashboard
    if current_user.is_authenticated and current_user.role == 'customer':
        return redirect(url_for('dashboard'))

    # If staff/admin is already logged in, redirect them to branch selection
    # or staff panel if branch is already selected
    if current_user.is_authenticated and current_user.role in ['staff', 'admin']:
        if 'loc_id' in session:
            return redirect(url_for('staff_panel'))
        else:
            return redirect(url_for('select_branch_for_staff'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            if user.role in ['staff', 'admin']:
                flash("This portal is for customers only. Please use the Staff Terminal.", "danger")
                return redirect(url_for('login'))  # Keep them on customer login page

            # Successful customer login
            login_user(user)
            flash(f"Welcome back, {user.full_name}!", "success")
            # Customers are not forced to select a branch immediately in this flow.
            # Their bookings might be linked to locations, but their general dashboard access doesn't require pre-selection.
            return redirect(url_for('dashboard'))

        flash("Invalid credentials. Please try again.", "danger")

    return render_template('login.html')


# --- STAFF TERMINAL LOGIN (Strictly Staff Only) ---
@app.route('/staff/login', methods=['GET', 'POST'])
def staff_login():
    # If staff/admin is already logged in, redirect them to branch selection
    # or staff panel if branch is already selected
    if current_user.is_authenticated and current_user.role in ['staff', 'admin']:
        if 'loc_id' in session:
            return redirect(url_for('staff_panel'))
        else:
            return redirect(url_for('select_branch_for_staff'))

    if request.method == 'POST':
        u = User.query.filter_by(username=request.form.get('username')).first()
        if u and check_password_hash(u.password_hash, request.form.get('password')):
            if u.role in ['staff', 'admin']:
                login_user(u)
                flash(f"Welcome to the Staff Terminal, {u.full_name}!", "success")
                # AFTER SUCCESSFUL STAFF LOGIN: Redirect to branch selection
                return redirect(url_for('select_branch_for_staff'))
            else:
                flash("Access Denied: Customer accounts cannot use the Staff Terminal.", "danger")
        else:
            flash("Invalid Credentials", "danger")
    return render_template('login_staff.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        hashed_pw = generate_password_hash(request.form.get('password'))
        new_user = User(
            username=request.form.get('username'),
            password_hash=hashed_pw,
            full_name=request.form.get('full_name'),
            phone=request.form.get('phone'),
            email=request.form.get('email'),
            company_name=request.form.get('company_name'),
            role='customer'  # Enforce customer role for self-registration
        )
        db.session.add(new_user)
        db.session.commit()
        flash("Registration successful. Please login.", "success")
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/dashboard')
@login_required
def dashboard():
    if current_user.role != 'customer':
        return redirect(url_for('staff_panel'))

    # 1. Fetch Full History
    bookings = Booking.query.filter_by(user_id=current_user.id) \
        .order_by(Booking.scheduled_time.desc()).all()

    # 2. Generate Forecast Data (Group upcoming bookings by Location)
    # This shows how many trucks the customer has scheduled for each hub
    now = datetime.now(timezone.utc)
    forecast_results = db.session.query(
        Location.name,
        func.count(Booking.id).label('unit_count')
    ).join(Booking).filter(
        Booking.user_id == current_user.id,
        Booking.scheduled_time >= now,
        Booking.status == 'pending'
    ).group_by(Location.name).all()

    # Convert to a dictionary for the UI
    branch_forecast = {name: count for name, count in forecast_results}

    booking_id = request.args.get('booking_id')
    active_booking = db.session.get(Booking, booking_id) if booking_id else None

    return render_template('dashboard.html',
                           service_history=bookings,
                           all_active_bookings=bookings,
                           booking=active_booking,
                           branch_forecast=branch_forecast)  # New variable


from datetime import datetime, timezone


@app.route('/book', methods=['GET', 'POST'])
@login_required
def book():
    if current_user.role != 'customer':
        flash("Access Denied: Staff must use the internal terminal.", "danger")
        return redirect(url_for('staff_panel'))

    if request.method == 'POST':
        time_str = request.form.get('time')
        if not time_str:
            flash("Error: Please select a valid date and arrival window.", "danger")
            return redirect(url_for('book'))

        try:
            v_id = request.form.get('vehicle_id')
            final_plate = ""

            # ASSET LOGIC
            if v_id == 'new':
                new_plate = request.form.get('new_plate', '').strip().upper()
                new_model = request.form.get('new_model', 'Standard Asset').strip()

                # Register new vehicle with MODEL
                vehicle_obj = Vehicle(
                    user_id=current_user.id,
                    plate_number=new_plate,
                    model_description=new_model
                )
                db.session.add(vehicle_obj)
                db.session.flush()
                v_id = vehicle_obj.id
                final_plate = vehicle_obj.plate_number
            else:
                v_obj = db.session.get(Vehicle, v_id)
                v_id = v_obj.id
                final_plate = v_obj.plate_number

            # INITIALIZE BOOKING with vehicle_id
            new_booking = Booking(
                user_id=current_user.id,
                location_id=request.form.get('location_id'),
                vehicle_id=v_id,  # Error Fixed Here
                plate_number=final_plate,
                service_type=request.form.get('product'),
                scheduled_time=datetime.fromisoformat(time_str),
                status='pending'
            )
            db.session.add(new_booking)
            db.session.commit()
            flash(f"Deployment Initialized for Unit {final_plate}", "success")
            return redirect(url_for('dashboard'))

        except Exception as e:
            db.session.rollback()
            print(f"Booking Error: {e}")
            flash("System Error: Could not process appointment.", "danger")
            return redirect(url_for('book'))

    categories = ServiceCategory.query.all()
    locations = Location.query.all()
    my_fleet = Vehicle.query.filter_by(user_id=current_user.id).all()
    return render_template('book.html', categories=categories, locations=locations, vehicles=my_fleet)

    # 5. GET LOGIC: Fetch parameters for the UI
    categories = ServiceCategory.query.order_by(ServiceCategory.name).all()
    locations = Location.query.order_by(Location.name).all()
    # Fetch only vehicles belonging to this customer
    my_fleet = Vehicle.query.filter_by(user_id=current_user.id).order_by(Vehicle.plate_number).all()

    return render_template('book.html',
                           categories=categories,
                           locations=locations,
                           vehicles=my_fleet,
                           title="New Deployment")


# --- STAFF OPERATIONS (Require Staff/Admin Role and Selected Location) ---

# Helper to check staff access and location
def require_staff_location():
    if current_user.is_authenticated and current_user.role not in ['staff', 'admin']:
        abort(403) # Not staff/admin, but logged in
    if not current_user.is_authenticated:
        return redirect(url_for('login')) # Not logged in
    if current_user.role in ['staff', 'admin'] and 'loc_id' not in session:
        flash("Please select your operating branch to proceed.", "warning")
        return redirect(url_for('select_branch_for_staff'))
    return None # No redirect needed, proceed


app.route('/staff')


@app.route('/staff/locations', methods=['GET', 'POST'])
@login_required
def staff_locations():
    # MODIFIED: Both 'admin' and 'staff' can access this page
    if current_user.role not in ['admin', 'staff']:
        abort(403)

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
def edit_location(loc_id):
    # MODIFIED: Allow both 'admin' and 'staff' to edit locations
    if current_user.role not in ['admin', 'staff']:
        abort(403)

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
def delete_location(loc_id):
    # MODIFIED: Allow both 'admin' and 'staff' to delete locations
    if current_user.role not in ['admin', 'staff']:
        abort(403)

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


@app.route('/staff/start-work/<int:q_id>', methods=['POST'])
@login_required
def start_work(q_id):
    # Security/Location checks
    loc_id = session.get('loc_id')

    # Get multiple technician IDs from the form
    tech_ids = request.form.getlist('technician_ids')

    q = db.session.get(Queue, q_id)
    if q and q.location_id == loc_id and tech_ids:
        # Fetch all selected technician objects
        techs = Technician.query.filter(Technician.id.in_(tech_ids)).all()

        q.assigned_techs = techs  # Assign the list of techs
        q.status = 'serving'
        q.start_time = datetime.now(timezone.utc)

        db.session.commit()

        # SMS Notification
        if q.booking and q.booking.customer.phone:
            send_sms(q.booking.customer.phone,
                     f"Coolaire: Unit {q.booking.plate_number} is now being served by our technical team.")

    return redirect(url_for('staff_panel'))


@app.route('/staff/complete-work/<int:q_id>')
@login_required
def complete_work(q_id):
    redirect_response = require_staff_location()
    if redirect_response: return redirect_response

    q = db.session.get(Queue, q_id)
    # Ensure the queue item belongs to the current staff's location
    if q and q.location_id == session.get('loc_id'):
        q.status, q.end_time = 'done', datetime.now(timezone.utc)
        if q.booking: q.booking.status = 'done'
        db.session.commit()
    else:
        flash("Could not complete work for the requested queue item.", "danger")
    return redirect(url_for('staff_panel'))


@app.route('/staff/records')
@login_required
def staff_records():
    if current_user.role not in ['staff', 'admin']: abort(403)

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
    # Kiosk needs a loc_id to function. If not set, redirect to *some* selection.
    # For a kiosk, it usually means a physical selection on-site or a system default.
    # If 'loc_id' is missing, for the public-facing Kiosk/TV, it should ideally go to a public branch picker.
    # For now, redirecting to login as a simple way to get to a path where loc_id might be set.
    # A dedicated public /select-kiosk-branch/ route and template would be better here.
    if 'loc_id' not in session:
        flash("Kiosk requires a branch to be selected.", "warning")
        # Placeholder: redirect to login or a dedicated *public* branch selector
        return redirect(url_for('login'))
    return render_template('kiosk.html')


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
    if not loc_id:
        return jsonify({"status": "error", "message": "Branch not set"}), 400

    today = datetime.now(timezone.utc).date()
    count = Queue.query.filter_by(location_id=loc_id).filter(func.date(Queue.created_at) == today).count()

    # Format: MKT-W-101
    ticket_no = f"{session.get('location_code', 'CCI')}-W-{101 + count}"

    new_q = Queue(ticket_number=ticket_no, location_id=loc_id, status='waiting')
    db.session.add(new_q)
    db.session.commit()

    return jsonify({"status": "success", "ticket": ticket_no, "q_id": new_q.id})


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
def staff_analytics():
    if current_user.role not in ['staff', 'admin']: abort(403)

    loc_id = session.get('loc_id')
    if not loc_id:
        flash("Please select a branch first.", "warning")
        return redirect(url_for('select_branch_for_staff'))

    today = datetime.now(timezone.utc).date()

    # 1. Real Daily Count
    daily_count = Queue.query.filter(
        Queue.location_id == loc_id,
        func.date(Queue.created_at) == today
    ).count()

    # 2. Real Monthly Count (Current Month)
    monthly_count = Queue.query.filter(
        Queue.location_id == loc_id,
        extract('month', Queue.created_at) == today.month,
        extract('year', Queue.created_at) == today.year
    ).count()

    # 3. Real Previous Month Count (for Momentum)
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - timedelta(days=1)

    prev_month_count = Queue.query.filter(
        Queue.location_id == loc_id,
        extract('month', Queue.created_at) == last_day_prev_month.month,
        extract('year', Queue.created_at) == last_day_prev_month.year
    ).count()

    # 4. Calculate Momentum %
    if prev_month_count > 0:
        momentum = int(((monthly_count - prev_month_count) / prev_month_count) * 100)
    else:
        momentum = 100 if monthly_count > 0 else 0

    # 5. Real Avg Wait Time (Actual Wait from creation to start of service)
    avg_wait = db.session.query(
        func.avg(extract('epoch', Queue.start_time - Queue.created_at) / 60)
    ).filter(
        Queue.location_id == loc_id,
        Queue.start_time.isnot(None)
    ).scalar() or 0

    return render_template('staff_analytics.html',
                           daily_count=daily_count,
                           monthly_count=monthly_count,
                           prev_month_count=prev_month_count,
                           momentum=momentum,
                           avg_wait=round(avg_wait, 1),
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
def staff_users():
    # 1. SECURITY: Only Staff/Admin can manage the Identity Registry
    if current_user.role not in ['staff', 'admin']:
        abort(403)

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')
        full_name = request.form.get('full_name')
        email = request.form.get('email')

        # Basic Validation
        if not all([username, password, role, full_name, email]):
            flash("System Error: All identity parameters are required for enrollment.", "danger")
            return redirect(url_for('staff_users'))

        # Check for Duplicate Identity
        if User.query.filter_by(username=username).first():
            flash(f"Conflict: Username '{username}' is already registered in the system.", "danger")
        else:
            hashed_pw = generate_password_hash(password)

            # Determine Company Association based on Role
            if role in ['staff', 'admin']:
                company = "Coolaire Consolidated Inc."
            else:
                # For customers, try to get the company from a form field if you add one later,
                # otherwise default to Independent
                company = request.form.get('company_name', "Independent Client")

            new_user = User(
                username=username,
                password_hash=hashed_pw,
                role=role,
                full_name=full_name,
                email=email,
                company_name=company
            )

            try:
                db.session.add(new_user)
                db.session.commit()
                flash(f"Success: Identity for {full_name} has been provisioned.", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Database Error: Could not enroll user. {str(e)}", "danger")

        return redirect(url_for('staff_users'))

    # GET Logic: Fetch all nodes for the Directory
    all_users = User.query.order_by(User.role.asc(), User.full_name.asc()).all()

    # CRITICAL: Pass now_utc so the template can calculate "CONNECTED" status
    return render_template(
        'staff_users.html',
        users=all_users,
        title="User Management",
        now_utc=datetime.now(timezone.utc)
    )


# Removed the redundant /staff/settings/update route.
# The POST logic for settings is handled directly in staff_settings.

@app.before_request
def update_last_seen():
    if current_user.is_authenticated and current_user.role in ['staff', 'admin']:
        # Update the user's last seen time to NOW
        current_user.last_seen = datetime.now(timezone.utc)

        # Sync the session location to the database so we know WHERE they are active
        if 'loc_id' in session:
            current_user.current_loc_id = session.get('loc_id')

        db.session.commit()


@app.route('/staff/technicians', methods=['GET', 'POST'])
@login_required
def staff_technicians():
    if current_user.role not in ['staff', 'admin']:
        abort(403)

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

    # Redirect back to records so they can see the change
    return redirect(url_for('staff_records'))



@app.route('/logout')
def logout():
    logout_user()
    session.clear()  # Clear session completely on logout
    # After logout, return to the customer login page as the entry point.
    return redirect(url_for('login'))


if __name__ == '__main__':
    with app.app_context():
        try:
            # Add the missing plate_number column
            db.session.execute(db.text('ALTER TABLE bookings ADD COLUMN plate_number VARCHAR(50)'))
            db.session.commit()
            print("--- Bookings Table Updated ---")
        except Exception as e:
            print(f"--- Column might already exist: {e} ---")
            db.session.rollback()

        db.create_all()
    app.run(debug=True)