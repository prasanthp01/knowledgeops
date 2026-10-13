# Container and dependency audit

Recorded on 2026-10-13 using pip-audit and Trivy 0.73.0. Exact dependency versions are hash-pinned; image digests are in Dockerfiles and scan receipts.

- Initial Debian app image: 64 HIGH/CRITICAL findings, including 20 with fixes available. Applying OS updates removed those fixable findings; 44 HIGH findings remained without listed fixes. The app was rebuilt on the official pinned Python 3.12 Alpine base, with OS upgrades, instead of retaining unnecessary Debian packages. Final app scan: zero HIGH/CRITICAL findings.
- Initial official PostgreSQL Alpine image: 25 HIGH/CRITICAL Go findings in the `gosu` privilege-switch binary. The project starts the image as the existing postgres user and removes the unused executable. This was checked against both an existing volume and a fresh initialized volume. Final PostgreSQL scan: zero HIGH/CRITICAL findings.
- Runtime Python dependency audit: no known vulnerabilities reported in the locked runtime requirements.

The receipts are `evidence/container-audit.json`, `evidence/postgres-audit.json` and `evidence/dependency-audit.json`. Prior scans remain in the local repository; the source archive omits superseded bulky reports. Scans cover reported findings at that time and severity selection. Trivy warned that Alpine 3.24 was absent from its EOL lookup. Scanner/database limitations, future disclosures, lower-severity findings, application design and configuration require ongoing review. No independent penetration test or security certification is claimed.

## Deployed x86_64 image update

Before production activation, all-severity scans additionally exposed six pip findings in the app image and two fixable Alpine library findings in PostgreSQL. Removed the unused pip/ensurepip installers and upgraded the database OS libraries. Exact server-built images then reported zero findings at all severities. See `evidence/deployment/final-app-scan.json`, `final-db-scan.json` and `DEPLOYMENT.md`. The earlier HIGH/CRITICAL-only checks remain historical.
