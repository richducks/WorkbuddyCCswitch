# WorkBuddy Console Permissions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal** — Add a Flask-AppBuilder security console with password login and enforce its permissions in WorkBuddy.

**Architecture** — In permissions mode, a single Flask WSGI app serves both the existing WorkBuddy UI and the Flask-AppBuilder security views. Legacy mode retains the existing HTTP handler. Both adapters use the same controller and business dispatch. The permission database and login signing key persist in the private web data volume.

**Tech stack** — Python 3.11, Flask-AppBuilder, SQLAlchemy SQLite, Waitress, unittest and Docker Compose.

**Spec** — [Console permissions design](../specs/2026-11-25-console-permissions-design.md).

## Global constraints

- Keep the current console port and existing account, check-in, gateway and bridge data.
- When permissions mode is enabled, a valid username and password replace the legacy console token mechanism. A legacy session or the loopback no-auth flag cannot grant access in this mode.
- Initialize the local administrator only once, require a password change on first login, and preserve the changed password across restarts. Read the bootstrap password from private deployment configuration.
- Require an explicit administrator-supplied initial password for LAN deployment.

## Review focus

- Restart with the original bootstrap config after a password change. The new password still works and the initial password fails.
- Initial administrator visits a security-admin URL directly. The request is confined to password change and logout until the first change succeeds.
- Only the old loopback bypass or an old Basic token is supplied in enabled mode. Protected requests stay unauthenticated.
- A logged-in operator loses a role or is disabled. Their next request uses the updated authorization state.
- A read-only summary or session lookup includes sensitive nested data. The summary withholds secrets and login QR data requires account-management permission.

### Task 1 — Login, bootstrap and security views

**Files** — Create `app/permissions.py`, `requirements-permissions.txt` and `scripts/test_permissions.py`.

**Interface** — `build_permissions_app(controller: Controller, web_data: Path, *, bootstrap_password: str | None, bind_ip: str) -> Flask` initializes the app and persistent SQLite database. `require_permission(action: str) -> Response | None` allows the Flask adapter to authorize requests, and returns a 401 or 403 response when blocked.

- [ ] Add failing tests for a fresh bootstrap login, direct access to security administration before initial password change, incorrect current password, and a restart authenticating only with the changed password. Use fixture data under a temporary directory.
- [ ] Run `python3 scripts/test_permissions.py` and verify tests fail at the missing app integration.
- [ ] Build the Flask-AppBuilder app with its database-authentication security manager. Add a durable first-change flag to the initial account and a CSRF-protected password-change form. Apply the restriction before routing to any protected app or FAB view. Preserve the existing administrator on startup and fail closed if a new deployment lacks an acceptable bootstrap password.
- [ ] Run `python3 scripts/test_permissions.py` and verify the authentication, initial-password and persistence cases pass.
- [ ] Commit the login and bootstrap implementation without staging prior workspace changes.

### Task 2 — Protect the existing WorkBuddy console

**Files** — Create `app/console_routes.py`. Modify `app/permissions.py`, `app/web.py`, `app/static/index.html` and `scripts/test_permissions.py`.

**Interface** — `dispatch_console_api(controller: Controller, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]` executes the existing API business operations in either mode. The Flask adapter checks role permissions and request integrity before dispatch.

- [ ] Add failing tests for anonymous and read-only access, role revocation in an existing session, disabled users, rejected cross-origin writes, and API calls carrying a legacy token. Exercise both `/api/accounts/move` and `/api/accounts/promote`. Assert read-only responses do not expose keys or QR data.
- [ ] Run `python3 scripts/test_permissions.py` and verify these cases fail before route integration.
- [ ] Extract existing API dispatch from the HTTP handler and mount it under Flask. Map method and path to explicit FAB business permissions. Separate status, account operations, check-in settings, provider configuration and key access. Direct request checks and UI capability checks use the same permissions. Serve the existing console and add navigation to the security backend for authorized users.
- [ ] Run `python3 scripts/test_permissions.py`, `python3 scripts/test_console.py` and `python3 scripts/test_providers.py` and verify all pass.
- [ ] Commit the API integration separately.

### Task 3 — Deployment and verification

**Files** — Modify `app/boot.py`, `Dockerfile`, `docker-compose.yml`, `.env.example` and the setup section of `README.md`.

**Interface** — `WB_CONSOLE_PERMISSIONS=1` selects the Flask server. Mount the private bootstrap configuration from the existing web data volume. Keep the legacy listener only in explicitly selected legacy mode.

- [ ] Add deployment configuration tests for permissions mode taking precedence over the legacy bypass, and initialization failure leaving no unauthenticated listener.
- [ ] Run the relevant console tests and see the new startup assertions fail before boot integration.
- [ ] Wire startup, shutdown and health checks. The permissions-mode health check must confirm readiness without granting access to console data. Enable permissions mode for the local deployment and supply the agreed password through private configuration.
- [ ] Run the console, permissions, provider and gateway check-in suites. Build and start with `docker compose up -d --build --wait`. Verify anonymous access is rejected, the password login page loads, and both the gateway and usage bridge are healthy. Use a temporary data directory to verify the complete first-login, password change and restart cycle without modifying the live administrator password.
- [ ] Commit deployment wiring, and report the local URL and the precise verification results.
