# ADR-0002: Ship on Django 5.2 LTS, keep the apps 4.2-compatible

**Status:** Accepted
**Date:** 2026-11-03
**Deciders:** Rajkumar Madhu (platform lead)

## Context

`requirements.txt` pinned `Django>=4.2,<5.0`. Django 4.2 LTS reached end of life in April 2026. pip-audit
reports 8 advisories against 4.2.30 whose fixes exist only in 5.2 and 6.0. Broker-dealer clients audited
under SEBI CSCRF will flag an unsupported framework.

The apps are also dropped into LinkedEyeWebProject, which still runs Django 4.x.

## Decision

This project ships on Django 5.2 LTS (`>=5.2.18,<6.0`), which has security support until April 2028.
The code stays compatible with 4.2 so LinkedEyeWebProject keeps working, and CI tests both.

## Options Considered

| Option | Security | Effort | Risk |
|---|---|---|---|
| Stay on 4.2 | 8 open advisories, more each month | None now | Audit finding; no patches |
| 5.2 LTS, 4.2-compatible code (chosen) | Supported to April 2028 | Low: full suite passed on 5.2.18 unchanged | Deprecations for 6.0 to clear |
| Jump to 6.0 | Supported, shorter window | Medium | LinkedEyeWebProject gap widens |

## Consequences

- Clean pip-audit today. The 836-test suite runs on 4.2 and 5.2.
- The Django 6.0 deprecations are already cleared:
    - `format_html()` with no arguments;
    - `URLField`'s default scheme (`FORMS_URLFIELD_ASSUME_HTTPS`). Django flags that transitional setting
      itself as deprecated; delete it when moving to 6.0, where https is already the default.
- LinkedEyeWebProject should plan its own 5.2 upgrade. Until then it runs these apps on an unsupported
  framework, and that risk belongs to that project.

## Action Items
1. [x] Bump `requirements.txt`; update the README and STRUCTURE docs
2. [x] CI matrix: Django 4.2 and 5.2 jobs
3. [ ] LinkedEyeWebProject upgrade ticket
