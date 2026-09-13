import argparse
import ast
import json
from pathlib import Path


ALLOWED_OPERATIONS = {"add_column", "create_index", "create_table"}


def operation_name(call):
    function = call.func
    if not isinstance(function, ast.Attribute):
        return None
    if not isinstance(function.value, ast.Name) or function.value.id != "op":
        return None
    return function.attr


def is_none(node):
    return isinstance(node, ast.Constant) and node.value is None


def validate_added_column(call, path, violations):
    if len(call.args) < 2 or not isinstance(call.args[1], ast.Call):
        violations.append(f"{path}:{call.lineno}: add_column cannot be statically validated")
        return
    column = call.args[1]
    function = column.func
    if not isinstance(function, ast.Attribute) or function.attr != "Column":
        violations.append(f"{path}:{call.lineno}: add_column must use sa.Column")
        return
    keywords = {keyword.arg: keyword.value for keyword in column.keywords if keyword.arg}
    nullable = keywords.get("nullable")
    if isinstance(nullable, ast.Constant) and nullable.value is False:
        default = keywords.get("server_default")
        if default is None or is_none(default):
            violations.append(
                f"{path}:{call.lineno}: non-null added columns require server_default"
            )


def check_migration(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    upgrades = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "upgrade"
    ]
    if len(upgrades) != 1:
        return [f"{path}: expected exactly one upgrade function"]
    violations = []
    for node in ast.walk(upgrades[0]):
        if not isinstance(node, ast.Call):
            continue
        operation = operation_name(node)
        if operation is None:
            continue
        if operation not in ALLOWED_OPERATIONS:
            violations.append(
                f"{path}:{node.lineno}: op.{operation} is not expand-contract compatible"
            )
            continue
        if operation == "add_column":
            validate_added_column(node, path, violations)
    return violations


def check_directory(directory):
    paths = sorted(Path(directory).glob("*.py"))
    if not paths:
        return [f"{directory}: no migration files found"]
    violations = []
    for path in paths:
        violations.extend(check_migration(path))
    return violations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", nargs="?", default="migrations/versions")
    args = parser.parse_args()
    violations = check_directory(args.directory)
    if violations:
        for violation in violations:
            print(violation)
        raise SystemExit(1)
    print(json.dumps({"status": "passed", "policy": "expand-contract"}, sort_keys=True))


if __name__ == "__main__":
    main()
