"""SQLite access for the accounts service."""

import sqlite3


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn
