# Security Policy

## Supported Versions

Security fixes are applied to the latest minor release on the `main` branch. Older versions are not maintained.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

**Please do NOT open a public GitHub issue for security vulnerabilities.**

If you believe you have found a security vulnerability in AutoTest, report it privately through one of the following channels:

1. **GitHub Security Advisory** (preferred):
   Open a private advisory at <https://github.com/ryanlin147188-commits/RL_TMP/security/advisories/new>
2. **Email**: <ryanlin147188@gmail.com>
   Please use the subject line `[AutoTest Security] <short title>`.

When reporting, please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce, or a proof-of-concept
- The affected version / commit hash
- Your suggested fix, if any
- Whether you intend to publicly disclose, and on what timeline

## What to Expect

- We will acknowledge receipt within **72 hours**.
- We aim to provide an initial assessment within **7 days**.
- For confirmed vulnerabilities, we will work with you on a coordinated disclosure timeline (typically 30–90 days depending on severity).
- We will credit you in the release notes unless you request anonymity.

## Out of Scope

The following are generally **not** considered security vulnerabilities:

- Issues requiring physical access to a user's device
- Vulnerabilities in third-party dependencies that are already publicly disclosed (please report upstream first)
- Self-XSS that requires the victim to paste attacker-controlled content into the browser console
- Missing security headers without a demonstrated impact
- Default credentials in `docker-compose.dev.yml` or local development configurations (production deployments are expected to override these — see [README](README.md#deployment))

## Hardening Recommendations for Self-Hosted Deployments

When running AutoTest in production, please ensure:

- `AUTOTEST_JWT_SECRET` and `AUTOTEST_FERNET_KEY` are set to long random values (the deploy scripts auto-generate these if absent).
- `ALLOWED_ORIGINS` is set to your actual front-end origin(s) — never `*`.
- Database, S3, and admin user credentials are rotated from any defaults.
- The service is placed behind HTTPS (e.g., a reverse proxy with a valid TLS certificate).
- Container images are pinned to specific versions / digests rather than `latest`.
- Backups of the PostgreSQL volume and SeaweedFS volume are scheduled.

Thank you for helping keep AutoTest and its users safe.
