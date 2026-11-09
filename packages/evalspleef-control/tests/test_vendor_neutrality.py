"""Architecture guard: vendor adapter names never decide behaviour in spi, core, control or gateway.

AGENTS.md rule 1 keeps vendor differences in adapters. The guard reads the sources as
Python syntax trees and flags every string literal and identifier equal to a vendor
adapter name; docstrings and comments may still name vendors as examples.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGES = Path(__file__).resolve().parents[2]
NEUTRAL_SOURCES = (
    "evalspleef-spi/src",
    "evalspleef-core/src",
    "evalspleef-control/src",
    "evalspleef-gateway/src",
)
VENDOR_NAMES = frozenset({"novita", "e2b", "anthropic", "deepseek", "beam"})

#: (source path under packages/, name) pairs that stay until their contract changes.
EXEMPT = {
    # The client-protocol flavor of the Anthropic Messages wire protocol the gateway
    # serves and converts (WireProtocol "anthropic_messages", FR-MOD-07); not an adapter.
    ("evalspleef-gateway/src/evalspleef/gateway/data_app.py", "anthropic"),
    # Source tag of the Novita bill import that PRD FR-OPS-04 / AC-10 specify in the
    # ledger; moving it behind a provider billing interface is an open contract question.
    ("evalspleef-control/src/evalspleef/control/ledger/ledger.py", "novita"),
}


def _docstrings(tree: ast.AST) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, owners)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _names(node: ast.AST) -> list[str]:
    """The identifiers ``node`` introduces or refers to."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [node.attr]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.keyword) and node.arg is not None:
        return [node.arg]
    if isinstance(node, ast.alias):
        return [part for name in (node.name, node.asname) if name for part in name.split(".")]
    return []


def vendor_names_in(source: str) -> set[tuple[str, int]]:
    """(vendor name, line) for each string literal or identifier equal to a vendor name."""
    tree = ast.parse(source)
    docstrings = _docstrings(tree)
    hits: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        candidates = _names(node)
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            candidates.append(node.value)
        for candidate in candidates:
            if candidate.lower() in VENDOR_NAMES:
                hits.add((candidate.lower(), getattr(node, "lineno", 0)))
    return hits


def test_the_guard_sees_literals_and_identifiers_but_not_docstrings() -> None:
    source = '''
"""Module docstring naming novita as an example."""
KINDS = {"e2b": 1}
def run(beam: int) -> None:
    """Docstring mentioning deepseek."""
    call(anthropic=beam)  # a comment about novita
'''
    assert {name for name, _ in vendor_names_in(source)} == {"e2b", "beam", "anthropic"}


def test_neutral_packages_carry_no_vendor_names() -> None:
    found: dict[tuple[str, str], list[int]] = {}
    scanned = 0
    for root in NEUTRAL_SOURCES:
        for path in sorted((PACKAGES / root).rglob("*.py")):
            scanned += 1
            relative = path.relative_to(PACKAGES).as_posix()
            for name, line in vendor_names_in(path.read_text(encoding="utf-8")):
                found.setdefault((relative, name), []).append(line)
    assert scanned > 100, "the guard did not find the package sources"
    leaks = {key: sorted(lines) for key, lines in found.items() if key not in EXEMPT}
    assert leaks == {}, "vendor names belong in adapters (AGENTS.md rule 1)"
    stale = EXEMPT - found.keys()
    assert stale == set(), "drop exemptions that no longer match anything"
