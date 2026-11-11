# 002: Gitleaks release binary in CI

Date: 2026-11-11

## Context

CI must scan the full git history for secrets on every push. The repo is public, so its CI logs
are public too, and a leaked value in history is permanent. Gitleaks does the scan. The choice is
how CI installs it.

## Options

- `gitleaks/gitleaks-action`. Easy to add. It needs a paid licence key for repos owned by an
  organisation, so moving this repo to one would break CI. It also wants wider token access to
  comment on pull requests.
- The gitleaks release binary. MIT licensed and needs only a read token. The cost is a few lines of
  shell to download, verify and unpack it, and a manual bump when a new release comes out.

## Decision

We download the release binary in its own CI job. The version and its SHA-256 checksum are fixed
in the workflow. The job checks the download with `sha256sum --check` and fails on a mismatch. It
then runs `gitleaks git` over all refs with `--redact`, so no secret value reaches the public log.
The workflow keeps a read-only token and references no secrets.

## Consequences

CI does not depend on a licence key or on where the repo lives. A tampered or swapped download
fails the job instead of running. The token stays read-only.

We lose pull request comments from the action. Findings show in the job log only. The pinned
version does not update itself, so bumping it means a new version and checksum in one commit. To
undo this, replace the job steps with the action and add the licence key if the repo needs one.
