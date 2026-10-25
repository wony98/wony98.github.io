# Environment-specific pitfalls

These are examples of environment-dependent failure modes, not universal workflow requirements. Load only the parts relevant to the active host and tools.

- Long inline shell payloads can break through quoting or character-encoding layers. Prefer a UTF-8 file passed to a deterministic script.
- Text redirection can corrupt binary responses. Use a binary-safe output option or library call.
- Default text writers may add a byte-order mark or change newline style. Verify bytes before external publishing.
- A command-line JSON filter may parse non-ASCII keys differently across shells. Prefer stable ASCII field names or parse structured output in a script.
- A mutation composed of read, check, and write steps needs a lock or another atomic boundary; a precondition check alone does not prevent races.

