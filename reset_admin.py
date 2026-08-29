from werkzeug.security import generate_password_hash
from db import get_db_connection

conn = get_db_connection()
cursor = conn.cursor()

new_password = "Admin@123"
password_hash = generate_password_hash(new_password)

cursor.execute("""
    UPDATE STAFF
    SET PASSWORD = :1
    WHERE STAFF_ID = 1
      AND UPPER(SPECIALIZATION) = 'ADMINISTRATION'
""", [password_hash])

conn.commit()

print("Admin password reset successfully.")
print("Email    : admin@townsphere.com")
print("Password : Admin@123")

cursor.close()
conn.close()