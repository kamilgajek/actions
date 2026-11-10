#!/usr/bin/env python3
"""Fail when a library is declared with a version below the minimum in policy.txt.

Lock files (uv, pdm, poetry, Pipfile) are checked by resolved version. Every other
text file (requirements, pyproject, Dockerfile, notebooks, YAML, scripts) is searched
for declarations such as `pkg>=1.0` or `pkg-1.0-py3-none-any.whl`; TOML files are also read
structurally for Poetry / Pipfile tables such as `pkg = "^1.0"`.
Declarations without a version are ignored: the lock file decides what is installed.
"""

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

SKIP_DIRS = {".git", ".venv", "venv", ".tox", "__pycache__", "node_modules", "dist", "build"}
LOCK_FILES = {"uv.lock", "pdm.lock", "poetry.lock", "Pipfile.lock"}
TEXT_SUFFIXES = {".txt", ".in", ".toml", ".cfg", ".ini", ".py", ".sh", ".yml", ".yaml", ".ipynb"}
TEXT_NAMES = {"Pipfile", "Makefile"}


def load_policy(path: Path) -> dict[str, Version]:
    """Read `package>=minimum-version` lines."""
    rules = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.partition("#")[0].strip()
        if line:
            match = re.fullmatch(r"([\w.-]+)\s*>=\s*(\S+)", line)
            if not match:
                raise ValueError(f"Policy line must look like package>=version: {line}")
            rules[canonicalize_name(match[1])] = Version(match[2])
    if not rules:
        raise ValueError("Policy is empty")
    return rules


def build_patterns(rules: dict[str, Version]) -> tuple[re.Pattern, re.Pattern]:
    names = "|".join(re.escape(name).replace(r"\-", "[-_.]+") for name in rules)
    start = rf"(?<![\w.-])(?P<name>{names})"
    operator = r"(?:===|==|~=|!=|>=|<=|>|<|\^|~)"
    # pkg>=1,<2 | pkg[extra]==1
    declared = re.compile(
        rf"{start}(?:\[[^\]]*\])?[ \t]*"
        rf"(?P<spec>(?:{operator}[ \t]*[\w.*+!-]+[ \t]*,?[ \t]*)+)",
        re.IGNORECASE,
    )
    # pkg-1.0-py3-none-any.whl | pkg-1.0.tar.gz
    archive = re.compile(
        rf"{start}-(?P<version>\d[\w.!+]*?)(?=-[\w.-]+\.whl|\.tar\.gz|\.zip)", re.IGNORECASE
    )
    return declared, archive


def meets_minimum(spec: str, minimum: Version) -> bool:
    """True if the specifier itself guarantees a version >= minimum."""
    spec = re.sub(r"\^|~(?!=)", ">=", spec.strip().rstrip(","))  # poetry ^1.2 / ~1.2
    return any(
        item.operator in {">=", ">", "==", "===", "~="}
        and Version(item.version.removesuffix(".*")) >= minimum
        for item in SpecifierSet(spec)
    )


def verify(name: str, spec: str, rules: dict, where: str) -> list[str]:
    try:
        if meets_minimum(spec, rules[name]):
            return []
        return [f"{where}: {name}{spec.strip()} must be >={rules[name]}"]
    except (InvalidVersion, ValueError) as exc:
        return [f"{where}: cannot verify {name}{spec}: {exc}"]


def check_declarations(
    text: str, rules: dict, patterns: tuple, label: str, archives_only: bool = False
) -> list[str]:
    errors = []
    pairs = ((patterns[0], "spec"), (patterns[1], "version"))
    for pattern, group in pairs[1:] if archives_only else pairs:
        for match in pattern.finditer(text):
            spec = match[group] if group == "spec" else f"=={match[group]}"
            line = text.count("\n", 0, match.start()) + 1
            errors += verify(canonicalize_name(match["name"]), spec, rules, f"{label}:{line}")
    return errors


def check_toml_tables(data: object, rules: dict, label: str) -> list[str]:
    """Poetry / Pipfile style: pkg = "^1.0", pkg = {version = "1.0"}, [..dependencies.pkg]."""
    errors = []
    if isinstance(data, dict):
        for key, value in data.items():
            name = canonicalize_name(key)
            spec = value.get("version") if isinstance(value, dict) else value
            if name in rules and isinstance(spec, str) and spec not in {"", "*"}:
                errors += verify(name, "==" + spec if spec[0].isdigit() else spec, rules, label)
            errors += check_toml_tables(value, rules, label)
    elif isinstance(data, list):
        for item in data:
            errors += check_toml_tables(item, rules, label)
    return errors


def check_lock(path: Path, rules: dict[str, Version], label: str) -> list[str]:
    if path.name == "Pipfile.lock":
        data = json.loads(path.read_text(encoding="utf-8"))
        packages = [
            (name, info.get("version", "").removeprefix("=="))
            for section in ("default", "develop")
            for name, info in data.get(section, {}).items()
        ]
    else:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        packages = [(item["name"], item.get("version", "")) for item in data.get("package", [])]
    errors = []
    for name, version in packages:
        name = canonicalize_name(name)
        if name in rules and (not version or Version(version) < rules[name]):
            errors.append(f"{label}: locked {name}=={version or '?'} must be >={rules[name]}")
    return errors


def read_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".ipynb":  # only code cells, not outputs
        cells = json.loads(text)["cells"]
        return "\n".join("".join(c["source"]) for c in cells if c["cell_type"] == "code")
    return text


def scan(root: Path, rules: dict[str, Version]) -> list[str]:
    patterns = build_patterns(rules)
    errors = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(directory) / name
            is_text = (
                path.suffix in TEXT_SUFFIXES or name in TEXT_NAMES or name.startswith("Dockerfile")
            )
            if path.is_symlink() or not (name in LOCK_FILES or is_text):
                continue
            label = path.relative_to(root).as_posix()
            try:
                if name in LOCK_FILES:
                    errors += check_lock(path, rules, label)
                    # sdist / wheel URLs inside the lock must not point at an older release
                    text = path.read_text(encoding="utf-8")
                    errors += check_declarations(text, rules, patterns, label, archives_only=True)
                else:
                    text = read_text(path)
                    errors += check_declarations(text, rules, patterns, label)
                    if path.suffix == ".toml" or name == "Pipfile":
                        errors += check_toml_tables(tomllib.loads(text), rules, label)
            except UnicodeDecodeError:
                continue
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{label}: cannot read dependency file: {exc}")
    return sorted(errors)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Repository to check")
    parser.add_argument("--policy", type=Path, default=Path(__file__).with_name("policy.txt"))
    args = parser.parse_args()
    errors = scan(args.root.resolve(), load_policy(args.policy))
    print("\n".join(errors))
    print(f"Dependency policy: {len(errors)} violation(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
