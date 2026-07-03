import sqlite3
import pandas as pd
from pathlib import Path

DB_PATH = Path("data/alert_history.db")

def get_connection():
    DB_PATH.parent.mkdir(exist_ok=True)
    return sqlite3.connect(DB_PATH)

def read_alerts():
    if not DB_PATH.exists():
        return pd.DataFrame()

    conn = get_connection()

    try:
        df = pd.read_sql_query(
            """
            SELECT *
            FROM alert_history
            ORDER BY data_alerta DESC
            """,
            conn
        )
    except Exception:
        df = pd.DataFrame()

    conn.close()
    return df
