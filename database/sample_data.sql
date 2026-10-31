-- TownSphere sample data
-- Oracle Database
-- These are fictional demonstration records, not real resident/staff data.
-- Run this file only after creating the TownSphere tables using schema.sql.
--
-- Password values below are placeholders and are NOT valid login passwords.
-- Replace them with Werkzeug password hashes if you need these accounts
-- to authenticate through the Flask application.

-- 1. Sample resident
INSERT INTO RESIDENT
    (NAME, EMAIL, PASSWORD, PHONE, APARTMENT_NUMBER, BLOCK_NAME,
     FLOOR_NUMBER, ACCOUNT_STATUS)
VALUES
    ('Sample Resident', 'resident.sample@example.com',
     'REPLACE_WITH_WERKZEUG_HASH', '9000000001', 'A-101',
     'Block A', 1, 'ACTIVE');

-- 2. Sample maintenance staff
INSERT INTO STAFF
    (NAME, EMAIL, PASSWORD, PHONE, SPECIALIZATION,
     AVAILABILITY_STATUS, SKILL_LEVEL, WORKLOAD_COUNT,
     LATITUDE, LONGITUDE, ACCOUNT_STATUS)
VALUES
    ('Sample Plumber', 'plumber.sample@example.com',
     'REPLACE_WITH_WERKZEUG_HASH', '9000000002', 'Plumbing',
     'AVAILABLE', 'Intermediate', 0,
     12.840600, 80.153400, 'ACTIVE');

-- 3. Sample complaint linked to the sample resident
INSERT INTO COMPLAINT
    (RESIDENT_ID, CATEGORY, DESCRIPTION, PRIORITY, STATUS,
     IMAGE_PATH, VISIT_TIME_SLOT, IS_EMERGENCY)
VALUES
    (
        (SELECT RESIDENT_ID
         FROM RESIDENT
         WHERE EMAIL = 'resident.sample@example.com'),
        'Plumbing',
        'Sample complaint: water leakage under the kitchen sink.',
        'Medium',
        'Assigned',
        NULL,
        'Anytime',
        0
    );

-- 4. Sample assignment linked to the sample complaint and staff member
INSERT INTO ASSIGNMENT
    (COMPLAINT_ID, STAFF_ID, ASSIGNMENT_STATUS)
VALUES
    (
        (SELECT COMPLAINT_ID
         FROM COMPLAINT
         WHERE DESCRIPTION =
             'Sample complaint: water leakage under the kitchen sink.'),
        (SELECT STAFF_ID
         FROM STAFF
         WHERE EMAIL = 'plumber.sample@example.com'),
        'Assigned'
    );

-- 5. Sample work progress update
INSERT INTO WORK_UPDATE
    (COMPLAINT_ID, STAFF_ID, WORK_STATUS, DESCRIPTION, IMAGE_PATH)
VALUES
    (
        (SELECT COMPLAINT_ID
         FROM COMPLAINT
         WHERE DESCRIPTION =
             'Sample complaint: water leakage under the kitchen sink.'),
        (SELECT STAFF_ID
         FROM STAFF
         WHERE EMAIL = 'plumber.sample@example.com'),
        'In Progress',
        'Sample update: inspection of the reported leakage has started.',
        NULL
    );

-- 6. Sample resident rating
-- This represents a demonstration record; add it only after the complaint
-- has been completed in an actual application workflow.
INSERT INTO RATING
    (COMPLAINT_ID, RATING_SCORE, FEEDBACK)
VALUES
    (
        (SELECT COMPLAINT_ID
         FROM COMPLAINT
         WHERE DESCRIPTION =
             'Sample complaint: water leakage under the kitchen sink.'),
        5,
        'Sample feedback for demonstration purposes.'
    );

COMMIT;
