## Summary
<!-- Describe the change and the problem it solves. -->

## Change type
- [ ] Security
- [ ] Bug fix
- [ ] Feature
- [ ] Documentation
- [ ] Operations / deployment

## Validation
- [ ] Relevant unit tests added or updated
- [ ] Relevant integration tests run
- [ ] Lint and static security checks pass
- [ ] CI is green on this exact commit

## Security and tenancy
- [ ] Authorization is fail-closed
- [ ] Tenant boundaries and RLS behavior are preserved
- [ ] No credentials, tokens, or private tenant data are committed
- [ ] Protected production actions still require explicit human approval
- [ ] Any migration is documented and safe to apply in order

## Operational impact
- [ ] Configuration changes and required secrets are documented
- [ ] Rollback or recovery steps are documented when needed
- [ ] Production-readiness claims are supported by evidence

## Review
- [ ] A reviewer other than the PR author has reviewed the change
- [ ] CODEOWNERS review is complete where applicable
