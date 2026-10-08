import os
import math
import re
import uuid

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash
)

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import oracledb

from db import get_db_connection


# ============================================================
# SYSTEM DOMAIN CONSTANTS & VALIDATORS
# ============================================================

EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')

VALID_CATEGORIES = {'Electrical', 'Plumbing', 'Carpentry', 'Gas / Safety', 'General Maintenance'}
VALID_PRIORITIES = {'Low', 'Medium', 'High'}
VALID_TIME_SLOTS = {'Anytime', '09:00 AM - 12:00 PM', '12:00 PM - 03:00 PM', '03:00 PM - 06:00 PM'}

def is_valid_email(email):
    return bool(email and EMAIL_REGEX.match(email.strip()))


# ============================================================
# FLASK CONFIGURATION
# ============================================================

app = Flask(__name__)

app.secret_key = 'townsphere_secret_key'

app.config['UPLOAD_FOLDER'] = os.path.join('static', 'uploads')
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg'}

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


# ============================================================
# FILE HELPERS
# ============================================================

def allowed_file(filename):
    return (
        '.' in filename
        and filename.rsplit('.', 1)[1].lower()
        in app.config['ALLOWED_EXTENSIONS']
    )


# ============================================================
# INTELLIGENT WORKER ALLOCATION
# ============================================================

# Default TownSphere location.
# Until resident/block coordinates are stored in Oracle,
# this is used as the complaint location.
DEFAULT_LATITUDE = 12.8406
DEFAULT_LONGITUDE = 80.1534

# --- INTELLIGENT WORKER ALLOCATION ---

def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Calculate simple geographical distance between two coordinates.

    For TownSphere's current prototype, this is sufficient because
    resident/staff coordinates are stored as latitude and longitude.
    """
    return math.sqrt((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2)


def find_best_worker(conn, category,
                     req_lat=DEFAULT_LATITUDE,
                     req_lng=DEFAULT_LONGITUDE):

    cursor = conn.cursor()

    try:
        print("\n============================================")
        print("[INTELLIGENT WORKER ALLOCATION]")
        print("Complaint Category :", category)
        print("Complaint Location :", req_lat, req_lng)

        # ---------------------------------------------------------
        # STEP 1: Find workers with matching skill + availability
        # ---------------------------------------------------------

        query = """
            SELECT
                STAFF_ID,
                NAME,
                SPECIALIZATION,
                NVL(WORKLOAD_COUNT, 0),
                NVL(LATITUDE, :req_lat),
                NVL(LONGITUDE, :req_lng)
            FROM STAFF
            WHERE UPPER(TRIM(SPECIALIZATION)) =
                  UPPER(TRIM(:category))
              AND UPPER(TRIM(AVAILABILITY_STATUS)) =
                  'AVAILABLE'
        """

        cursor.execute(
            query,
            {
                "category": category,
                "req_lat": req_lat,
                "req_lng": req_lng
            }
        )

        candidates = cursor.fetchall()

        print("DEBUG candidates =", candidates)
        print("DEBUG candidate count =", len(candidates))

        if not candidates:

            print("[ALLOCATION RESULT] No suitable available worker.")
            print("============================================\n")

            return None

        # ---------------------------------------------------------
        # STEP 2: Calculate distances first
        # ---------------------------------------------------------

        workers = []

        for staff_id, name, specialization, workload, lat, lng in candidates:

            distance = calculate_distance(
                req_lat,
                req_lng,
                lat,
                lng
            )

            workers.append({
                "staff_id": staff_id,
                "name": name,
                "specialization": specialization,
                "workload": workload,
                "distance": distance
            })

        # ---------------------------------------------------------
        # STEP 3: Find min/max values for normalization
        # ---------------------------------------------------------

        workloads = [
            worker["workload"]
            for worker in workers
        ]

        distances = [
            worker["distance"]
            for worker in workers
        ]

        min_workload = min(workloads)
        max_workload = max(workloads)

        min_distance = min(distances)
        max_distance = max(distances)

        # ---------------------------------------------------------
        # STEP 4: Calculate normalized intelligent score
        # ---------------------------------------------------------

        ranked_workers = []

        for worker in workers:

            normalized_workload = normalize(
                worker["workload"],
                min_workload,
                max_workload
            )

            normalized_distance = normalize(
                worker["distance"],
                min_distance,
                max_distance
            )

            # Lower score = better worker.
            #
            # Workload = 60%
            # Proximity = 40%
            #
            score = (
                normalized_workload * 0.60
                +
                normalized_distance * 0.40
            )

            print(
                f"[CANDIDATE] "
                f"ID={worker['staff_id']} | "
                f"Name={worker['name']} | "
                f"Skill={worker['specialization']} | "
                f"Workload={worker['workload']} | "
                f"Distance={worker['distance']:.6f} | "
                f"Normalized Workload={normalized_workload:.3f} | "
                f"Normalized Distance={normalized_distance:.3f} | "
                f"Score={score:.3f}"
            )

            worker["normalized_workload"] = normalized_workload
            worker["normalized_distance"] = normalized_distance
            worker["score"] = score

            ranked_workers.append(worker)

        # ---------------------------------------------------------
        # STEP 5: Rank workers
        # ---------------------------------------------------------

        ranked_workers.sort(
            key=lambda worker: (
                worker["score"],
                worker["workload"],
                worker["distance"],
                worker["staff_id"]
            )
        )

        best_worker = ranked_workers[0]

        # ---------------------------------------------------------
        # STEP 6: Display selected worker
        # ---------------------------------------------------------

        print("--------------------------------------------")

        print(
            "[SELECTED WORKER]",
            best_worker["staff_id"],
            best_worker["name"]
        )

        print(
            "[REASON] "
            "Workload:",
            best_worker["workload"],
            "| Distance:",
            f"{best_worker['distance']:.6f}",
            "| Score:",
            f"{best_worker['score']:.3f}"
        )

        print("============================================\n")

        return best_worker["staff_id"]

    except Exception as e:

        print("[ALLOCATION ERROR]", e)

        return None

    finally:

        cursor.close()
def normalize(value, minimum, maximum):
    """
    Normalize a value between 0 and 1.

    0 = best
    1 = worst

    Used so workload and distance can be compared fairly.
    """

    if maximum == minimum:
        return 0.0

    return (value - minimum) / (maximum - minimum)

# ============================================================
# NOTIFICATION HELPERS
# ============================================================

def create_notification(
    conn,
    notification_message,
    assignment_id=None,
    resident_id=None,
    staff_id=None,
    admin_id=None
):
    """
    Create a persistent notification in Oracle.

    Existing TownSphere notification values:
        NOTIFICATION_TYPE   = 'ASSIGNMENT'
        NOTIFICATION_STATUS = 'Unread'
    """

    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            INSERT INTO NOTIFICATION
            (
                ASSIGNMENT_ID,
                RESIDENT_ID,
                STAFF_ID,
                ADMIN_ID,
                NOTIFICATION_TYPE,
                NOTIFICATION_MESSAGE,
                SENT_TIME,
                NOTIFICATION_STATUS
            )
            VALUES
            (
                :1,
                :2,
                :3,
                :4,
                'ASSIGNMENT',
                :5,
                CURRENT_TIMESTAMP,
                'Unread'
            )
            """,
            [
                assignment_id,
                resident_id,
                staff_id,
                admin_id,
                notification_message
            ]
        )

    finally:
        cursor.close()


def notify_all_admins(
    conn,
    notification_message,
    assignment_id=None
):
    """
    Broadcast an alert notification to all registered administrators (FR-36, FR-43, FR-60).
    """
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT STAFF_ID
            FROM STAFF
            WHERE UPPER(TRIM(SPECIALIZATION)) = 'ADMINISTRATION'
            """
        )
        admins = cursor.fetchall()
        for (admin_staff_id,) in admins:
            create_notification(
                conn,
                notification_message,
                assignment_id=assignment_id,
                admin_id=admin_staff_id
            )
    finally:
        cursor.close()


def get_unread_notification_count(role, user_id):
    """
    Return unread notification count for the logged-in
    resident or staff member.
    """

    if role not in ('resident', 'staff') or not user_id:
        return 0

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        if role == 'resident':
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM NOTIFICATION
                WHERE RESIDENT_ID = :1
                  AND NOTIFICATION_STATUS = 'Unread'
                """,
                [user_id]
            )

        else:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM NOTIFICATION
                WHERE STAFF_ID = :1
                  AND NOTIFICATION_STATUS = 'Unread'
                """,
                [user_id]
            )

        return cursor.fetchone()[0]

    except Exception as e:

        print(
            "[NOTIFICATION COUNT ERROR]",
            e
        )

        return 0

    finally:
        cursor.close()
        conn.close()


@app.context_processor
def inject_notification_count():

    return {
        'unread_notification_count':
            get_unread_notification_count(
                session.get('role'),
                session.get('user_id')
            )
    }


# ============================================================
# HOME / DASHBOARD ROUTE
# ============================================================

@app.route('/')
def index():

    if 'user_id' not in session:

        role_selected = request.args.get('role')
        action = request.args.get('action', 'login')

        return render_template(
            'index.html',
            view='gateway',
            role_selected=role_selected,
            action=action
        )

    role = session.get('role')

    if role == 'admin':
        return redirect(url_for('admin_dashboard'))

    elif role == 'staff':
        return redirect(url_for('staff_dashboard'))

    # --------------------------------------------------------
    # RESIDENT DASHBOARD
    # --------------------------------------------------------

    conn = get_db_connection()
    cursor = conn.cursor()

    active_tab = request.args.get('tab', 'overview')

    context = {
        'active_tab': active_tab
    }

    try:

        if role == 'resident':

            uid = session['user_id']

            # Total complaints
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM COMPLAINT
                WHERE RESIDENT_ID = :1
                """,
                [uid]
            )

            context['total_count'] = cursor.fetchone()[0]

            # Active complaints
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM COMPLAINT
                WHERE RESIDENT_ID = :1
                  AND STATUS IN (
                      'Submitted',
                      'Pending Worker',
                      'Assigned',
                      'In Progress'
                  )
                """,
                [uid]
            )

            context['active_count'] = cursor.fetchone()[0]

            # Resolved complaints
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM COMPLAINT
                WHERE RESIDENT_ID = :1
                  AND STATUS = 'Completed'
                """,
                [uid]
            )

            context['resolved_count'] = cursor.fetchone()[0]

            # Complaint history
            cursor.execute(
                """
                SELECT
                    COMPLAINT_ID,
                    CATEGORY,
                    DESCRIPTION,
                    PRIORITY,
                    STATUS,
                    VISIT_TIME_SLOT,
                    IMAGE_PATH,
                    IS_EMERGENCY
                FROM COMPLAINT
                WHERE RESIDENT_ID = :1
                ORDER BY
                    IS_EMERGENCY DESC,
                    CREATED_DATE DESC
                """,
                [uid]
            )

            context['complaints'] = cursor.fetchall()
            # --------------------------------------------------------
