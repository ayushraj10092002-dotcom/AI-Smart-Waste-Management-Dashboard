# WasteWise — Smart Waste Management

WasteWise is an incremental Flask SaaS prototype for organization-scoped waste operations. It preserves the existing operational dashboard and provides a persistent foundation for users/roles, organization-owned waste datasets, bins, locations, collection tasks, alerts, audit events, device credentials, and queued notifications.

## Architecture and current scope

- Flask routes keep the existing Dashboard, Analytics, Locations, Smart Bins, Collection Planner, Prediction, Classification, Alerts, and Reports pages.
- Flask-SQLAlchemy models support SQLite locally and PostgreSQL through `DATABASE_URL`. Alembic migrations are in `migrations/`; migrations add/update schema without dropping application data.
- Flask-Login authentication uses Werkzeug password hashes. Admin bootstrap is an interactive CLI operation; there is no public signup or default account.
- Reusable role permissions are in `services/access.py`. Every operational data query and write is scoped by organization; Staff task/bin/location/alert reads are further assignment-scoped.
- Audit records store UTC event time, user/organization IDs, action, resource, and status. Passwords and tokens are never included.
- English/Hindi catalog scaffolding and organization language, unit, date, timezone, and currency preferences are stored. Waste is stored and exported in kg; the dashboard converts display values to kg, tonnes, or lbs.
- `waste_data.csv` is copied as a separate demo dataset when an organization is provisioned. Static location/bin values and rule-generated alerts are explicitly simulated. No external IoT, GPS, email, SMS, or push provider is connected.
- Prediction remains a limited-data LinearRegression prototype. Classification is a fixed disposal guide, not computer vision. Recycling/environmental metrics are not inferred.

This is a prototype foundation, not a claim of production security, regulatory compliance, or completed enterprise operations.

## Local installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
python3 -m flask --app app db upgrade
python3 -m flask --app app create-admin --organization "Example Organization" --email admin@example.com
python3 app.py
```

The admin command prompts for a password and confirmation. Passwords must contain at least 12 characters and are stored only as password hashes. Local SQLite defaults to `instance/wastewise.sqlite`.

After starting the app, visit `/register` to create the first organization and administrator account. Registration requires a unique organization name and email address, validates password strength, and stores only a secure password hash. Existing deployments should apply migrations before using registration:

```bash
python3 -m flask --app app db upgrade
```

## Environment variables

- `SECRET_KEY`: required when `WASTEWISE_ENV=production`; used to sign sessions and CSRF tokens. Keep it private and stable across instances.
- `WASTEWISE_ENV`: use `production` to enable secure session cookies, HSTS, and required secret configuration.
- `DATABASE_URL`: optional for local development, where SQLite is the default. Required in production and must be a persistent PostgreSQL connection URL; Render's local filesystem is ephemeral, so do not use SQLite there. PostgreSQL URLs are accepted with the `psycopg` driver.
- `PORT`: optional direct-run port; hosting platforms generally supply it.

## Authentication and roles

Unauthenticated page requests redirect to `/login`; unauthenticated API requests return 401. `/` sends signed-in users to `/dashboard`. Logout is a CSRF-protected POST.

Roles are Admin, Manager, Staff, Student/User, and Viewer. Admins can manage organization users, locations, bins, reports, settings, and audit events. Managers manage analytics, collection workflows, and alerts. Staff are limited to assigned tasks/resources. Student/User gets disposal guidance. Viewer is read-only. Admin user management is at `/admin/users`; the audit view is at `/admin/audit`. The operational navbar is unchanged.

## Database setup and migration

Apply reviewed migrations with:

```bash
python3 -m flask --app app db upgrade
```

Create a new revision after changing models with `python3 -m flask --app app db migrate -m "describe change"`, review the generated migration, then apply it with `db upgrade`. Do not use a destructive reset against a database containing user data. For hosted deployments, provision PostgreSQL and set `DATABASE_URL` to that service's private connection URL.

## Run and deploy

For local development, use `python3 app.py`. For a production WSGI server:

```bash
gunicorn app:app
```

Render configuration is in `render.yaml`; create a Render PostgreSQL database (or use another managed PostgreSQL instance) and set the service's `DATABASE_URL` to its private connection URL before deploying. The blueprint marks this value as required input and the application rejects missing or non-PostgreSQL production URLs. The start command applies migrations before launching Gunicorn. Run `create-admin` once after the first deploy. `/health` is a minimal liveness check and does not expose infrastructure details.

## API

The new integration surface is under `/api/v1`. Session-authenticated writes require CSRF tokens; retrieve a session token from `GET /api/v1/csrf` and send it in `X-CSRFToken`. Device telemetry uses organization-scoped bearer credentials created by `python3 -m flask --app app create-device-token`; there is no public unauthenticated telemetry endpoint. Existing dashboard APIs remain available with authentication and permissions.

Endpoint methods, roles, request fields, and error behavior are documented in [docs/API.md](docs/API.md).

## Tests

```bash
python3 -m pytest -v
```

Tests use a temporary SQLite database and do not modify the configured local or production database.

## Project structure

- `app.py` — application config, auth/RBAC routes, operational APIs, health, and CLI commands
- `models/database.py` — organization-scoped SQLAlchemy schema
- `migrations/` — Alembic schema history
- `services/access.py`, `services/audit.py`, `services/tenant_data.py` — permission, audit, and tenant data boundaries
- `services/demo_data.py` — explicitly simulated operations fixtures and rule-generated alerts
- `services/notifications.py` — notification outbox; no external provider delivery
- `services/prediction.py`, `models/waste_model.py` — limited-data forecasting prototype
- `templates/` and `static/` — existing operational UI, preserved and incrementally extended
