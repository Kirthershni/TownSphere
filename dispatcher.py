import time
import math
import logging
import oracledb
from db import get_db_connection

# Configure logging to track background dispatcher activities
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculates Euclidean distance between two spatial coordinates."""
    return math.sqrt((lat1 - lat2)**2 + (lon1 - lon2)**2)

def find_best_worker(conn, category, req_lat=12.8406, req_lng=80.1534):
    """
    Finds the optimal worker based on workload (60% weight) 
    and distance (40% weight).
    """
    cursor = conn.cursor()
    query = """
        SELECT STAFF_ID, NVL(WORKLOAD_COUNT, 0), NVL(LATITUDE, 12.8406), NVL(LONGITUDE, 80.1534) 
        FROM STAFF 
        WHERE LOWER(SPECIALIZATION) = LOWER(:1) 
          AND UPPER(AVAILABILITY_STATUS) = 'AVAILABLE'
        ORDER BY NVL(WORKLOAD_COUNT, 0) ASC
    """
    cursor.execute(query, [category])
    candidates = cursor.fetchall()
    cursor.close()
    
    if not candidates:
        return None

    ranked_workers = []
    for staff_id, workload, lat, lng in candidates:
        dist = calculate_distance(req_lat, req_lng, lat, lng)
        score = (workload * 0.6) + (dist * 0.4) 
        ranked_workers.append((score, staff_id))
    
    ranked_workers.sort(key=lambda x: x[0])
    return ranked_workers[0][1]

def dispatch_pending_complaints():
    """
    Scans for unassigned complaints (Pending Worker) and attempts
    to auto-match them to available staff members.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Fetch complaints waiting for staff allocation
        cursor.execute("""
            SELECT COMPLAINT_ID, CATEGORY, IS_EMERGENCY 
            FROM COMPLAINT 
            WHERE STATUS = 'Pending Worker'
            ORDER BY NVL(IS_EMERGENCY, 0) DESC, CREATED_DATE ASC
        """)
        pending_complaints = cursor.fetchall()

        if not pending_complaints:
            cursor.close()
            return

        logging.info(f"Found {len(pending_complaints)} pending complaint(s) for auto-dispatch.")

        for complaint_id, category, is_emergency in pending_complaints:
            staff_id = find_best_worker(conn, category)
            if staff_id:
                # Offer the complaint to the selected worker
                cursor.execute("""
                    INSERT INTO ASSIGNMENT (COMPLAINT_ID, STAFF_ID, ASSIGNMENT_STATUS)
                    VALUES (:1, :2, 'Offered')
                """, [complaint_id, staff_id])

                cursor.execute("""
                    UPDATE COMPLAINT 
                    SET STATUS = 'Offered' 
                    WHERE COMPLAINT_ID = :1
                """, [complaint_id])

                conn.commit()
                logging.info(f"Complaint #{complaint_id} ({category}) assigned to Staff #{staff_id}.")
            else:
                logging.warning(f"No available workers found for Category: '{category}' (Complaint #{complaint_id}).")

        cursor.close()
    except oracledb.DatabaseError as e:
        logging.error(f"Database error during dispatch execution: {e}")
    finally:
        if conn:
            conn.close()

def auto_escalate_emergencies():
    """
    Escalates high-priority emergency complaints that remain unassigned for over 15 minutes.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Update priority/status for stale emergencies
        cursor.execute("""
            UPDATE COMPLAINT
            SET PRIORITY = 'High'
            WHERE IS_EMERGENCY = 1 
              AND STATUS = 'Pending Worker'
              AND CREATED_DATE < (SYSDATE - INTERVAL '15' MINUTE)
        """)
        
        if cursor.rowcount > 0:
            conn.commit()
            logging.info(f"Escalated {cursor.rowcount} stale emergency complaint(s).")
            
        cursor.close()
    except oracledb.DatabaseError as e:
        logging.error(f"Error escalating emergencies: {e}")
    finally:
        if conn:
            conn.close()

def run_dispatcher_loop(interval_seconds=30):
    """Runs continuous background dispatch loop every N seconds."""
    logging.info(f"Starting Background Dispatcher Loop (Polling every {interval_seconds}s)...")
    while True:
        try:
            dispatch_pending_complaints()
            auto_escalate_emergencies()
        except Exception as e:
            logging.error(f"Unexpected error in dispatcher loop: {e}")
        time.sleep(interval_seconds)

if __name__ == '__main__':
    # Run standalone dispatcher daemon
    run_dispatcher_loop(interval_seconds=30)