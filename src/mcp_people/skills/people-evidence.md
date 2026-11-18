---
name: people-evidence
description: Record the facts behind a claim. A signal is a fact about them attested by a source; a proof point is a claim about our own work attested by a person. Read what is recorded before researching again, and never cite what is not attested.
metadata:
  nimblebrain:
    loading-strategy: dynamic
    tool-affinity:
      - list_signal_types
      - list_signals
      - record_signal
      - withdraw_signal
      - list_proof_points
      - record_proof_point
      - verify_proof_point
---

# Evidence

Prose is never evidence. A fact you would put in a message is a record, and the record
carries what makes it checkable. Two kinds, because they have different attestors:

| | A **signal** | A **proof point** |
|---|---|---|
| About | them: an account or a person | our own work |
| Attested by | a source you read (`source_url`, `evidence_quote` verbatim) | a person (`attested_by`) |
| Citable | once recorded | only once `verified` |

## Before you research

Read `list_signals(organization_id=...)` first: the work may already be done. An empty
result for an account already researched means nothing qualified, which is an answer.
`list_signal_types` lists the types this workspace records; each type's description
is what decides whether a fact is one.

## Recording

- Record a signal only from a source you read. Finding nothing worth recording is a good
  outcome: record nothing rather than a weak signal.
- A signal carries no grade. Judge it against your purpose when you use it.
- Say whom each signal is evidence for (`evidence_for`): `"workspace"` when it says the
  organization might buy from this workspace, or the id of the organization you researched
  it for. Fix a wrong one with `update_signal(remove_evidence_for=...)`; the fact itself stays.
- Never cite a returned signal whose quote does not support your point.
- A number invented to workshop copy is a proof point marked `fabricated-placeholder`, never
  an unmarked one: the mark is what stops it being read as evidence later.

## Correcting

A wrong signal is withdrawn with a reason, never re-recorded over: the row stays so anything
that cited it can still be traced. A proof point changes state only through
`verify_proof_point`, which names who made the call.
