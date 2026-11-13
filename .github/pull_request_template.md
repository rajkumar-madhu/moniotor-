## What changes and why

<!-- The behaviour change and the reason. Link the issue: Closes #… -->

## Validation

<!-- Commands you ran and what you saw, e.g. `make check`, `make test TEST=network.tests`. -->

## Checklist

- [ ] `make check` and `make test` pass
- [ ] Tests added or updated for behaviour, permission or integration changes
- [ ] Migration included and `make migrations-check` passes (if a model changed)
- [ ] New or changed `LINKEDEYE_*` settings are in `.env.example` and `docs/DEPLOYMENT.md`
- [ ] Records are site-scoped (`scope_by_site`, `can_on`), mutations are audited, sockets go through `probes.checks`
- [ ] No secrets, customer inventories or real host names / addresses committed
- [ ] Screenshots attached for UI changes
- [ ] Docs updated (`docs/MODULES.md` for a module contract change, `docs/STRUCTURE.md` for a layout change)
