# adapters/seed.py
"""Fill the target's database with realistic volume, quickly.

Hashing happens inside the api container with the app's own function, so the hashes match
its scheme. Rows are then bulk-inserted with SQL: registering 50k users through the API
would turn setup into a load test (bcrypt is slow on purpose).
"""
from adapters.sandbox import Sandbox, SandboxError
from swarm.manifest import Manifest

_HASH = "import sys\nfrom app.core.security import hash_password\nprint(hash_password(sys.argv[1]))"


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def seed(m: Manifest, sandbox: Sandbox) -> dict:
    unsupported = set(m.seed.rows) - {"users"}
    if unsupported:
        raise SandboxError(f"seed: no seeder for tables {sorted(unsupported)}")
    total = m.seed.rows["users"]
    named = m.auth.seed_users
    if total < len(named):
        raise SandboxError(f"seed: users={total} is fewer than the {len(named)} named seed users")

    hashes = {pw: sandbox.python(_HASH, pw).strip() for pw in {u.password for u in named}}
    if not all(h.startswith("$2") for h in hashes.values()):
        raise SandboxError(f"seed: app did not return bcrypt hashes: {list(hashes.values())!r}")

    # The schema has no default for id or is_active; role labels are uppercase in the enum.
    named_rows = ", ".join(
        f"(gen_random_uuid(), {_q(u.email)}, {_q(hashes[u.password])}, "
        f"{_q(u.role.upper())}::user_role_enum, true)" for u in named
    )
    sandbox.psql("INSERT INTO users (id, email, hashed_password, role, is_active) VALUES " + named_rows)

    filler = total - len(named)
    if filler:
        sandbox.psql(
            "INSERT INTO users (id, email, hashed_password, role, is_active) "
            f"SELECT gen_random_uuid(), 'load' || g || '@example.com', {_q(hashes[named[0].password])}, "
            f"'USER'::user_role_enum, true FROM generate_series(1, {filler}) AS g"
        )

    count = int(sandbox.psql("SELECT count(*) FROM users").strip())
    if count != total:
        raise SandboxError(f"seed: expected {total} users, found {count}")
    return {"users": count}
