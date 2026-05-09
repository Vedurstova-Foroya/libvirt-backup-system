# Agent Handbook

## Generic Instructions

### Working Agreement
- Operate as the top-level manager agent for repository tasks. Focus on conserving the context. Delegate implementation and execution work to worker agents whenever feasible, and keep the manager focused on orchestration, review, and validation.

### Validation Scope
- Unless the user explicitly asks for a push to the git remote, stop after a quick pass or self-check and do not run the full validation pipeline by default. Do not run the full CI workflow, or git hook-driven pre-push path unless a push has been requested.

### Repository Quality Gates
- Treat CI completeness as a push gate: all required jobs must pass, the 300 LOC maximum policy must remain enforced, all tests must pass, and warnings must be treated as errors.

### Pre-Push Process
- Before pushing to the git remote, ensure the full CI workflow has been run completely and let the normal git hooks run in full. Never use `git push --no-verify`. Do not skip, mock, fake, short-circuit, or otherwise alter any CI configuration, CI setup, git hook, or CI execution path to get a passing result.
