# Complete proposal format

A proposal is complete when its manifest describes the entire closed file set and
every listed file is ready for review without placeholders.

## Manifest

Start from the JSON printed by `pco proposal scaffold` and preserve its generated
`review_id`, `base_commit`, `workspace_version`, and `profile_version`.

- `input_material` lists every source used. Give each entry a unique stable
  `material_id`, its `local_file` locator or `proposal_artifact` path, its fidelity,
  and the expected content hash required for commit.
- `files` lists every business Markdown file once, with its normalized relative
  path and intended operation.
- Every regular file under `inputs/` appears once as proposal input material. Every
  business Markdown file appears once in `files`. Do not leave unlisted files.

## Reviewable content

Author complete, schema-valid Markdown for every proposed revision. Make evidence
links and source material explicit, distinguish observations from interpretations,
preserve contradictions and uncertainty, and avoid unsupported certainty. Include
`META_MEMORY.md` or `SESSION_HANDOFF.md` only when it is part of the disclosed
intent.

Before the Edit, summarize:

1. each object added, revised, rejected, or left unchanged;
2. the evidence and input material supporting each operation;
3. uncertainty, conflicts, and alternatives retained; and
4. every manifest, business, and input-artifact path to be written.

Use the Host Edit surface to author that whole disclosed set. Then run
`pco proposal validate <review_id>`. If validation reports observed hashes or other
errors, explain the required revisions, update the proposal through another Host
Edit, and validate again. Validation reads and checks the proposal; it does not
approve or commit it.
