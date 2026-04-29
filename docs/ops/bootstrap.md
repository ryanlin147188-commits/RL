# First-admin bootstrap (operator runbook)

A fresh AutoTest deployment has no users. The platform refuses self-service
registration without an invite or matching `email_domains`, so somebody has
to create the very first admin out-of-band.

Two ways to do it. Pick one.

## Option A — `POST /api/auth/bootstrap-invite` (UI-friendly)

Mints a single-use Admin invite that the first user can paste into the
normal `/register` flow. Endpoint is **disabled by default**; turn it on
just long enough to bootstrap, then turn it off.

### Steps

1. **Generate a strong bootstrap token** (operator-only secret):

    ```sh
    openssl rand -hex 32
    # e.g. 0f2c...b71a
    ```

2. **Set the env var on the backend container** and restart:

    ```sh
    # in .env (or your secret manager)
    echo "AUTOTEST_BOOTSTRAP_TOKEN=0f2c...b71a" >> .env

    docker compose up -d --no-deps --force-recreate backend
    ```

3. **Mint the invite** (from any host that can reach the API):

    ```sh
    curl -sS -X POST http://localhost:8000/api/auth/bootstrap-invite \
      -H 'Content-Type: application/json' \
      -d '{
        "bootstrap_token": "0f2c...b71a",
        "organization_slug": "default",
        "email": "you@example.com",
        "ttl_hours": 1
      }'
    ```

   Response:

    ```json
    {
      "invite_token": "BOOT-xxxxxxxxxxxxxxxxxxxxxxxx",
      "organization_id": "<uuid>",
      "organization_slug": "default",
      "role": "Admin",
      "expires_at": "2026-04-29T19:30:00",
      "note": "Use this token in POST /api/auth/register as `invite_token`. ..."
    }
    ```

4. **Use the invite to register** via the normal UI flow (or curl):

    ```sh
    curl -X POST http://localhost:8000/api/auth/register \
      -H 'Content-Type: application/json' \
      -d '{
        "username": "first_admin",
        "password": "your-strong-password",
        "email": "you@example.com",
        "invite_token": "BOOT-xxxxxxxxxxxxxxxxxxxxxxxx"
      }'
    ```

5. **Close the door** — unset the env var and restart so future calls
   to `/bootstrap-invite` get 503:

    ```sh
    sed -i.bak '/^AUTOTEST_BOOTSTRAP_TOKEN=/d' .env
    docker compose up -d --no-deps --force-recreate backend
    ```

   (Optional) elevate the new user to `is_superuser=True` if you want
   them to bypass RBAC for support tasks:

    ```sh
    docker exec autotest-postgres psql -U admin -d autotest_db -c \
      "UPDATE users SET is_superuser=true WHERE username='first_admin';"
    ```

### Safety properties

The endpoint refuses to mint a token unless **all** of the following hold,
so the worst that can happen if you forget to unset the env var is one
extra invite for an org that already has admins (nothing happens):

| Check | Failure code | Behaviour |
|---|---|---|
| `AUTOTEST_BOOTSTRAP_TOKEN` env unset | 503 | endpoint disabled |
| `bootstrap_token` body field doesn't match | 403 | constant-time compare |
| Target org doesn't exist | 404 | — |
| Org already has an active admin (superuser OR Admin role) | 409 | — |
| > 3 calls per hour from same IP | 429 | slowapi throttle (disabled in tests) |

The endpoint is also kept off the JWT-required path list, so an
unauthenticated request can reach it — gating is purely
`bootstrap_token` + admin-presence.

## Option B — `python -m app.cli create-admin` (no HTTP)

Direct CLI on the backend container. Doesn't touch HTTP at all, so it
works even if the gateway is down. Trade-off: SSH access required.

```sh
docker compose exec backend python -m app.cli create-admin
# interactive: prompts for username / password / email
```

Or non-interactively:

```sh
AUTOTEST_ADMIN_USERNAME=first_admin \
AUTOTEST_ADMIN_PASSWORD='your-strong-password' \
AUTOTEST_ADMIN_EMAIL=you@example.com \
docker compose exec -T backend python -m app.cli create-admin --non-interactive
```

CLI-created users are `is_superuser=True` by default; they bypass RBAC.

## Which option?

| Need | Option |
|---|---|
| You have UI / curl access; SSH is restricted | A |
| You're scripting deploys (Terraform, Ansible) | A (idempotent: 409 on retry) |
| You want a normal-looking Admin user | A |
| You want `is_superuser=True` immediately | B |
| You don't want to expose HTTP secret token to anyone | B |

## What if I lose access?

If you lose every admin's password and `bootstrap-invite` returns 409:

```sh
# 1) Soft-deactivate every existing admin so the gate flips back open
docker exec autotest-postgres psql -U admin -d autotest_db -c \
  "UPDATE users SET is_active=false
    WHERE organization_id=(SELECT id FROM organizations WHERE slug='default')
      AND (is_superuser OR role_id=(SELECT id FROM roles WHERE name='Admin'));"

# 2) Re-bootstrap via Option A
# 3) Re-activate any user you still want
```

Don't `DELETE` admins — referenced by audit_logs / todo_items / etc.
Soft delete (`is_active=false`) is the right tool.