# WORK UPDATES FOR RESIDENT COMPLAINT HISTORY
# --------------------------------------------------------

            cursor.execute(
            """
            SELECT
                W.COMPLAINT_ID,
                W.WORK_STATUS,
                W.DESCRIPTION,
                W.IMAGE_PATH,
                W.CREATED_AT,
                S.NAME
            FROM WORK_UPDATE W
            JOIN COMPLAINT C
            ON W.COMPLAINT_ID = C.COMPLAINT_ID
            LEFT JOIN STAFF S
            ON W.STAFF_ID = S.STAFF_ID
            WHERE C.RESIDENT_ID = :1
            ORDER BY W.CREATED_AT DESC
            """,
            [uid]
        )

            context['work_updates'] = cursor.fetchall()
            # Completed complaints waiting for rating
            cursor.execute(
                """
                SELECT
                    C.COMPLAINT_ID,
                    C.CATEGORY,
                    C.DESCRIPTION
                FROM COMPLAINT C
                LEFT JOIN RATING R
                    ON C.COMPLAINT_ID = R.COMPLAINT_ID
                WHERE C.RESIDENT_ID = :1
                  AND C.STATUS = 'Completed'
                  AND R.RATING_ID IS NULL
                """,
                [uid]
            )

            context['unrated_complaints'] = cursor.fetchall()

        return render_template(
            'index.html',
            view='dashboard',
            **context
        )

    finally:

        cursor.close()
        conn.close()


# ============================================================
# LOGIN
# ============================================================

@app.route('/login', methods=['POST'])
def login():

    email = request.form['email']
    password = request.form['password']
    role = request.form['role']

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        # ----------------------------------------------------
        # RESIDENT LOGIN
        # ----------------------------------------------------

        if role == 'resident':

            cursor.execute(
                """
                SELECT
                    RESIDENT_ID,
                    NAME,
                    PASSWORD
                FROM RESIDENT
                WHERE EMAIL = :1
                """,
                [email]
            )

            user = cursor.fetchone()

            if user and check_password_hash(user[2], password):

                session['user_id'] = user[0]
                session['name'] = user[1]
                session['role'] = 'resident'

                return redirect(url_for('index'))

        # ----------------------------------------------------
        # STAFF LOGIN
        # ----------------------------------------------------

        elif role == 'staff':

            cursor.execute(
                """
                SELECT
                    STAFF_ID,
                    NAME,
                    PASSWORD
                FROM STAFF
                WHERE EMAIL = :1
                """,
                [email]
            )

            user = cursor.fetchone()

            if user and check_password_hash(user[2], password):

                session['user_id'] = user[0]
                session['name'] = user[1]
                session['role'] = 'staff'

                print("\n====================================")
                print("[STAFF LOGIN]")
                print("Staff ID   :", user[0])
                print("Staff Name :", user[1])
                print("====================================")

                return redirect(url_for('staff_dashboard'))

        # ----------------------------------------------------
        # ADMIN LOGIN
        # ----------------------------------------------------

        elif role == 'admin':

            try:

                cursor.execute(
                    """
                    SELECT
                        ADMIN_ID,
                        NAME,
                        PASSWORD
                    FROM ADMIN
                    WHERE EMAIL = :1
                    """,
                    [email]
                )

                user = cursor.fetchone()

            except oracledb.DatabaseError:

                cursor.execute(
                    """
                    SELECT
                        STAFF_ID,
                        NAME,
                        PASSWORD
                    FROM STAFF
                    WHERE EMAIL = :1
                      AND UPPER(SPECIALIZATION) =
                          'ADMINISTRATION'
                    """,
                    [email]
                )

                user = cursor.fetchone()

            if user and check_password_hash(user[2], password):

                session['user_id'] = user[0]
                session['name'] = user[1]
                session['role'] = 'admin'

                return redirect(
                    url_for('admin_dashboard')
                )

    except oracledb.DatabaseError as e:

        print("Login DB Error:", e)

        flash(
            'Database connection error. Verify db.py settings.',
            'error'
        )

        return redirect(
            url_for(
                'index',
                role=role,
                action='login'
            )
        )

    finally:

        cursor.close()
        conn.close()

    flash(
        'Invalid email or password. Please try again.',
        'error'
    )

    return redirect(
        url_for(
            'index',
            role=role,
            action='login'
        )
    )


# ============================================================
# RESIDENT REGISTRATION
# ============================================================

@app.route('/register', methods=['POST'])
def register():

    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    raw_password = request.form.get('password', '')
    phone = request.form.get('phone', '').strip()
    apt = request.form.get('apartment', '').strip().upper()

    # FR-02: Server-side validation of mandatory fields
    if not name or len(name) < 2 or len(name) > 100:
        flash('Please provide a valid full name (2-100 characters).', 'error')
        return redirect(url_for('index', role='resident', action='register'))

    if not is_valid_email(email):
        flash('Please provide a valid email address.', 'error')
        return redirect(url_for('index', role='resident', action='register'))

    if not raw_password or len(raw_password) < 6:
        flash('Password must be at least 6 characters long.', 'error')
        return redirect(url_for('index', role='resident', action='register'))

    if phone and not re.match(r'^\d{10}$', phone):
        flash('Phone number must be exactly 10 digits.', 'error')
        return redirect(url_for('index', role='resident', action='register'))

    if not apt or len(apt) > 20:
        flash('Please provide a valid apartment/flat number (max 20 characters).', 'error')
        return redirect(url_for('index', role='resident', action='register'))
        # Automatically derive block and floor from apartment number
    try:
        block_letter, flat_number = apt.split('-', 1)

        if not block_letter.isalpha() or not flat_number.isdigit():
            raise ValueError

        block_name = f"Block {block_letter}"
        floor_number = int(flat_number[:-2])

        if floor_number <= 0:
            raise ValueError

    except (ValueError, IndexError):
        flash(
            'Apartment number must be in the format Block-FloorUnit, for example F-903 or A-1002.',
            'error'
        )
        return redirect(
            url_for(
                'index',
                role='resident',
                action='register'
            )
        )

    password = generate_password_hash(raw_password)
    phone_to_store = phone if phone else '0000000000'

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Check duplicate email before insert
        cursor.execute(
            """
            SELECT 1 FROM RESIDENT WHERE LOWER(EMAIL) = LOWER(:1)
            """,
            [email]
        )
        if cursor.fetchone():
            flash('Registration failed. An account with this email address already exists.', 'error')
            return redirect(url_for('index', role='resident', action='register'))

        cursor.execute(
            """
            INSERT INTO RESIDENT
            (
                NAME,
                EMAIL,
                PASSWORD,
                PHONE,
                APARTMENT_NUMBER,
                BLOCK_NAME,
                FLOOR_NUMBER
            )
            VALUES
            (
                :1,
                :2,
                :3,
                :4,
                :5,
                :6,
                :7
            )
            """,
            [
                name,
                email,
                password,
                phone_to_store,
                apt,
                block_name,
                floor_number
            ]
        )

        conn.commit()

        flash(
            'Registration successful! Please login with your credentials.',
            'success'
        )

        return redirect(
            url_for(
                'index',
                role='resident',
                action='login'
            )
        )

    except oracledb.DatabaseError as e:

        conn.rollback()

        flash(
            'Registration could not be completed. Please check your details and try again.',
            'error'
        )

        return redirect(
            url_for(
                'index',
                role='resident',
                action='register'
            )
        )

    finally:

        cursor.close()
        conn.close()


# ============================================================
# STAFF REGISTRATION
# ============================================================

@app.route('/register_staff', methods=['POST'])
def register_staff():

    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip()
    raw_password = request.form.get('password', '')
    specialization = request.form.get('specialization', '').strip()
    phone = request.form.get('phone', '').strip()

    if not name or len(name) < 2 or len(name) > 100:
        flash('Please provide a valid full name (2-100 characters).', 'error')
        return redirect(url_for('index', role='staff', action='register'))

    if not is_valid_email(email):
        flash('Please provide a valid work email address.', 'error')
        return redirect(url_for('index', role='staff', action='register'))

    if not raw_password or len(raw_password) < 6:
        flash('Password must be at least 6 characters long.', 'error')
        return redirect(url_for('index', role='staff', action='register'))

    if specialization not in VALID_CATEGORIES:
        flash('Please select a valid specialization domain.', 'error')
        return redirect(url_for('index', role='staff', action='register'))

    if phone and not re.match(r'^\d{10}$', phone):
        flash('Phone number must be exactly 10 digits.', 'error')
        return redirect(url_for('index', role='staff', action='register'))

    password = generate_password_hash(raw_password)
    phone_to_store = phone if phone else '0000000000'

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Check duplicate staff email
        cursor.execute(
            """
            SELECT 1 FROM STAFF WHERE LOWER(EMAIL) = LOWER(:1)
            """,
            [email]
        )
        if cursor.fetchone():
            flash('Registration failed. An account with this work email already exists.', 'error')
            return redirect(url_for('index', role='staff', action='register'))

        cursor.execute(
            """
            INSERT INTO STAFF
            (
                NAME,
                EMAIL,
                PASSWORD,
                PHONE,
                SPECIALIZATION,
                AVAILABILITY_STATUS,
                SKILL_LEVEL,
                WORKLOAD_COUNT
            )
            VALUES
            (
                :1,
                :2,
                :3,
                :4,
                :5,
                'AVAILABLE',
                'Intermediate',
                0
            )
            """,
            [
                name,
                email,
                password,
                phone_to_store,
                specialization
            ]
        )

        conn.commit()

        flash(
            'Staff registration successful! You can now log in.',
            'success'
        )

        return redirect(
            url_for(
                'index',
                role='staff',
                action='login'
            )
        )

    except oracledb.DatabaseError as e:

        conn.rollback()

        print(
            "Staff Register DB Error:",
            e
        )

        flash(
            'Staff registration could not be completed. Please verify details and try again.',
            'error'
        )

        return redirect(
            url_for(
                'index',
                role='staff',
                action='register'
            )
        )

    finally:

        cursor.close()
        conn.close()


