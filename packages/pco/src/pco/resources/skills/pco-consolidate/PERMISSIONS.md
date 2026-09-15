# Permissions

PCO verification, canonical search and history, proposal status, proposal
scaffolding, source inspection, and proposal validation are read-only. They need no
write permission and cannot establish approval for a later write.

## Boundary 1: proposal authoring

Before asking the Host to Edit proposal files, explain the complete intended
proposal before Edit. Request one-time write permission only for the disclosed,
reviewable proposal diff. Author all initial manifest entries, business Markdown,
and input artifacts through the Host Edit surface.

If Edit permission is denied, make no proposal change and stop. Do not try an
alternate filesystem or command path. The denial leaves canonical memory unchanged.

## Boundary 2: canonical submission

After validation and user review, request the Host's separate permission for
exactly `pco proposal commit <review_id>`. Permission to author or revise proposal
files does not authorize this submission. Validation is advisory and read-only; it
does not prove that the Host or user approved a commit.

If commit permission is denied, stop. The proposal remains editable, and denial
leaves canonical memory unchanged. Resume it later by verifying the workspace,
checking proposal status, and inspecting the proposal again.

Neither boundary permits a standing grant. Permanent workspace write permission is unsupported.
