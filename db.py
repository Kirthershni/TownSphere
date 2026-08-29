import os
import oracledb

# Database connection details with fallback environment variables
DB_USER = os.environ.get("DB_USER", "system")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "kirthu")
DB_DSN = os.environ.get("DB_DSN", "localhost:1521/XE")

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