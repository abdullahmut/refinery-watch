"""robots.txt rules as RFC 9309 defines them: longest match wins, * and $ work, Allow wins a tie.

The standard library parser takes the first matching rule and has no wildcards, so it is not used.
"""

import string
from dataclasses import dataclass
from typing import Final

MAX_BYTES: Final = 500 * 1024
_UNRESERVED: Final = frozenset(string.ascii_letters + string.digits + "-._~")


@dataclass(frozen=True, slots=True)
class Rule:
    allow: bool
    pattern: str


@dataclass(frozen=True, slots=True)
class RobotsRules:
    rules: tuple[Rule, ...]
    crawl_delay: float | None

    def allows(self, path_and_query: str) -> bool:
        target = _normalise(path_and_query or "/")
        best: Rule | None = None
        for rule in self.rules:
            if not _matches(rule.pattern, target):
                continue
            longer = best is None or len(rule.pattern) > len(best.pattern)
            tie = best is not None and len(rule.pattern) == len(best.pattern) and rule.allow
            if longer or tie:
                best = rule
        return best is None or best.allow


ALLOW_ALL: Final = RobotsRules((), None)


def _normalise(text: str) -> str:
    """RFC 9309 form: unreserved octets decoded, other octets and non-ASCII as upper-case %XX."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        escaped = text[i + 1 : i + 3]
        if ch == "%" and len(escaped) == 2 and all(c in string.hexdigits for c in escaped):
            byte = int(escaped, 16)
            out.append(chr(byte) if chr(byte) in _UNRESERVED else f"%{byte:02X}")
            i += 3
            continue
        if ord(ch) > 127:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8", errors="replace"))
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _matches(pattern: str, target: str) -> bool:
    """Prefix match with * as any run and a trailing $ as the end of the target."""
    anchored = pattern.endswith("$")
    parts = (pattern[:-1] if anchored else pattern).split("*")
    if not target.startswith(parts[0]):
        return False
    pos = len(parts[0])
    if len(parts) == 1:
        return pos == len(target) if anchored else True
    for part in parts[1:-1]:
        found = target.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    last = parts[-1]
    if anchored:
        return len(target) - len(last) >= pos and target.endswith(last)
    return target.find(last, pos) >= 0


def parse_robots(text: str, token: str) -> RobotsRules:
    """Rules for one product token. The group naming the token wins over the * group."""
    groups: list[tuple[list[str], list[Rule], list[float]]] = []
    in_agents = False
    for raw in text[:MAX_BYTES].lstrip("\ufeff").splitlines():
        line = raw.split("#", 1)[0].strip()
        field, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = field.strip().lower(), value.strip()
        if key == "user-agent":
            if not in_agents:
                groups.append(([], [], []))
            groups[-1][0].append(value.lower())
            in_agents = True
            continue
        in_agents = False
        if not groups:
            continue
        if key in ("allow", "disallow") and value:
            groups[-1][1].append(Rule(key == "allow", _normalise(value)))
        elif key == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                continue
            if not delay < 0:
                groups[-1][2].append(delay)
    return _select(groups, token.lower())


def _select(groups: list[tuple[list[str], list[Rule], list[float]]], token: str) -> RobotsRules:
    for wanted in (token, "*"):
        picked = [g for g in groups if wanted in g[0]]
        if picked:
            rules = tuple(rule for g in picked for rule in g[1])
            delays = [d for g in picked for d in g[2]]
            return RobotsRules(rules, delays[0] if delays else None)
    return ALLOW_ALL
