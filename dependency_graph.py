# dependency_graph.py
"""
Builds a fan-in dependency graph for a Python codebase: for every .py file,
how many OTHER files in the same project import it.

Fan-in feeds sonar_agent.py's confidence scoring as a "blast radius" signal -
a file many others depend on is riskier to auto-fix than an isolated one,
independent of how safe the specific rule is (see
Approach_confidence_score.txt for the full write-up).

Limitations (documented, not hidden):
  - Python-only: parses `import` / `from ... import` via ast. A different
    language needs a different parser (e.g. jdeps for Java, madge for
    JavaScript) - the CONCEPT transfers, this specific script does not.
  - File-level, not function-level. A file used by 10 others still scores as
    "risky" even if the specific buggy function inside it is dead code that
    nothing actually calls.
  - Only sees dependencies within --source-root. It cannot detect external
    callers (another repository, a separately deployed service).
"""
import argparse
import ast
import json
from pathlib import Path


def find_python_files(source_root):
    return [p for p in source_root.rglob("*.py") if p.is_file()]


def module_name_for(file_path, source_root):
    rel = file_path.relative_to(source_root).with_suffix("")
    parts = rel.parts
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def extract_imported_modules(file_path):
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def resolve_to_project_file(module_name, module_to_file):
    """Tries the full dotted path, then progressively shorter prefixes, so
    `from pkg.sub import thing` still resolves against a known `pkg.sub`
    module even though `thing` itself isn't a file."""
    parts = module_name.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in module_to_file:
            return module_to_file[candidate]
    return None


def build_graph(source_root):
    source_root = Path(source_root).resolve()
    files = find_python_files(source_root)

    module_to_file = {module_name_for(f, source_root): f for f in files}

    def rel(f):
        return str(f.relative_to(source_root)).replace("\\", "/")

    graph = {rel(f): {"fan_in": 0, "imported_by": []} for f in files}

    for f in files:
        for module_name in extract_imported_modules(f):
            target = resolve_to_project_file(module_name, module_to_file)
            if target is None or target == f:
                continue  # external library (e.g. pandas, requests), or self-import
            target_key = rel(target)
            graph[target_key]["fan_in"] += 1
            graph[target_key]["imported_by"].append(rel(f))

    return graph


def main():
    parser = argparse.ArgumentParser(description="Build a fan-in dependency graph for a Python codebase")
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output", default="dependency_graph.json")
    args = parser.parse_args()

    graph = build_graph(args.source_root)

    with open(args.output, "w") as f:
        json.dump(graph, f, indent=2)

    print(f"[dependency_graph] {len(graph)} file(s) analyzed -> {args.output}")


if __name__ == "__main__":
    main()
