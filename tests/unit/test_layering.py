"""The dependency rule from docs/01-architecture.md: lower layers never import upper ones."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "kestreldb"

# What each layer may import from inside kestreldb. Extended as layers are added.
BASE = {"kestreldb.config", "kestreldb.errors", "kestreldb.faults"}  # leaf modules, no layer
ALLOWED_INTERNAL_IMPORTS = {
    "storage": BASE | {"kestreldb.storage"},
    "index": BASE | {"kestreldb.storage", "kestreldb.index"},
    "recovery": BASE | {"kestreldb.storage", "kestreldb.recovery"},
    "sql": BASE | {"kestreldb.sql"},
    # The catalog uses the CREATE TABLE syntax node as its input description.
    "catalog": BASE
    | {"kestreldb.storage", "kestreldb.index", "kestreldb.sql", "kestreldb.catalog"},
    "transactions": BASE | {"kestreldb.transactions"},
    "planner": BASE
    | {
        "kestreldb.storage",
        "kestreldb.index",
        "kestreldb.catalog",
        "kestreldb.sql",
        "kestreldb.execution",
        "kestreldb.planner",
    },
    "execution": BASE
    | {
        "kestreldb.storage",
        "kestreldb.index",
        "kestreldb.catalog",
        "kestreldb.sql",
        "kestreldb.execution",
    },
}
# The planner builds trees of operators, so it sits above the operator and
# expression modules. Only the statement executor, which drives the planner,
# may reach back up into it.
EXTRA_ALLOWED = {"execution/executor.py": {"kestreldb.planner"}}


def internal_imports(source_file: Path) -> set[str]:
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "kestreldb":
                found.update(f"kestreldb.{alias.name}" for alias in node.names)
            else:
                found.add(node.module)
    return {name for name in found if name == "kestreldb" or name.startswith("kestreldb.")}


def test_layers_only_import_what_they_are_allowed_to() -> None:
    violations: list[str] = []
    for layer, allowed in ALLOWED_INTERNAL_IMPORTS.items():
        files = sorted((PACKAGE_ROOT / layer).rglob("*.py"))
        assert files, f"layer {layer!r} has no source files"
        for source_file in files:
            relative = source_file.relative_to(PACKAGE_ROOT).as_posix()
            permitted = allowed | EXTRA_ALLOWED.get(relative, set())
            for name in sorted(internal_imports(source_file)):
                if not any(name == prefix or name.startswith(prefix + ".") for prefix in permitted):
                    violations.append(f"{source_file.relative_to(PACKAGE_ROOT)} imports {name}")

    assert violations == []


def test_the_checker_itself_catches_a_violation(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from kestreldb.sql import parser\nfrom kestreldb import cli\nimport os\n", encoding="utf-8"
    )

    assert internal_imports(bad) == {"kestreldb.sql", "kestreldb.cli"}
