# Verification discipline

Use these checks when a thread request depends on evidence rather than simple coordination.

## Completeness

- Do not describe a partial page, time window, or sample as a complete check.
- State the searched scope, filters, ordering, pagination, and known omissions.
- Recheck volatile evidence immediately before a consequential reply.

## Reproducibility

- Use the version and execution path fixed by the target repository or artifact.
- Start from a clean, documented state. Manually supplying missing prerequisites invalidates a clean-run claim unless those prerequisites are part of the documented setup.
- Record commands, exit status, relevant counts, and environmental assumptions.

## Evidence strength

- Label claims as confirmed, inferred, or unverified.
- Correct exaggerated or false claims in the same public surface where they were made when authorized to do so.
- Separate factual observations from evaluation or attribution.

## Method validity

- Confirm that the inspection method can observe the target behavior. Static source, server output, rendered state, and interactive behavior are different evidence surfaces.
- Inspect the diff first so the test targets what actually changed.
- Prefer focused counterexamples that can falsify the claim over broad checks that only appear comprehensive.

