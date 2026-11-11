# 001: Python dependency tooling

Date: 2026-11-11

## Context

The project needs one way to declare, lock and install Python dependencies. Development happens on
Windows and CI runs on Linux, so the lock must be valid on both. The VMs are small, with 512 MB and
1 GB of memory, and must never resolve dependencies themselves.

## Options

- pip-tools. Writes one lock per platform. We would need a Windows lock and a Linux lock, or one
  lock that only one platform can trust.
- Poetry. Writes a cross-platform lock, but runs its resolver in Python and is the heavier tool for
  the same result.
- uv. One static binary. Writes a single universal `uv.lock` with hashes. The main cost is that it
  is young and has one main vendor.

## Decision

We use uv with one universal `uv.lock`, committed to the repo. Hatchling is the build backend,
because it builds a src layout package from one setting. Every install and run uses `--locked`, so
a lock that is out of step with `pyproject.toml` fails the build.

uv runs on the laptop, in CI and later in an image build stage. It never runs on a VM and adds
nothing to their memory.

## Consequences

One lock file serves Windows and Linux, and the same commands run in both places. Developers need
uv on PATH. The uv version is pinned in CI and set as the lower bound in `pyproject.toml`, and
bumping it is a manual change.

The way out is cheap. `pyproject.toml` uses standard tables only. `uv export` writes a hashed
requirements file that plain pip can install, so we could drop uv without touching the package.