# ============================================================
# ADMIN DASHBOARD
# ============================================================

@app.route('/admin_dashboard')
def admin_dashboard():

    if session.get('role') != 'admin':

        flash(
            'Unauthorized access. Admin privileges required.',
            'error'
        )

        return redirect(url_for('index'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        stats = {}

        # ============================================================
        # ADMIN UNREAD NOTIFICATION COUNT
        # ============================================================

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM NOTIFICATION
            WHERE ADMIN_ID = :1
              AND NOTIFICATION_STATUS = 'Unread'
            """,
            [session['user_id']]
        )

        admin_unread_notification_count = cursor.fetchone()[0]


        # ============================================================
        # TOTAL COMPLAINTS
        # ============================================================

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM COMPLAINT
            """
        )

        stats['total'] = cursor.fetchone()[0]


        # ============================================================
        # PENDING COMPLAINTS
        # ============================================================

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM COMPLAINT
            WHERE STATUS IN (
                'Submitted',
                'Pending Worker'
            )
            """
        )

        stats['pending'] = cursor.fetchone()[0]


        # ============================================================
        # IN-PROGRESS COMPLAINTS
        # ============================================================

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM COMPLAINT
            WHERE STATUS IN (
                'Assigned',
                'In Progress'
            )
            """
        )

        stats['in_progress'] = cursor.fetchone()[0]


        # ============================================================
        # COMPLETED COMPLAINTS
        # ============================================================

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM COMPLAINT
            WHERE STATUS = 'Completed'
            """
        )

        stats['resolved'] = cursor.fetchone()[0]


        # ============================================================
        # ALL COMPLAINTS
        # ============================================================

        cursor.execute(
            """
            SELECT
                C.COMPLAINT_ID,
                R.NAME,
                C.CATEGORY,
                C.PRIORITY,
                C.STATUS,
                S.NAME,
                C.CATEGORY,
                C.CREATED_DATE,
                C.COMPLETED_DATE
            FROM COMPLAINT C
            JOIN RESIDENT R
                ON C.RESIDENT_ID = R.RESIDENT_ID
            LEFT JOIN (
                SELECT COMPLAINT_ID, STAFF_ID, ASSIGNMENT_STATUS
                FROM ASSIGNMENT
                WHERE ASSIGNMENT_ID IN (
                    SELECT MAX(ASSIGNMENT_ID)
                    FROM ASSIGNMENT
                    GROUP BY COMPLAINT_ID
                )
            ) A
                ON C.COMPLAINT_ID = A.COMPLAINT_ID
            LEFT JOIN STAFF S
                ON A.STAFF_ID = S.STAFF_ID
            ORDER BY
                C.CREATED_DATE DESC
            """
        )

        complaints = cursor.fetchall()
        # ============================================================
        # REPEATED COMPLAINT ALERTS (FR-63 to FR-67)
        # ============================================================

        cursor.execute(
            """
            SELECT
                D.ALERT_ID,
                D.COMPLAINT_ID,
                D.MATCHED_COMPLAINT_ID,
                C.CATEGORY,
                C.DESCRIPTION,
                R.NAME,
                D.ALERT_DATE,
                D.ALERT_STATUS
            FROM DUPLICATE_COMPLAINT_ALERT D
            JOIN COMPLAINT C
                ON D.COMPLAINT_ID = C.COMPLAINT_ID
            JOIN RESIDENT R
                ON C.RESIDENT_ID = R.RESIDENT_ID
            ORDER BY
                D.ALERT_DATE DESC,
                D.ALERT_ID DESC
            """
        )

        duplicate_alerts = cursor.fetchall()
                # ============================================================
        # RECURRING COMPLAINT INFORMATION (FR-83)
        # ============================================================

        cursor.execute(
            """
            SELECT
                CATEGORY,
                COUNT(*) AS COMPLAINT_COUNT
            FROM COMPLAINT
            GROUP BY CATEGORY
            HAVING COUNT(*) > 1
            ORDER BY COMPLAINT_COUNT DESC
            """
        )

        recurring_complaints = cursor.fetchall()
        # ============================================================
        # REPORTS & ANALYTICS (FR-85)
        # ============================================================

        # Complaints by Category
        cursor.execute(
            """
            SELECT
                CATEGORY,
                COUNT(*) AS COMPLAINT_COUNT
            FROM COMPLAINT
            GROUP BY CATEGORY
            ORDER BY COMPLAINT_COUNT DESC
            """
        )

        category_report = cursor.fetchall()


        # Complaints by Status
        cursor.execute(
            """
            SELECT
                STATUS,
                COUNT(*) AS COMPLAINT_COUNT
            FROM COMPLAINT
            GROUP BY STATUS
            ORDER BY COMPLAINT_COUNT DESC
            """
        )

        status_report = cursor.fetchall()


        # Complaints by Priority
        cursor.execute(
            """
            SELECT
                NVL(PRIORITY, 'Not Set') AS PRIORITY,
                COUNT(*) AS COMPLAINT_COUNT
            FROM COMPLAINT
            GROUP BY NVL(PRIORITY, 'Not Set')
            ORDER BY COMPLAINT_COUNT DESC
            """
        )

        priority_report = cursor.fetchall()
        # ============================================================
        # AVAILABLE STAFF
        # ============================================================

        cursor.execute(
            """
            SELECT
                STAFF_ID,
                NAME,
                SPECIALIZATION,

                CASE
                    WHEN UPPER(AVAILABILITY_STATUS)
                         = 'AVAILABLE'
                    THEN 1
                    ELSE 0
                END

            FROM STAFF
            """
        )

        available_staff = cursor.fetchall()


        # ============================================================
        # STAFF ROSTER
        # ============================================================

                # ============================================================
        # STAFF ROSTER / WORKER INFORMATION (FR-69)
        # ============================================================

        cursor.execute(
            """
            SELECT
                STAFF_ID,
                NAME,
                EMAIL,
                PHONE,
                SPECIALIZATION,
                NVL(SKILL_LEVEL, 'Standard'),
                AVAILABILITY_STATUS,
                NVL(WORKLOAD_COUNT, 0),
                ACCOUNT_STATUS
            FROM STAFF
            ORDER BY STAFF_ID
            """
        )

        staff_list = cursor.fetchall()
                # UNASSIGNED COMPLAINTS (FR-74)
        cursor.execute(
            """
            SELECT
                C.COMPLAINT_ID,
                R.NAME,
                C.CATEGORY,
                C.PRIORITY,
                C.STATUS,
                C.DESCRIPTION,
                C.CREATED_DATE
            FROM COMPLAINT C
            JOIN RESIDENT R
                ON C.RESIDENT_ID = R.RESIDENT_ID
            LEFT JOIN ASSIGNMENT A
                ON C.COMPLAINT_ID = A.COMPLAINT_ID
            WHERE A.COMPLAINT_ID IS NULL
            OR A.ASSIGNMENT_ID IS NULL
            ORDER BY C.CREATED_DATE DESC
            """
        )
        unassigned_complaints = cursor.fetchall()
                # ============================================================
        # RESIDENT INFORMATION (FR-68)
        # ============================================================

        cursor.execute(
            """
            SELECT
                RESIDENT_ID,
                NAME,
                EMAIL,
                PHONE,
                APARTMENT_NUMBER,
                BLOCK_NAME,
                FLOOR_NUMBER,
                ACCOUNT_STATUS
            FROM RESIDENT
            ORDER BY RESIDENT_ID
            """
        )

        resident_list = cursor.fetchall()

        # ============================================================
        # RENDER ADMIN DASHBOARD
        # ============================================================

        return render_template(
            'admin_dashboard.html',
            
            stats=stats,

            complaints=complaints,

            available_staff=available_staff,

            staff_list=staff_list,
            resident_list=resident_list,
            unassigned_complaints=unassigned_complaints,
            admin_unread_notification_count=
                admin_unread_notification_count,
            duplicate_alerts=duplicate_alerts,
            recurring_complaints=recurring_complaints,
            category_report=category_report,
            status_report=status_report,
            priority_report=priority_report
        )


    except Exception as e:

        flash(
            f'Error loading Admin Dashboard: {str(e)}',
            'error'
        )

        return render_template(
            'admin_dashboard.html',

            stats={},

            complaints=[],

            available_staff=[],

            staff_list=[],
            resident_list=[],
            unassigned_complaints=[],
            admin_unread_notification_count=0,
            duplicate_alerts=[],
            recurring_complaints=[],
            category_report=[],
            status_report=[],
            priority_report=[]
        )


    finally:

        cursor.close()
        conn.close()
@app.route('/admin/review_duplicate_alert', methods=['POST'])
def admin_review_duplicate_alert():

    if session.get('role') != 'admin':
        flash(
            'Unauthorized access. Admin privileges required.',
            'error'
        )
        return redirect(url_for('index'))

    alert_id = request.form.get('alert_id')

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE DUPLICATE_COMPLAINT_ALERT
            SET ALERT_STATUS = 'Reviewed'
            WHERE ALERT_ID = :1
              AND ALERT_STATUS = 'Unread'
            """,
            [alert_id]
        )

        if cursor.rowcount > 0:
            conn.commit()
            flash(
                f'Duplicate alert #{alert_id} marked as reviewed.',
                'success'
            )
        else:
            conn.rollback()
            flash(
                'Alert not found or already reviewed.',
                'error'
            )

    except Exception as e:
        conn.rollback()
        flash(
            f'Failed to update duplicate alert: {str(e)}',
            'error'
        )

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_dashboard'))

# ============================================================
# CREATE ADMIN
# ============================================================

