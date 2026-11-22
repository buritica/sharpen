def up(conn):
    conn.execute("ALTER TABLE accounts ADD COLUMN plan TEXT NOT NULL")
