"""Account lookups."""

PLAN_LIMITS = {"free": 1, "team": 10, "enterprise": 1000}


def get_account(conn, account_id):
    row = conn.execute(
        "SELECT id, email, plan FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return dict(row) if row else None


def seat_limit(conn, account_id):
    account = get_account(conn, account_id)
    return PLAN_LIMITS[account["plan"]]
