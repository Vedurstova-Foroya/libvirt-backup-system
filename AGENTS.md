# Agent Handbook

## Generic Instructions

### Working Agreement
1. Operate as the top-level manager agent for repository tasks. Delegate implementation and execution work to worker agents whenever feasible, and keep the manager focused on orchestration, review, and validation.

### Validation Scope
2. Unless the user explicitly asks for a push to the git remote, stop after a quick pass or self-check and do not run the full validation pipeline by default. Do not run the reviewer loop, full CI workflow, or git hook-driven pre-push path unless a push has been requested.

### Pre-Push Process
3. Before pushing to the git remote, spawn a fresh reviewer agent to review the current change set. Route every review finding back through the pipeline to a worker agent for fixes, then rerun a fresh reviewer. Repeat until the reviewer reports no findings and explicitly says `LGTM`, with a hard cap of 10 review/fix iterations.
4. Before pushing to the git remote, ensure the full CI workflow has been run completely and let the normal git hooks run in full. Never use `git push --no-verify`. Do not skip, mock, fake, short-circuit, or otherwise alter any CI configuration, CI setup, git hook, or CI execution path to get a passing result.
5. Before pushing to the git remote, spawn a dedicated refactor-review sub-agent to inspect the current change set for opportunities to reduce redundancy, keep code DRY, simplify implementation details, and preserve a clean, minimal design. Route actionable refactor findings through a worker agent for fixes before the final push validation completes.

## Repository-Specific Requirements

### Repository Quality Gates
7. Treat CI completeness as a push gate: all required jobs must pass, the 300 LOC maximum policy must remain enforced, all tests must pass, and warnings must be treated as errors.
8. Require 100/100/100/100 100% test coverage for all authored source code.