@app.route('/create_admin', methods=['POST'])
def create_admin():

    if session.get('role') != 'admin':

        flash(
            'Unauthorized action.',
            'error'
        )

        return redirect(url_for('index'))

    name = request.form.get('name', '').strip()
    username = request.form.get('username', '').strip()
    raw_password = request.form.get('password', '')

    if not name or len(name) < 2 or len(name) > 100:
        flash('Please provide a valid administrator full name (2-100 characters).', 'error')
        return redirect(url_for('admin_dashboard'))

    if not username or len(username) < 3 or len(username) > 100:
        flash('Username/email must be between 3 and 100 characters.', 'error')
        return redirect(url_for('admin_dashboard'))

    if not raw_password or len(raw_password) < 6:
        flash('Password must be at least 6 characters long.', 'error')
        return redirect(url_for('admin_dashboard'))

    password = generate_password_hash(raw_password)

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Pre-check for duplicate admin username/email
        cursor.execute(
            """
            SELECT 1 FROM STAFF WHERE LOWER(EMAIL) = LOWER(:1)
            """,
            [username]
        )
        if cursor.fetchone():
            flash('Failed to create administrator: An account with this username/email already exists.', 'error')
            return redirect(url_for('admin_dashboard'))

        cursor.execute(
            """
            INSERT INTO STAFF
            (
                NAME,
                EMAIL,
                PASSWORD,
                SPECIALIZATION,
                AVAILABILITY_STATUS,
                SKILL_LEVEL
            )
            VALUES
            (
                :1,
                :2,
                :3,
                'Administration',
                'ACTIVE',
                'Expert'
            )
            """,
            [
                name,
                username,
                password
            ]
        )

        conn.commit()

        flash(
            f'New Administrator "{name}" registered successfully.',
            'success'
        )

    except Exception as e:

        conn.rollback()

        flash(
            'Failed to create administrator. Please check your inputs.',
            'error'
        )

    finally:

        cursor.close()
        conn.close()
        

    return redirect(
        url_for('admin_dashboard')
    )

# ============================================================
# ADMIN NOTIFICATIONS
# ============================================================

