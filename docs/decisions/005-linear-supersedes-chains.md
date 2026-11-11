# 005: Linear supersedes chains

Date: 2026-11-11

## Context

`outage_event` is append-only. A revision is a new row whose `supersedes` points at the row it
replaces, and current rows are those no later row supersedes. Nothing stopped two rows from
superseding the same row. That fork leaves one event with two current rows, and its capacity is
counted twice. It also blurs which rows a restart's `ends_event` covers. See the Restarts and
Revisions sections of docs/definitions.md.

## Options

- Allow forks, and apply a closure to every row descending from the row a restart points at.
  Closure then works, but one event still shows two current rows, and every as-of query has to
  pick between them.
- Forbid forks. A new row may supersede only a row that is current when it is written. A second
  row that supersedes the same row goes to review. The cost is that a revision built from a stale
  row is held for review, not written.

## Decision

Forbid forks. Each chain is a single line, with at most one current row at any T. The as-of rule
and the "current rows" rule stay as written, with no tie-breaking between branches. The database
holds it with a unique constraint on `supersedes`, so code cannot get it wrong.

## Consequences

As-of queries and closure checks stay simple. An event shows once, and a refined products desk
sees one capacity figure for it. Two sources revising the same row at once means one of them goes
to review and someone has to rebase it on the current row by hand. That extra review work is the
cost. The write path must check that the superseded row is current inside the insert
transaction. To undo, drop the unique constraint and define a rule for choosing between branches.
