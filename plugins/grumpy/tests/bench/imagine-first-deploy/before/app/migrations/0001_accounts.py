def up(conn):
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT NOT NULL)")


def down(conn):
    conn.execute("DROP TABLE accounts")