@app.route('/admin_notifications')
def admin_notifications():

    if session.get('role') != 'admin':
        flash(
            'Unauthorized access. Admin privileges required.',
            'error'
        )

        return redirect(url_for('index'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            SELECT
                NOTIFICATION_ID,
                ASSIGNMENT_ID,
                NOTIFICATION_TYPE,
                NOTIFICATION_MESSAGE,
                SENT_TIME,
                NOTIFICATION_STATUS
            FROM NOTIFICATION
            WHERE ADMIN_ID = :1
            ORDER BY SENT_TIME DESC
            """,
            [session['user_id']]
        )

        notifications = cursor.fetchall()

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM NOTIFICATION
            WHERE ADMIN_ID = :1
              AND NOTIFICATION_STATUS = 'Unread'
            """,
            [session['user_id']]
        )

        admin_unread_notification_count = cursor.fetchone()[0]

    except Exception as e:

        flash(
            f'Unable to load notifications: {str(e)}',
            'error'
        )

        notifications = []
        admin_unread_notification_count = 0

    finally:

        cursor.close()
        conn.close()

    return render_template(
        'admin_notifications.html',
        notifications=notifications,
        admin_unread_notification_count=
            admin_unread_notification_count
    )

# ============================================================
# MARK ADMIN NOTIFICATION AS READ
# ============================================================

@app.route(
    '/admin_notifications/read/<int:notification_id>',
    methods=['POST']
)
def mark_admin_notification_read(notification_id):

    if session.get('role') != 'admin':
        return redirect(url_for('index'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            UPDATE NOTIFICATION
            SET NOTIFICATION_STATUS = 'Read'
            WHERE NOTIFICATION_ID = :1
              AND ADMIN_ID = :2
            """,
            [
                notification_id,
                session['user_id']
            ]
        )

        conn.commit()

    except Exception as e:

        conn.rollback()

        flash(
            f'Unable to update notification: {str(e)}',
            'error'
        )

    finally:

        cursor.close()
        conn.close()

    return redirect(url_for('admin_notifications'))


# ============================================================
# ADMIN PRIORITY MANAGEMENT (FR-24, FR-25, FR-26)
# ============================================================

@app.route('/admin/update_priority', methods=['POST'])
def admin_update_priority():

    if session.get('role') != 'admin':
        flash('Unauthorized access.', 'error')
        return redirect(url_for('index'))

    complaint_id = request.form.get('complaint_id')
    new_priority = request.form.get('priority', '').strip()

    if new_priority not in VALID_PRIORITIES:
        flash('Invalid priority level selected.', 'error')
        return redirect(url_for('admin_dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT RESIDENT_ID, CATEGORY, PRIORITY
            FROM COMPLAINT
            WHERE COMPLAINT_ID = :1
            """,
            [complaint_id]
        )
        comp = cursor.fetchone()
        if not comp:
            flash('Complaint not found.', 'error')
            return redirect(url_for('admin_dashboard'))

        resident_id, category, old_priority = comp

        if old_priority == new_priority:
            flash(f'Complaint #{complaint_id} is already set to {new_priority} priority.', 'error')
            return redirect(url_for('admin_dashboard'))

        # Update in Oracle (FR-25)
        cursor.execute(
            """
            UPDATE COMPLAINT
            SET PRIORITY = :1
            WHERE COMPLAINT_ID = :2
            """,
            [new_priority, complaint_id]
        )

        # Notify resident
        create_notification(
            conn,
            f'The priority of your complaint #{complaint_id} ({category}) was updated to {new_priority} by the administrator.',
            resident_id=resident_id
        )

        # Notify assigned worker if one is active
        cursor.execute(
            """
            SELECT ASSIGNMENT_ID, STAFF_ID
            FROM ASSIGNMENT
            WHERE COMPLAINT_ID = :1
              AND ASSIGNMENT_STATUS IN ('Accepted', 'Offered', 'Re-opened')
            ORDER BY ASSIGNMENT_ID DESC
            FETCH FIRST 1 ROW ONLY
            """,
            [complaint_id]
        )
        active_assignment = cursor.fetchone()
        if active_assignment:
            create_notification(
                conn,
                f'Priority for Complaint #{complaint_id} ({category}) was changed from {old_priority} to {new_priority} by the administrator.',
                assignment_id=active_assignment[0],
                staff_id=active_assignment[1]
            )

        conn.commit()

        flash(f'Priority for Complaint #{complaint_id} successfully updated to {new_priority}.', 'success')

    except Exception as e:
        conn.rollback()
        flash(f'Failed to update priority: {str(e)}', 'error')

    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('admin_dashboard'))

@app.route('/admin_update_worker', methods=['POST'])
def admin_update_worker():

    if session.get('role') != 'admin':
        flash(
            'Unauthorized access. Admin privileges required.',
            'error'
        )
        return redirect(url_for('index'))

    staff_id = request.form.get('staff_id', '').strip()
    skill_level = request.form.get('skill_level', '').strip()
    availability_status = request.form.get('availability_status', '').strip()

    # Validate Staff ID
    if not staff_id.isdigit():
        flash('Invalid worker ID.', 'error')
        return redirect(url_for('admin_dashboard'))

    # Validate skill level
    allowed_skills = ['Standard', 'Intermediate', 'Expert']

    if skill_level not in allowed_skills:
        flash('Invalid skill level.', 'error')
        return redirect(url_for('admin_dashboard'))

    # Validate availability
    allowed_availability = ['AVAILABLE', 'UNAVAILABLE']

    if availability_status.upper() not in allowed_availability:
        flash('Invalid availability status.', 'error')
        return redirect(url_for('admin_dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            UPDATE STAFF
            SET
                SKILL_LEVEL = :1,
                AVAILABILITY_STATUS = :2
            WHERE STAFF_ID = :3
            """,
            [
                skill_level,
                availability_status.upper(),
                int(staff_id)
            ]
        )

        if cursor.rowcount == 0:
            flash('Worker not found.', 'error')
            return redirect(url_for('admin_dashboard'))

        conn.commit()

        flash(
            'Worker skill and availability updated successfully.',
            'success'
        )

        return redirect(url_for('admin_dashboard'))

    except oracledb.DatabaseError:

        conn.rollback()

        flash(
            'Unable to update worker information.',
            'error'
        )

        return redirect(url_for('admin_dashboard'))

    finally:

        cursor.close()
        conn.close()

@app.route('/admin_update_account_status', methods=['POST'])
def admin_update_account_status():

    if session.get('role') != 'admin':
        flash(
            'Unauthorized access. Admin privileges required.',
            'error'
        )
        return redirect(url_for('index'))

    user_type = request.form.get('user_type', '').strip().lower()
    user_id = request.form.get('user_id', '').strip()
    account_status = request.form.get('account_status', '').strip().upper()

    # Validate user type
    if user_type not in ['resident', 'staff']:
        flash('Invalid user type.', 'error')
        return redirect(url_for('admin_dashboard'))

    # Validate user ID
    if not user_id.isdigit():
        flash('Invalid user ID.', 'error')
        return redirect(url_for('admin_dashboard'))

    # Validate account status
    if account_status not in ['ACTIVE', 'INACTIVE']:
        flash('Invalid account status.', 'error')
        return redirect(url_for('admin_dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        if user_type == 'resident':

            cursor.execute(
                """
                UPDATE RESIDENT
                SET ACCOUNT_STATUS = :1
                WHERE RESIDENT_ID = :2
                """,
                [account_status, int(user_id)]
            )

        else:

            cursor.execute(
                """
                UPDATE STAFF
                SET ACCOUNT_STATUS = :1
                WHERE STAFF_ID = :2
                """,
                [account_status, int(user_id)]
            )

        if cursor.rowcount == 0:
            flash('User not found.', 'error')
            return redirect(url_for('admin_dashboard'))

        conn.commit()

        flash(
            'Account status updated successfully.',
            'success'
        )

        return redirect(url_for('admin_dashboard'))

    except oracledb.DatabaseError:

        conn.rollback()

        flash(
            'Unable to update account status.',
            'error'
        )

        return redirect(url_for('admin_dashboard'))

    finally:
        cursor.close()
        conn.close()
# ============================================================
# MANUAL WORKER ASSIGNMENT
# ============================================================

@app.route('/assign_worker', methods=['POST'])
def assign_worker():

    if session.get('role') != 'admin':

        flash(
            'Unauthorized access.',
            'error'
        )

        return redirect(
            url_for('index')
        )

    complaint_id = request.form.get(
        'complaint_id'
    )

    staff_id = request.form.get(
        'staff_id'
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        # ----------------------------------------------------
        # GET COMPLAINT INFORMATION
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT
                RESIDENT_ID,
                CATEGORY
            FROM COMPLAINT
            WHERE COMPLAINT_ID = :1
            """,
            [complaint_id]
        )

        complaint_info = cursor.fetchone()

        if not complaint_info:

            raise ValueError(
                'Complaint not found.'
            )

        resident_id = complaint_info[0]
        category = complaint_info[1]

        # ----------------------------------------------------
        # HANDLE EXISTING ACTIVE ASSIGNMENT (IF REASSIGNING)
        # ----------------------------------------------------
        cursor.execute(
            """
            SELECT ASSIGNMENT_ID, STAFF_ID, ASSIGNMENT_STATUS
            FROM ASSIGNMENT
            WHERE COMPLAINT_ID = :1
              AND ASSIGNMENT_STATUS IN ('Accepted', 'Offered', 'Re-opened')
            ORDER BY ASSIGNMENT_ID DESC
            FETCH FIRST 1 ROW ONLY
            """,
            [complaint_id]
        )
        prev_assignment = cursor.fetchone()

        if prev_assignment:
            prev_assignment_id, prev_staff_id, prev_status = prev_assignment

            # If already assigned and accepted by the same worker, no-op notice
            if str(prev_staff_id) == str(staff_id) and prev_status == 'Accepted':
                flash(
                    f'Worker #{staff_id} is already actively assigned to Complaint #{complaint_id}.',
                    'error'
                )
                return redirect(url_for('admin_dashboard'))

            # Mark previous active assignment as Reassigned to maintain clean history
            cursor.execute(
                """
                UPDATE ASSIGNMENT
                SET ASSIGNMENT_STATUS = 'Reassigned',
                    DECISION_DATE = CURRENT_TIMESTAMP
                WHERE ASSIGNMENT_ID = :1
                """,
                [prev_assignment_id]
            )

            # Decrement previous worker's workload if they had accepted or re-opened it
            if prev_status in ('Accepted', 'Re-opened'):
                cursor.execute(
                    """
                    UPDATE STAFF
                    SET WORKLOAD_COUNT = GREATEST(NVL(WORKLOAD_COUNT, 1) - 1, 0)
                    WHERE STAFF_ID = :1
                    """,
                    [prev_staff_id]
                )

            # Notify previous worker about the reassignment
            create_notification(
                conn,
                f'Complaint #{complaint_id} ({category}) has been reassigned to another technician by the administrator.',
                assignment_id=prev_assignment_id,
                staff_id=prev_staff_id
            )

        # ----------------------------------------------------
        # CREATE NEW ASSIGNMENT
        # ----------------------------------------------------
        assignment_id_var = cursor.var(int)

        cursor.execute(
            """
            INSERT INTO ASSIGNMENT
            (
                COMPLAINT_ID,
                STAFF_ID,
                ASSIGNMENT_STATUS,
                ASSIGNED_DATE,
                DECISION_DATE
            )
            VALUES
            (
                :1,
                :2,
                'Accepted',
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            RETURNING ASSIGNMENT_ID INTO :3
            """,
            [
                complaint_id,
                staff_id,
                assignment_id_var
            ]
        )

        assignment_id = (
            assignment_id_var.getvalue()[0]
        )

        # ----------------------------------------------------
        # UPDATE COMPLAINT STATUS
        # ----------------------------------------------------
        cursor.execute(
            """
            UPDATE COMPLAINT
            SET STATUS = 'Assigned'
            WHERE COMPLAINT_ID = :1
            """,
            [complaint_id]
        )

        # ----------------------------------------------------
        # UPDATE WORKER WORKLOAD
        # ----------------------------------------------------
        cursor.execute(
            """
            UPDATE STAFF
            SET WORKLOAD_COUNT = NVL(WORKLOAD_COUNT, 0) + 1
            WHERE STAFF_ID = :1
            """,
            [staff_id]
        )

        # ----------------------------------------------------
        # NOTIFY NEW STAFF
        # ----------------------------------------------------
        create_notification(
            conn,
            f'Complaint #{complaint_id} ({category}) has been manually assigned to you by the administrator.',
            assignment_id=assignment_id,
            staff_id=staff_id
        )

        # ----------------------------------------------------
        # NOTIFY RESIDENT ABOUT ASSIGNMENT / REASSIGNMENT
        # ----------------------------------------------------
        create_notification(
            conn,
            f'Your complaint #{complaint_id} ({category}) has been assigned to a technician by the administrator.',
            assignment_id=assignment_id,
            resident_id=resident_id
        )

        conn.commit()

        flash(
            f'Assigned complaint #{complaint_id} to worker #{staff_id}.',
            'success'
        )

    except Exception as e:

        conn.rollback()

        flash(
            f'Manual dispatch failed: {str(e)}',
            'error'
        )

    finally:

        cursor.close()
        conn.close()

    return redirect(
        url_for('admin_dashboard')
    )


# ============================================================
# RAISE COMPLAINT
# ============================================================

@app.route('/raise_complaint', methods=['POST'])
def raise_complaint():

    if session.get('role') != 'resident':

        return redirect(
            url_for('index')
        )

    category = request.form.get('category', '').strip()
    priority = request.form.get('priority', '').strip()
    time_slot = request.form.get('time_slot', 'Anytime').strip()
    description = request.form.get('description', '').strip()

    is_emergency = (
        1
        if request.form.get('is_emergency')
        else 0
    )

    # FR-26: Category and Priority validation
    if category not in VALID_CATEGORIES:
        flash('Invalid complaint category selected. Please choose a valid category.', 'error')
        return redirect(url_for('index', tab='raise'))

    if priority not in VALID_PRIORITIES:
        flash('Invalid priority level selected. Please choose Low, Medium, or High.', 'error')
        return redirect(url_for('index', tab='raise'))

    if time_slot not in VALID_TIME_SLOTS:
        time_slot = 'Anytime'

    # FR-13: Mandatory description validation
    if not description or len(description) < 10:
        flash('Please provide a detailed description of the maintenance issue (at least 10 characters).', 'error')
        return redirect(url_for('index', tab='raise'))

    if len(description) > 1000:
        flash('Complaint description is too long (maximum 1000 characters).', 'error')
        return redirect(url_for('index', tab='raise'))

    # --------------------------------------------------------
    # FR-17: IMAGE UPLOAD & ERROR HANDLING
    # --------------------------------------------------------
    image_filename = None

    if 'image' in request.files and request.files['image'].filename:
        file = request.files['image']

        if not allowed_file(file.filename):
            flash('Invalid image format. Only PNG, JPG, and JPEG images are allowed.', 'error')
            return redirect(url_for('index', tab='raise'))

        try:
            safe_name = secure_filename(file.filename)
            unique_prefix = uuid.uuid4().hex[:8]
            image_filename = f"{unique_prefix}_{safe_name}"
            file.save(
                os.path.join(
                    app.config['UPLOAD_FOLDER'],
                    image_filename
                )
            )
        except Exception as upload_err:
            flash(f'Image upload failed: {str(upload_err)}', 'error')
            return redirect(url_for('index', tab='raise'))

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        # ----------------------------------------------------
        # INSERT COMPLAINT
        # ----------------------------------------------------

        out_id = cursor.var(int)

        cursor.execute(
            """
            INSERT INTO COMPLAINT
            (
                RESIDENT_ID,
                CATEGORY,
                DESCRIPTION,
                PRIORITY,
                VISIT_TIME_SLOT,
                IS_EMERGENCY,
                IMAGE_PATH,
                STATUS
            )
            VALUES
            (
                :1,
                :2,
                :3,
                :4,
                :5,
                :6,
                :7,
                'Pending Worker'
            )
            RETURNING COMPLAINT_ID INTO :8
            """,
            [
                session['user_id'],
                category,
                description,
                priority,
                time_slot,
                is_emergency,
                image_filename,
                out_id
            ]
        )

        complaint_id = out_id.getvalue()[0]
                # ----------------------------------------------------
        # REPEATED COMPLAINT DETECTION (FR-63 to FR-67)
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT COMPLAINT_ID
            FROM COMPLAINT
            WHERE COMPLAINT_ID <> :1
              AND UPPER(TRIM(CATEGORY)) = UPPER(TRIM(:2))
              AND UPPER(TRIM(DESCRIPTION)) = UPPER(TRIM(:3))
            ORDER BY CREATED_DATE DESC
            FETCH FIRST 1 ROW ONLY
            """,
            [
                complaint_id,
                category,
                description
            ]
        )

        matched_complaint = cursor.fetchone()

        if matched_complaint:

            matched_complaint_id = matched_complaint[0]

            # Save the suspected duplicate alert
            cursor.execute(
                """
                INSERT INTO DUPLICATE_COMPLAINT_ALERT
                (
                    COMPLAINT_ID,
                    MATCHED_COMPLAINT_ID,
                    ALERT_DATE,
                    ALERT_STATUS
                )
                VALUES
                (
                    :1,
                    :2,
                    CURRENT_TIMESTAMP,
                    'Unread'
                )
                """,
                [
                    complaint_id,
                    matched_complaint_id
                ]
            )

            # Notify all administrators
            notify_all_admins(
                conn,
                f'Possible duplicate complaint detected: '
                f'Complaint #{complaint_id} matches earlier '
                f'Complaint #{matched_complaint_id}. '
                f'Category: {category}. Please review.'
            )

        # ----------------------------------------------------
        # RESIDENT LOCATION
        # ----------------------------------------------------
        #
        # Currently using the TownSphere default location.
        #
        # Later we will connect this to:
        #
        # Apartment / Block -> Latitude / Longitude
        #
        # so that proximity becomes genuinely location-based.
        # ----------------------------------------------------

        complaint_lat = DEFAULT_LATITUDE
        complaint_lng = DEFAULT_LONGITUDE

        # ----------------------------------------------------
        # INTELLIGENT WORKER ALLOCATION
        # ----------------------------------------------------

        assigned_staff_id = find_best_worker(
            conn,
            category,
            complaint_lat,
            complaint_lng
        )

        # ----------------------------------------------------
        # CREATE ASSIGNMENT
        # ----------------------------------------------------

        if assigned_staff_id:

    # --------------------------------------------------------
    # CREATE OFFERED ASSIGNMENT
    # --------------------------------------------------------

            assignment_id_var = cursor.var(int)

            cursor.execute(
                """
                INSERT INTO ASSIGNMENT
                (
                    COMPLAINT_ID,
                    STAFF_ID,
                    ASSIGNMENT_STATUS
                )
                VALUES
                (
                    :1,
                    :2,
                    'Offered'
                )
                RETURNING ASSIGNMENT_ID INTO :3
                """,
                [
                    complaint_id,
                    assigned_staff_id,
                    assignment_id_var
                ]
            )

            assignment_id = (
                assignment_id_var.getvalue()[0]
            )

            # NOTIFY SELECTED WORKER (FR-34)
            create_notification(
                conn,
                f'New {category} complaint #{complaint_id} has been assigned to you. Please review and respond.',
                assignment_id=assignment_id,
                staff_id=assigned_staff_id
            )

            # NOTIFY RESIDENT (FR-58)
            create_notification(
                conn,
                f'Your maintenance complaint #{complaint_id} ({category}, Priority: {priority}) has been submitted successfully and offered to a technician.',
                assignment_id=assignment_id,
                resident_id=session['user_id']
            )

            # IF EMERGENCY SOS, NOTIFY ADMINS (FR-60)
            if is_emergency:
                notify_all_admins(
                    conn,
                    f'🚨 EMERGENCY SOS: New urgent complaint #{complaint_id} ({category}) submitted by Resident #{session["user_id"]}. Immediate attention required.',
                    assignment_id=assignment_id
                )

            conn.commit()

            flash(
                f'Complaint #{complaint_id} submitted! '
                f'Automatically offered to the best available '
                f'worker based on skill, workload and proximity.',
                'success'
            )

        else:

            # NOTIFY RESIDENT OF QUEUED COMPLAINT (FR-58)
            create_notification(
                conn,
                f'Your maintenance complaint #{complaint_id} ({category}, Priority: {priority}) has been submitted and is currently queued for technician allocation.',
                resident_id=session['user_id']
            )

            # NOTIFY ADMINISTRATORS ABOUT UNASSIGNED COMPLAINT (FR-36, FR-60)
            notify_all_admins(
                conn,
                f'⚠️ Unassigned Alert: Complaint #{complaint_id} ({category}, Priority: {priority}) has no available worker and requires manual dispatch.'
            )

            if is_emergency:
                notify_all_admins(
                    conn,
                    f'🚨 EMERGENCY SOS: Unassigned emergency complaint #{complaint_id} ({category}) submitted by Resident #{session["user_id"]}!'
                )

            conn.commit()

            flash(
                f'Complaint #{complaint_id} submitted! '
                f'No suitable worker is currently available. '
                f'Worker allocation queued.',
                'success'
            )

    except Exception as e:

            conn.rollback()

            flash(
                f'Failed to submit complaint: {str(e)}',
                'error'
            )

    finally:

        cursor.close()
        conn.close()

    return redirect(
        url_for(
            'index',
            tab='history'
        )
    )


# ============================================================
# WORKER ACCEPT / REJECT
# ============================================================
@app.route('/worker_respond', methods=['POST'])
def worker_respond():

    if session.get('role') != 'staff':
        flash('Unauthorized access.', 'error')
        return redirect(url_for('index'))

    assignment_id = request.form.get('assignment_id')
    complaint_id = request.form.get('complaint_id')
    action = request.form.get('action')
    staff_id = session.get('user_id')

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        # ====================================================
        # ACCEPT
        # ====================================================

        if action == 'accept':

            cursor.execute(
                """
                UPDATE ASSIGNMENT
                SET
                    ASSIGNMENT_STATUS = 'Accepted',
                    DECISION_DATE = CURRENT_TIMESTAMP
                WHERE ASSIGNMENT_ID = :1
                  AND STAFF_ID = :2
                  AND ASSIGNMENT_STATUS = 'Offered'
                """,
                [assignment_id, staff_id]
            )

            if cursor.rowcount == 0:
                raise ValueError(
                    'Assignment not found or no longer available.'
                )

            cursor.execute(
                """
                UPDATE COMPLAINT
                SET STATUS = 'Assigned'
                WHERE COMPLAINT_ID = :1
                """,
                [complaint_id]
            )

            cursor.execute(
                """
                UPDATE STAFF
                SET WORKLOAD_COUNT =
                    NVL(WORKLOAD_COUNT, 0) + 1
                WHERE STAFF_ID = :1
                """,
                [staff_id]
            )

            conn.commit()

            # ------------------------------------------------
            # NOTIFY RESIDENT
            # ------------------------------------------------

            cursor.execute(
                """
                SELECT RESIDENT_ID
                FROM COMPLAINT
                WHERE COMPLAINT_ID = :1
                """,
                [complaint_id]
            )

            resident_row = cursor.fetchone()

            if resident_row:
                create_notification(
                    conn,
                    f'Your complaint #{complaint_id} '
                    f'has been accepted by the assigned worker.',
                    assignment_id=assignment_id,
                    resident_id=resident_row[0]
                )

                conn.commit()

            flash(
                'Task accepted! It is now in your Active Assigned Tasks.',
                'success'
            )


        # ====================================================
        # REJECT
        # ====================================================

        elif action == 'reject':

            cursor.execute(
                """
                UPDATE ASSIGNMENT
                SET
                    ASSIGNMENT_STATUS = 'Rejected',
                    DECISION_DATE = CURRENT_TIMESTAMP
                WHERE ASSIGNMENT_ID = :1
                  AND STAFF_ID = :2
                  AND ASSIGNMENT_STATUS = 'Offered'
                """,
                [assignment_id, staff_id]
            )

            if cursor.rowcount == 0:
                raise ValueError(
                    'Assignment not found or no longer available.'
                )

            # ------------------------------------------------
            # GET COMPLAINT CATEGORY
            # ------------------------------------------------

            cursor.execute(
                """
                SELECT CATEGORY
                FROM COMPLAINT
                WHERE COMPLAINT_ID = :1
                """,
                [complaint_id]
            )

            category_res = cursor.fetchone()

            category = (
                category_res[0]
                if category_res
                else None
            )

            # ------------------------------------------------
            # TRY NEXT SUITABLE WORKER
            # ------------------------------------------------

            next_worker_id = find_best_worker(
                conn,
                category
            )

            # ------------------------------------------------
            # DON'T SEND BACK TO SAME WORKER
            # ------------------------------------------------

            if next_worker_id == staff_id:

                cursor.execute(
                    """
                    SELECT STAFF_ID
                    FROM STAFF
                    WHERE LOWER(TRIM(SPECIALIZATION)) =
                          LOWER(TRIM(:1))
                      AND UPPER(TRIM(AVAILABILITY_STATUS)) =
                          'AVAILABLE'
                      AND STAFF_ID != :2
                      AND STAFF_ID NOT IN
                          (
                              SELECT STAFF_ID
                              FROM ASSIGNMENT
                              WHERE COMPLAINT_ID = :3
                          )
                    ORDER BY
                        NVL(WORKLOAD_COUNT, 0),
                        STAFF_ID
                    """,
                    [
                        category,
                        staff_id,
                        complaint_id
                    ]
                )

                fallback = cursor.fetchone()

                next_worker_id = (
                    fallback[0]
                    if fallback
                    else None
                )


            # =================================================
            # ALTERNATE WORKER FOUND
            # =================================================

            if next_worker_id:

                # ------------------------------------------------
                # CREATE NEW OFFERED ASSIGNMENT
                # ------------------------------------------------

                next_assignment_id_var = cursor.var(int)

                cursor.execute(
                    """
                    INSERT INTO ASSIGNMENT
                    (
                        COMPLAINT_ID,
                        STAFF_ID,
                        ASSIGNMENT_STATUS
                    )
                    VALUES
                    (
                        :1,
                        :2,
                        'Offered'
                    )
                    RETURNING ASSIGNMENT_ID INTO :3
                    """,
                    [
                        complaint_id,
                        next_worker_id,
                        next_assignment_id_var
                    ]
                )

                next_assignment_id = (
                    next_assignment_id_var.getvalue()[0]
                )

                # ------------------------------------------------
                # NOTIFY NEW WORKER
                # ------------------------------------------------

                create_notification(
                    conn,
                    f'Complaint #{complaint_id} '
                    f'has been reassigned to you after the '
                    f'previous worker declined it.',
                    assignment_id=next_assignment_id,
                    staff_id=next_worker_id
                )

                # ------------------------------------------------
                # NOTIFY RESIDENT
                # ------------------------------------------------

                cursor.execute(
                    """
                    SELECT RESIDENT_ID
                    FROM COMPLAINT
                    WHERE COMPLAINT_ID = :1
                    """,
                    [complaint_id]
                )

                resident_row = cursor.fetchone()

                if resident_row:

                    create_notification(
                        conn,
                        f'Your complaint #{complaint_id} '
                        f'was declined by the assigned worker '
                        f'and is being reassigned.',
                        assignment_id=assignment_id,
                        resident_id=resident_row[0]
                    )

                # ------------------------------------------------
                # NOTIFY ADMIN
                # ------------------------------------------------

                notify_all_admins(
                    conn,
                    f'Worker Decline: Staff #{staff_id} '
                    f'({session.get("name", "Worker")}) declined '
                    f'Complaint #{complaint_id} '
                    f'({category or "General"}).',
                    assignment_id=assignment_id
                )

                # ------------------------------------------------
                # IMPORTANT: COMMIT REASSIGNMENT
                # ------------------------------------------------

                conn.commit()

                flash(
                    'Task declined. Complaint automatically '
                    'reassigned to another worker.',
                    'error'
                )


            # =================================================
            # NO ALTERNATE WORKER AVAILABLE
            # =================================================

            else:

                cursor.execute(
                    """
                    UPDATE COMPLAINT
                    SET STATUS = 'Pending Worker'
                    WHERE COMPLAINT_ID = :1
                    """,
                    [complaint_id]
                )

                # ------------------------------------------------
                # NOTIFY ADMIN
                # ------------------------------------------------

                notify_all_admins(
                    conn,
                    f'⚠️ Reassignment Alert: Complaint '
                    f'#{complaint_id} ({category or "General"}) '
                    f'declined by Staff #{staff_id} and no '
                    f'alternate worker is available. '
                    f'Manual assignment required.'
                )

                # ------------------------------------------------
                # NOTIFY RESIDENT
                # ------------------------------------------------

                cursor.execute(
                    """
                    SELECT RESIDENT_ID
                    FROM COMPLAINT
                    WHERE COMPLAINT_ID = :1
                    """,
                    [complaint_id]
                )

                resident_row = cursor.fetchone()

                if resident_row:

                    create_notification(
                        conn,
                        f'Your complaint #{complaint_id} '
                        f'was declined and no suitable worker '
                        f'is currently available. It remains '
                        f'pending for reassignment.',
                        assignment_id=assignment_id,
                        resident_id=resident_row[0]
                    )

                conn.commit()

                flash(
                    'Task declined. No alternate worker was '
                    'available, so the complaint is pending '
                    'manual reassignment.',
                    'error'
                )


        # ====================================================
        # INVALID ACTION
        # ====================================================

        else:

            raise ValueError('Invalid worker response action.')


    except Exception as e:

        conn.rollback()

        flash(
            f'An error occurred while processing task response: '
            f'{str(e)}',
            'error'
        )


    finally:

        cursor.close()
        conn.close()


    return redirect(
        url_for('staff_dashboard')
    )   

# ============================================================
# RESIDENT REJECTS COMPLETION
# ============================================================

@app.route('/reject_resolution', methods=['POST'])
def reject_resolution():

    if session.get('role') != 'resident':

        return redirect(
            url_for('index')
        )

    complaint_id = request.form[
        'complaint_id'
    ]

    reason = request.form[
        'reason'
    ]

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Verify that complaint exists and belongs to the logged-in resident (FR-18)
        cursor.execute(
            """
            SELECT RESIDENT_ID
            FROM COMPLAINT
            WHERE COMPLAINT_ID = :1
            """,
            [complaint_id]
        )
        comp_row = cursor.fetchone()

        if not comp_row or comp_row[0] != session.get('user_id'):
            flash(
                'Unauthorized access. You can only update your own complaints.',
                'error'
            )
            return redirect(
                url_for('index', tab='history')
            )

        cursor.execute(
            """
            UPDATE COMPLAINT
            SET
                STATUS = 'In Progress',
                REJECTION_REASON = :1
            WHERE COMPLAINT_ID = :2
            """,
            [
                reason,
                complaint_id
            ]
        )

        # Update only the latest assignment row to preserve historical status of older assignments (FR-79)
        cursor.execute(
            """
            UPDATE ASSIGNMENT
            SET ASSIGNMENT_STATUS = 'Re-opened'
            WHERE ASSIGNMENT_ID = (
                SELECT MAX(ASSIGNMENT_ID)
                FROM ASSIGNMENT
                WHERE COMPLAINT_ID = :1
            )
            """,
            [complaint_id]
        )
        # --------------------------------------------------------
# NOTIFY WORKER
# --------------------------------------------------------

        cursor.execute(
            """
            SELECT
                ASSIGNMENT_ID,
                STAFF_ID
            FROM ASSIGNMENT
            WHERE COMPLAINT_ID = :1
            AND ASSIGNMENT_STATUS = 'Re-opened'
            ORDER BY ASSIGNMENT_ID DESC
            FETCH FIRST 1 ROW ONLY
            """,
            [complaint_id]
        )

        reopened_assignment = cursor.fetchone()

        if reopened_assignment:

            create_notification(
                conn,

                f'Complaint #{complaint_id} has been reopened '
                f'after the resident rejected the resolution. '
                f'Please review it again.',

                assignment_id=reopened_assignment[0],
                staff_id=reopened_assignment[1]
            )

            notify_all_admins(
                conn,
                f'Resident rejected resolution for Complaint #{complaint_id}. Reason: {reason[:100]}',
                assignment_id=reopened_assignment[0]
            )
        else:
            notify_all_admins(
                conn,
                f'Resident rejected resolution for Complaint #{complaint_id}. Reason: {reason[:100]}'
            )

        conn.commit()

        flash(
            'Resolution rejected. Task marked back to '
            'In Progress for staff review.',
            'error'
        )

    except Exception as e:

        conn.rollback()

        flash(
            f'Error rejecting resolution: {str(e)}',
            'error'
        )

    finally:

        cursor.close()
        conn.close()

    return redirect(
        url_for(
            'index',
            tab='history'
        )
    )


# ============================================================
# STAFF DASHBOARD
# ============================================================

@app.route('/staff_dashboard')
def staff_dashboard():

    if session.get('role') != 'staff':

        flash(
            'Unauthorized access. Please log in as a staff member.',
            'error'
        )

        return redirect(
            url_for('index')
        )

    staff_id = session.get(
        'user_id'
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        # ----------------------------------------------------
        # STAFF INFORMATION
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT
                STAFF_ID,
                NAME,
                SPECIALIZATION,
                AVAILABILITY_STATUS,
                NVL(WORKLOAD_COUNT, 0)
            FROM STAFF
            WHERE STAFF_ID = :1
            """,
            [staff_id]
        )

        staff_info = cursor.fetchone()

        print("\n============================================")
        print("[STAFF DASHBOARD]")
        print("Session staff_id :", staff_id)
        print("Session name     :", session.get('name'))
        print("Session role     :", session.get('role'))
        print("============================================")

        print(
            "[STAFF INFO]",
            staff_info
        )

        # ----------------------------------------------------
        # OFFERED TASKS
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT
                A.ASSIGNMENT_ID,
                C.COMPLAINT_ID,
                C.CATEGORY,
                C.DESCRIPTION,
                C.PRIORITY,
                C.VISIT_TIME_SLOT,
                NVL(C.IS_EMERGENCY, 0),
                C.IMAGE_PATH,
                R.APARTMENT_NUMBER
            FROM ASSIGNMENT A

            JOIN COMPLAINT C
                ON A.COMPLAINT_ID =
                   C.COMPLAINT_ID

            JOIN RESIDENT R
                ON C.RESIDENT_ID =
                   R.RESIDENT_ID

            WHERE A.STAFF_ID = :1
              AND A.ASSIGNMENT_STATUS =
                  'Offered'

            ORDER BY
                NVL(C.IS_EMERGENCY, 0) DESC,
                C.CREATED_DATE ASC
            """,
            [staff_id]
        )

        offered_tasks = cursor.fetchall()

        print(
            "[OFFERED TASKS]",
            offered_tasks
        )

        print(
            "[OFFERED COUNT]",
            len(offered_tasks)
        )

        # ----------------------------------------------------
        # ACTIVE TASKS
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT
                C.COMPLAINT_ID,
                C.CATEGORY,
                C.DESCRIPTION,
                C.PRIORITY,
                C.STATUS,
                C.VISIT_TIME_SLOT,
                NVL(C.IS_EMERGENCY, 0),
                R.APARTMENT_NUMBER,
                R.PHONE,
                A.ASSIGNMENT_STATUS

            FROM ASSIGNMENT A

            JOIN COMPLAINT C
                ON A.COMPLAINT_ID =
                   C.COMPLAINT_ID

            JOIN RESIDENT R
                ON C.RESIDENT_ID =
                   R.RESIDENT_ID

            WHERE A.STAFF_ID = :1
              AND A.ASSIGNMENT_STATUS IN
                  (
                      'Accepted',
                      'Re-opened'
                  )

              AND C.STATUS IN
                  (
                      'Assigned',
                      'In Progress'
                  )

            ORDER BY
                NVL(C.IS_EMERGENCY, 0) DESC,
                C.CREATED_DATE ASC
            """,
            [staff_id]
        )

        active_tasks = cursor.fetchall()

        print(
            "[ACTIVE TASKS]",
            active_tasks
        )

        print(
            "[ACTIVE COUNT]",
            len(active_tasks)
        )

        print(
            "============================================\n"
        )

        return render_template(
            'staff_dashboard.html',
            offered_tasks=offered_tasks,
            active_tasks=active_tasks
        )

    except Exception as e:

        print(
            "[STAFF DASHBOARD ERROR]",
            e
        )

        flash(
            f'Error retrieving task dashboard: {str(e)}',
            'error'
        )

        return render_template(
            'staff_dashboard.html',
            offered_tasks=[],
            active_tasks=[]
        )

    finally:

        cursor.close()
        conn.close()


