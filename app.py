import os
import math

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

    name = request.form['name']
    email = request.form['email']

    password = generate_password_hash(
        request.form['password']
    )

    phone = request.form.get(
        'phone',
        '0000000000'
    )

    apt = request.form['apartment']

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            INSERT INTO RESIDENT
            (
                NAME,
                EMAIL,
                PASSWORD,
                PHONE,
                APARTMENT_NUMBER
            )
            VALUES
            (
                :1,
                :2,
                :3,
                :4,
                :5
            )
            """,
            [
                name,
                email,
                password,
                phone,
                apt
            ]
        )

        conn.commit()

        flash(
            'Registration successful! Please login.',
            'success'
        )

        return redirect(
            url_for(
                'index',
                role='resident',
                action='login'
            )
        )

    except oracledb.DatabaseError:

        conn.rollback()

        flash(
            'Registration failed. Email might already exist.',
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

    name = request.form['name']
    email = request.form['email']

    password = generate_password_hash(
        request.form['password']
    )

    specialization = request.form[
        'specialization'
    ]

    phone = request.form.get(
        'phone',
        '0000000000'
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            INSERT INTO STAFF
            (
                NAME,
                EMAIL,
                PASSWORD,
                SPECIALIZATION,
                AVAILABILITY_STATUS,
                WORKLOAD_COUNT
            )
            VALUES
            (
                :1,
                :2,
                :3,
                :4,
                'AVAILABLE',
                0
            )
            """,
            [
                name,
                email,
                password,
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
            'Registration failed. Email might already exist.',
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

        # Total complaints
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM COMPLAINT
            """
        )

        stats['total'] = cursor.fetchone()[0]

        # Pending complaints
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

        # In-progress complaints
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

        # Resolved complaints
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM COMPLAINT
            WHERE STATUS = 'Completed'
            """
        )

        stats['resolved'] = cursor.fetchone()[0]

        # All complaints
        cursor.execute(
            """
            SELECT
                C.COMPLAINT_ID,
                R.NAME,
                C.CATEGORY,
                C.PRIORITY,
                C.STATUS,
                S.NAME,
                C.CATEGORY
            FROM COMPLAINT C

            JOIN RESIDENT R
                ON C.RESIDENT_ID =
                   R.RESIDENT_ID

            LEFT JOIN ASSIGNMENT A
                ON C.COMPLAINT_ID =
                   A.COMPLAINT_ID
                AND A.ASSIGNMENT_STATUS =
                    'Accepted'

            LEFT JOIN STAFF S
                ON A.STAFF_ID =
                   S.STAFF_ID

            ORDER BY
                C.CREATED_DATE DESC
            """
        )

        complaints = cursor.fetchall()

        # Available staff
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

        # Staff roster
        cursor.execute(
            """
            SELECT
                STAFF_ID,
                NAME,
                SPECIALIZATION,
                NVL(SKILL_LEVEL, 'Standard'),
                CASE
                    WHEN UPPER(AVAILABILITY_STATUS)
                         = 'AVAILABLE'
                    THEN 1
                    ELSE 0
                END
            FROM STAFF
            """
        )

        staff_list = cursor.fetchall()

        return render_template(
            'admin_dashboard.html',
            stats=stats,
            complaints=complaints,
            available_staff=available_staff,
            staff_list=staff_list
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
            staff_list=[]
        )

    finally:

        cursor.close()
        conn.close()


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

    name = request.form['name']
    username = request.form['username']

    password = generate_password_hash(
        request.form['password']
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        cursor.execute(
            """
            INSERT INTO STAFF
            (
                NAME,
                EMAIL,
                PASSWORD,
                SPECIALIZATION,
                AVAILABILITY_STATUS
            )
            VALUES
            (
                :1,
                :2,
                :3,
                'Administration',
                'ACTIVE'
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
            f'Failed to create administrator: {str(e)}',
            'error'
        )

    finally:

        cursor.close()
        conn.close()

    return redirect(
        url_for('admin_dashboard')
    )


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

        return redirect(url_for('index'))

    complaint_id = request.form.get(
        'complaint_id'
    )

    staff_id = request.form.get(
        'staff_id'
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

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
                'Accepted'
            )
            """,
            [
                complaint_id,
                staff_id
            ]
        )

        cursor.execute(
            """
            UPDATE COMPLAINT
            SET STATUS = 'Assigned'
            WHERE COMPLAINT_ID = :1
            """,
            [complaint_id]
        )

        conn.commit()

        flash(
            f'Manually assigned complaint #{complaint_id} '
            f'to worker #{staff_id}.',
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

    category = request.form.get(
        'category'
    )

    priority = request.form.get(
        'priority'
    )

    time_slot = request.form.get(
        'time_slot',
        'Anytime'
    )

    description = request.form.get(
        'description'
    )

    is_emergency = (
        1
        if request.form.get('is_emergency')
        else 0
    )

    # --------------------------------------------------------
    # IMAGE UPLOAD
    # --------------------------------------------------------

    image_filename = None

    if 'image' in request.files:

        file = request.files['image']

        if file and allowed_file(
            file.filename
        ):

            image_filename = secure_filename(
                file.filename
            )

            file.save(
                os.path.join(
                    app.config['UPLOAD_FOLDER'],
                    image_filename
                )
            )

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
                """,
                [
                    complaint_id,
                    assigned_staff_id
                ]
            )

            flash(
                f'Complaint #{complaint_id} submitted! '
                f'Automatically offered to the best available '
                f'worker based on skill, workload and proximity.',
                'success'
            )

        else:

            flash(
                f'Complaint #{complaint_id} submitted! '
                f'No suitable worker is currently available. '
                f'Worker allocation queued.',
                'success'
            )

        conn.commit()

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

        flash(
            'Unauthorized access.',
            'error'
        )

        return redirect(url_for('index'))

    assignment_id = request.form.get(
        'assignment_id'
    )

    complaint_id = request.form.get(
        'complaint_id'
    )

    action = request.form.get(
        'action'
    )

    staff_id = session.get(
        'user_id'
    )

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
                    DECISION_DATE =
                        CURRENT_TIMESTAMP
                WHERE ASSIGNMENT_ID = :1
                  AND STAFF_ID = :2
                """,
                [
                    assignment_id,
                    staff_id
                ]
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
                    DECISION_DATE =
                        CURRENT_TIMESTAMP
                WHERE ASSIGNMENT_ID = :1
                  AND STAFF_ID = :2
                """,
                [
                    assignment_id,
                    staff_id
                ]
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

            # Don't send the complaint back to the worker
            # who just rejected it.

            if next_worker_id == staff_id:

                cursor.execute(
                    """
                    SELECT STAFF_ID
                    FROM STAFF
                    WHERE LOWER(TRIM(SPECIALIZATION)) =
                          LOWER(TRIM(:1))
                      AND UPPER(TRIM(AVAILABILITY_STATUS))
                          = 'AVAILABLE'
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

            if next_worker_id:

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
                    """,
                    [
                        complaint_id,
                        next_worker_id
                    ]
                )

            else:

                cursor.execute(
                    """
                    UPDATE COMPLAINT
                    SET STATUS = 'Pending Worker'
                    WHERE COMPLAINT_ID = :1
                    """,
                    [complaint_id]
                )

            conn.commit()

            flash(
                'Task declined. Automatic re-allocation initiated.',
                'error'
            )

    except Exception as e:

        conn.rollback()

        flash(
            f'An error occurred while processing task response: {str(e)}',
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

        cursor.execute(
            """
            UPDATE ASSIGNMENT
            SET ASSIGNMENT_STATUS = 'Re-opened'
            WHERE COMPLAINT_ID = :1
            """,
            [complaint_id]
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

    new_status = request.form.get(
        'status'
    )

    staff_id = session.get(
        'user_id'
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

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

    return redirect(
        url_for('staff_dashboard')
    )


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