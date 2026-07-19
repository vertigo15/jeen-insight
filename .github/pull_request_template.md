## Summary
- 

## Text-to-SQL hardening checklist
- [ ] The increment scope and its risk are described above.
- [ ] Deterministic controls remain enforced in code; no setting can bypass authorization, SQL validation, DLP, or execution limits.
- [ ] Focused tests pass.
- [ ] `pytest tests/unit/` passes.
- [ ] `python -m evals.run_eval --min-safety 1.0 --min-groundedness 1.0` passes.
- [ ] Relevant integration or load tests pass, or their omission is explained.
- [ ] The diff received an Opus 4.8 review after tests passed.
- [ ] Every review finding is fixed, covered by a regression test, or has an approved deferral.
- [ ] No High or Critical review finding remains unresolved.

## Validation evidence
<!-- Include commands, results, and any baseline/after metrics. -->

## Rollback plan
<!-- State how to disable or revert this increment safely. -->
