"""Account lookups."""


def get_account(conn, account_id):
    row = conn.execute("SELECT id, email FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None
