import os
import oracledb

# Database connection details with fallback environment variables

DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_DSN = os.getenv("DB_DSN")

def get_db_connection():
    """Establishes and returns a connection to the Oracle Database."""
    try:
        conn = oracledb.connect(
            user=DB_USER,
            password=DB_PASSWORD,
            dsn=DB_DSN
        )
        return conn
    except oracledb.DatabaseError as e:
        print(f"Database Connection Error: {e}")
        raise e