# ============================================================
# UPDATE TASK STATUS
# ============================================================
@app.route('/update_task_status', methods=['POST'])
def update_task_status():

    if session.get('role') != 'staff':
        flash('Unauthorized access.', 'error')
        return redirect(url_for('index'))

    complaint_id = request.form.get('complaint_id')
    new_status = request.form.get('status')
    description = request.form.get('description', '').strip()
    image = request.files.get('work_image')
    staff_id = session.get('user_id')

    # Allow only the statuses used by the staff dashboard
    if new_status not in ['In Progress', 'Completed']:
        flash('Invalid task status.', 'error')
        return redirect(url_for('staff_dashboard'))

    # Require an image when completing the task
    if new_status == 'Completed':
        if not image or image.filename == '':
            flash('Please upload a completion image.', 'error')
            return redirect(url_for('staff_dashboard'))

    image_path = None

    # ----------------------------------------------------
    # SAVE WORK IMAGE
    # ----------------------------------------------------

    if image and image.filename:

        allowed_extensions = {
            'png', 'jpg', 'jpeg', 'webp'
        }

        filename = secure_filename(image.filename)

        if (
            '.' not in filename
            or filename.rsplit('.', 1)[1].lower()
            not in allowed_extensions
        ):
            flash(
                'Invalid image format. Use PNG, JPG, JPEG, or WEBP.',
                'error'
            )
            return redirect(url_for('staff_dashboard'))

        upload_folder = os.path.join(
            app.root_path,
            'static',
            'uploads',
            'work_updates'
        )

        os.makedirs(upload_folder, exist_ok=True)

        # Avoid overwriting files with the same name
        import uuid
        unique_filename = (
            f"{uuid.uuid4().hex}_"
            f"{filename}"
        )

        image.save(
            os.path.join(upload_folder, unique_filename)
        )

        # Path relative to the static folder
        image_path = (
            f"uploads/work_updates/{unique_filename}"
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        # ----------------------------------------------------
        # UPDATE COMPLAINT STATUS
        # ----------------------------------------------------

        cursor.execute(
            """
            UPDATE COMPLAINT
            SET STATUS = :1
            WHERE COMPLAINT_ID = :2
            """,
            [
                new_status,
                complaint_id
            ]
        )

        # ----------------------------------------------------
        # UPDATE ASSIGNMENT STATUS
        # ----------------------------------------------------

        cursor.execute(
            """
            UPDATE ASSIGNMENT
            SET ASSIGNMENT_STATUS = 'Accepted'
            WHERE COMPLAINT_ID = :1
              AND STAFF_ID = :2
            """,
            [
                complaint_id,
                staff_id
            ]
        )

        # ----------------------------------------------------
        # SAVE WORK UPDATE
        # ----------------------------------------------------

        cursor.execute(
            """
            INSERT INTO WORK_UPDATE (
                COMPLAINT_ID,
                STAFF_ID,
                WORK_STATUS,
                DESCRIPTION,
                IMAGE_PATH
            )
            VALUES (:1, :2, :3, :4, :5)
            """,
            [
                complaint_id,
                staff_id,
                new_status,
                description or None,
                image_path
            ]
        )

        # ----------------------------------------------------
        # REDUCE WORKLOAD WHEN COMPLETED
        # ----------------------------------------------------

        if new_status == 'Completed':

            cursor.execute(
                """
                UPDATE STAFF
                SET WORKLOAD_COUNT =
                    GREATEST(
                        NVL(WORKLOAD_COUNT, 1) - 1,
                        0
                    )
                WHERE STAFF_ID = :1
                """,
                [staff_id]
            )

        # ----------------------------------------------------
        # GET RESIDENT
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT RESIDENT_ID
            FROM COMPLAINT
            WHERE COMPLAINT_ID = :1
            """,
            [complaint_id]
        )

        resident_row = cursor.fetchone()

        # ----------------------------------------------------
        # GET CURRENT ASSIGNMENT
        # ----------------------------------------------------

        cursor.execute(
            """
            SELECT ASSIGNMENT_ID
            FROM ASSIGNMENT
            WHERE COMPLAINT_ID = :1
              AND STAFF_ID = :2
            ORDER BY ASSIGNMENT_ID DESC
            FETCH FIRST 1 ROW ONLY
            """,
            [
                complaint_id,
                staff_id
            ]
        )

        assignment_row = cursor.fetchone()

        # ----------------------------------------------------
        # NOTIFY RESIDENT
        # ----------------------------------------------------

        if resident_row:

            if new_status == 'Completed':
                notification_message = (
                    f'Your complaint #{complaint_id} '
                    f'has been marked Completed.'
                )
            else:
                notification_message = (
                    f'Your complaint #{complaint_id} '
                    f'is now {new_status}.'
                )

            create_notification(
                conn,
                notification_message,
                assignment_id=(
                    assignment_row[0]
                    if assignment_row
                    else None
                ),
                resident_id=resident_row[0]
            )

        conn.commit()

        flash(
            f'Complaint #{complaint_id} updated to {new_status}.',
            'success'
        )

    except Exception as e:

        conn.rollback()

        flash(
            f'Failed to update status: {str(e)}',
            'error'
        )

    finally:

        cursor.close()
        conn.close()

    return redirect(url_for('staff_dashboard'))

# ============================================================
# SUBMIT RATING
# ============================================================

@app.route('/submit_rating', methods=['POST'])
def submit_rating():

    if (
        'user_id' not in session
        or session.get('role') != 'resident'
    ):

        flash(
            'Unauthorized access.',
            'error'
        )

        return redirect(
            url_for('index')
        )

    complaint_id = request.form.get(
        'complaint_id'
    )

    rating_score = request.form.get(
        'rating_score'
    )

    feedback_text = request.form.get(
        'feedback'
    )

    if (
        not complaint_id
        or not rating_score
        or not feedback_text
    ):

        flash(
            'Missing required feedback fields.',
            'error'
        )

        return redirect(
            url_for(
                'index',
                tab='history'
            )
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        complaint_id_int = int(
            complaint_id
        )

        rating_score_int = int(
            rating_score
        )

        # Verify that complaint exists and belongs to the logged-in resident (FR-18)
        cursor.execute(
            """
            SELECT RESIDENT_ID
            FROM COMPLAINT
            WHERE COMPLAINT_ID = :1
            """,
            [complaint_id_int]
        )
        comp_row = cursor.fetchone()

        if not comp_row or comp_row[0] != session.get('user_id'):
            flash(
                'Unauthorized access. You can only rate your own complaints.',
                'error'
            )
            return redirect(
                url_for('index', tab='history')
            )

        cursor.execute(
            """
            INSERT INTO RATING
            (
                COMPLAINT_ID,
                RATING_SCORE,
                FEEDBACK,
                CREATED_AT
            )
            VALUES
            (
                :1,
                :2,
                :3,
                SYSDATE
            )
            """,
            [
                complaint_id_int,
                rating_score_int,
                feedback_text
            ]
        )

        cursor.execute(
            """
            UPDATE COMPLAINT
            SET STATUS = 'Completed'
            WHERE COMPLAINT_ID = :1
            """,
            [complaint_id_int]
        )
        # --------------------------------------------------------
# NOTIFY ASSIGNED WORKER
# --------------------------------------------------------

        cursor.execute(
            """
            SELECT
                ASSIGNMENT_ID,
                STAFF_ID
            FROM ASSIGNMENT
            WHERE COMPLAINT_ID = :1
                AND ASSIGNMENT_STATUS IN
                    ('Accepted', 'Re-opened')
            ORDER BY ASSIGNMENT_ID DESC
            FETCH FIRST 1 ROW ONLY
            """,
            [complaint_id_int]
        )

        assignment_row = cursor.fetchone()

        if assignment_row:

            create_notification(
                conn,

                f'Resident has submitted a rating '
                f'and feedback for complaint '
                f'#{complaint_id_int}. '
                f'The complaint is completed.',

                assignment_id=assignment_row[0],
                staff_id=assignment_row[1]
            )
        conn.commit()

        flash(
            'Thank you! Your feedback has been recorded.',
            'success'
        )

    except Exception as e:

        conn.rollback()

        print(
            "Rating Submit Error:",
            e
        )

        flash(
            'Failed to record rating. Please try again.',
            'error'
        )

    finally:

        cursor.close()
        conn.close()

    return redirect(
        url_for(
            'index',
            tab='history'
        )
    )

# ============================================================
# NOTIFICATIONS
# ============================================================

@app.route('/notifications')
def notifications():

    role = session.get('role')
    user_id = session.get('user_id')

    if role not in ('resident', 'staff') or not user_id:

        flash(
            'Please log in to view notifications.',
            'error'
        )

        return redirect(
            url_for('index')
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        if role == 'resident':

            cursor.execute(
                """
                SELECT
                    NOTIFICATION_ID,
                    ASSIGNMENT_ID,
                    NOTIFICATION_TYPE,
                    NOTIFICATION_MESSAGE,
                    SENT_TIME,
                    NOTIFICATION_STATUS
                FROM NOTIFICATION
                WHERE RESIDENT_ID = :1
                ORDER BY
                    SENT_TIME DESC NULLS LAST,
                    NOTIFICATION_ID DESC
                """,
                [user_id]
            )

        else:

            cursor.execute(
                """
                SELECT
                    NOTIFICATION_ID,
                    ASSIGNMENT_ID,
                    NOTIFICATION_TYPE,
                    NOTIFICATION_MESSAGE,
                    SENT_TIME,
                    NOTIFICATION_STATUS
                FROM NOTIFICATION
                WHERE STAFF_ID = :1
                ORDER BY
                    SENT_TIME DESC NULLS LAST,
                    NOTIFICATION_ID DESC
                """,
                [user_id]
            )

        notification_list = cursor.fetchall()

        return render_template(
            'notifications.html',
            notifications=notification_list,
            role=role
        )

    except Exception as e:

        print(
            '[NOTIFICATIONS ERROR]',
            e
        )

        flash(
            f'Error loading notifications: {str(e)}',
            'error'
        )

        return redirect(
            url_for('index')
        )

    finally:

        cursor.close()
        conn.close()


# ============================================================
# MARK ONE NOTIFICATION AS READ
# ============================================================

@app.route(
    '/notifications/mark-read/<int:notification_id>',
    methods=['POST']
)
def mark_notification_read(notification_id):

    role = session.get('role')
    user_id = session.get('user_id')

    if role not in ('resident', 'staff') or not user_id:

        flash(
            'Unauthorized access.',
            'error'
        )

        return redirect(
            url_for('index')
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        if role == 'resident':

            cursor.execute(
                """
                UPDATE NOTIFICATION
                SET NOTIFICATION_STATUS = 'Read'
                WHERE NOTIFICATION_ID = :1
                  AND RESIDENT_ID = :2
                """,
                [
                    notification_id,
                    user_id
                ]
            )

        else:

            cursor.execute(
                """
                UPDATE NOTIFICATION
                SET NOTIFICATION_STATUS = 'Read'
                WHERE NOTIFICATION_ID = :1
                  AND STAFF_ID = :2
                """,
                [
                    notification_id,
                    user_id
                ]
            )

        conn.commit()

    except Exception as e:

        conn.rollback()

        flash(
            f'Failed to mark notification as read: {str(e)}',
            'error'
        )

    finally:

        cursor.close()
        conn.close()

    return redirect(
        url_for('notifications')
    )


# ============================================================
# MARK ALL NOTIFICATIONS AS READ
# ============================================================

@app.route(
    '/notifications/mark-all-read',
    methods=['POST']
)
def mark_all_notifications_read():

    role = session.get('role')
    user_id = session.get('user_id')

    if role not in ('resident', 'staff') or not user_id:

        flash(
            'Unauthorized access.',
            'error'
        )

        return redirect(
            url_for('index')
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        if role == 'resident':

            cursor.execute(
                """
                UPDATE NOTIFICATION
                SET NOTIFICATION_STATUS = 'Read'
                WHERE RESIDENT_ID = :1
                  AND NOTIFICATION_STATUS = 'Unread'
                """,
                [user_id]
            )

        else:

            cursor.execute(
                """
                UPDATE NOTIFICATION
                SET NOTIFICATION_STATUS = 'Read'
                WHERE STAFF_ID = :1
                  AND NOTIFICATION_STATUS = 'Unread'
                """,
                [user_id]
            )

        conn.commit()

        flash(
            'All notifications marked as read.',
            'success'
        )

    except Exception as e:

        conn.rollback()

        flash(
            f'Failed to mark notifications as read: {str(e)}',
            'error'
        )

    finally:

        cursor.close()
        conn.close()

    return redirect(
        url_for('notifications')
    )
# ============================================================
# LOGOUT
# ============================================================

@app.route('/logout')
def logout():

    session.clear()

    return redirect(
        url_for('index')
    )


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == '__main__':

    app.run(
        debug=True
    )
