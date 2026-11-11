"""The shape of a pre-filter term matcher. No project imports, so any module can use it."""

from collections.abc import Callable, Sequence

TermMatcher = Callable[[str], Sequence[str]]
