# consumer-contract-reviewer
model: sonnet
verification: evidence-query
**Checks:** Changes to cortex-utils names, signatures, pydantic model fields and DDL that sibling services (gateway, triage, postmark) import, and imports of service code into this library.
**Spawn when:** diff renames, removes or changes the signature/default of a public name in `src/cortex_utils/` (`__init__.py` exports, `triage_config/models.py`, `queue/` public functions, `cli.py` commands), changes a pydantic model field or a table/column used by consumers, or adds an import of `triage`, `postmark` or `gateway`; skip otherwise.

**Proof:** greps this repo for the changed name, and the diff for the consumers' documented import sites; consumer breakage that cannot be shown from the diff is labelled judgement.

cortex-utils is the shared library: services import it as a package and must never import each other (CLAUDE.md "NEVER cross-import between services"). A change here reaches every consumer on its next pin bump, and deployment order has broken `PUT /config` before (the upsert arbiter, #56).

**What to flag:**

1. Removal, rename or positional-argument reordering of a public function/class/model field with no deprecation alias and no mention in the PR description of consumers.
2. A new required parameter, or a changed default that alters behaviour, on a function the queue/triage_config packages export.
3. A DDL change (column drop/rename, constraint rename) whose old form is still referenced by DDL text in `schema.py` or by the exported SQL helpers.
4. Any `import triage`, `import postmark`, `import gateway` (or `from` forms) in `src/`.
5. A pydantic model in `triage_config/models.py` with a field removed or made stricter, which rejects previously valid stored config.

**Do NOT flag:**
- Private (underscore) names and names not exported from `__init__.py`.
- Additive changes: new optional parameters, new functions, new optional fields.
- Dead-code removal of names grep shows are unexported and unused (dead-code-reviewer's lane).
- Docs-only diffs.

Severity: P1 for 4 and for breaking removals of exported names; P2 for the rest.
