# Admin/User Surface Audit (2026-03-05)

## Scope
- Repository: `Deadlock`
- Purpose: verify where admin domain and admin Twitch links are registered.

## Findings
- Admin public origin constants:
  - `service/dashboard.py`
    - `MASTER_DASHBOARD_PUBLIC_URL = "https://admin.earlysalty.de"`
    - Discord callback derived from admin domain
- Master-dashboard Twitch link target:
  - `service/dashboard.py`
    - `MASTER_DASHBOARD_TWITCH_URL = "https://twitch.earlysalty.com/twitch/admin"`
    - used as admin-only jump link from master dashboard
- CSRF/origin validation uses configured allowed origins:
  - `service/dashboard.py`
    - `_build_allowed_request_origins()`
    - `_is_allowed_request_origin()`
- Security workflows cover admin path checks:
  - `.github/workflows/dashboard-auth-guard.yml`
  - `.github/workflows/master-dashboard-auth-guard.yml`

## Classification
- All above entries are admin-surface registrations; no user-surface route changes were required in this repo for the `admin.earlysalty.de/twitch/dashboard*` issue.
