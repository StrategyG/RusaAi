# Container vulnerability exceptions

**Review date:** 2026-11-06  
**Owner:** RUSA-AI platform maintainers  
**Expiry:** 2026-12-06  
**Scope:** Official Python 3.13 Trixie inference API and agent runtime images.

The 2026-11-06 Trivy scan reported the HIGH OS findings below without a Debian fixed version in the scanner database. The agent runtime's fixable msgpack and urllib3 findings are remediated by dependency upgrades; setuptools and other build-only tooling are removed from the agent virtual environment. No CRITICAL finding is accepted by this register.

| CVE | Component | Mitigation / exposure assessment |
|---|---|---|
| CVE-2026-76642 | util-linux mount helpers | No mount helper use; non-root, read-only root, all capabilities dropped, no-new-privileges. |
| CVE-2026-78408 | util-linux nsenter/cgroup | No nsenter or cgroup migration; no capabilities. |
| CVE-2026-78409 | util-linux X-mount.subdir | No X-mount usage; no mount capabilities. |
| CVE-2026-78410 | util-linux restricted bind mounts | No mount operations; no capabilities and read-only root. |
| CVE-2026-54369 | libacl1 | No ACL-management path; non-root and no privilege escalation. |
| CVE-2025-69720 | ncurses | No terminal UI, TTY input, or ncurses use in the HTTP API/worker. |
| CVE-2026-119373 | libsqlite3-0 | Application persistence is PostgreSQL; no SQLite code path or user-supplied SQLite file. |
| CVE-2026-119374 | libsqlite3-0 / FTS5 | No SQLite FTS5 use; PostgreSQL is the only application database. |
| CVE-2026-119379 | libsqlite3-0 / session extension | No SQLite session-extension use; PostgreSQL is the only application database. |
| CVE-2026-16742 | libsystemd0 / libudev1 | systemd-homed is not run in the containers; non-root and no capabilities. |
| CVE-2026-9538 | perl-base | No Perl runtime or Perl archive extraction path. |
| CVE-2026-76844 | zlib1g mapping | Advisory title refers to webpack-dev-middleware; no Node.js or webpack runtime is present. Retained for upstream database review. |
| CVE-2026-85091 | zlib1g | No untrusted gzip/archive extraction endpoint; request-size limits, read-only model mount, non-root runtime, no capabilities. |

These are temporary, scoped risk acceptances, not claims that the affected packages are patched. Exact CVE IDs are listed in the root .trivyignore file. The CI validator checks that the ignore list matches this register and that the expiry has not passed. The pipeline:
- blocks every CRITICAL finding without exception;
- blocks HIGH findings not explicitly listed in the expiring exception file;
- uploads unfiltered HIGH/CRITICAL JSON reports for both application images for 30 days;
- fails when an exception expires unless the finding is fixed or explicitly re-reviewed and renewed.

At expiry, maintainers must verify current Debian security status, upgrade affected packages or change the runtime base, and remove resolved exceptions. Any evidence that makes one of these findings reachable invalidates its mitigation immediately.
