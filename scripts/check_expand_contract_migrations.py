import argparse
import ast
import json
from pathlib import Path


ALLOWED_OPERATIONS = {"add_column", "create_index", "create_table", "execute"}


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


def static_string(node, constants):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = static_string(node.left, constants)
        right = static_string(node.right, constants)
        return left + right if left is not None and right is not None else None
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        template = static_string(node.func.value, constants)
        if template is None or node.args:
            return None
        values = {}
        for keyword in node.keywords:
            if keyword.arg is None:
                return None
            value = static_string(keyword.value, constants)
            if value is None:
                return None
            values[keyword.arg] = value
        try:
            return template.format(**values)
        except (KeyError, ValueError):
            return None
    return None


def validate_execute(call, path, violations, constants):
    if not call.args:
        violations.append(f"{path}:{call.lineno}: execute cannot be statically validated")
        return
    statement = call.args[0]
    if (
        isinstance(statement, ast.Call)
        and isinstance(statement.func, ast.Attribute)
        and statement.func.attr == "text"
        and statement.args
    ):
        statement = statement.args[0]
    statement_value = static_string(statement, constants)
    if statement_value is None:
        violations.append(f"{path}:{call.lineno}: execute must use static SQL")
        return
    sql = " ".join(statement_value.strip().rstrip(";").split()).upper()
    is_security_expansion = (
        sql.startswith("CREATE EXTENSION IF NOT EXISTS ")
        or sql.startswith("CREATE EXTENSION ")
        or sql.startswith("CREATE POLICY ")
        or sql.startswith("CREATE FUNCTION ")
        or sql.startswith("CREATE OR REPLACE FUNCTION ")
        or sql.startswith("ALTER POLICY ")
        or sql.startswith("GRANT ")
        or sql.startswith("REVOKE ")
        or (
            sql.startswith("ALTER TABLE ")
            and (
                sql.endswith(" ENABLE ROW LEVEL SECURITY")
                or sql.endswith(" FORCE ROW LEVEL SECURITY")
            )
        )
    )
    if (
        not sql.startswith(("INSERT INTO ", "UPDATE "))
        and not is_security_expansion
    ) or ";" in sql:
        violations.append(
            f"{path}:{call.lineno}: execute only permits one backfill or additive security statement"
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
    constants = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            value = static_string(node.value, constants)
            if value is not None:
                constants[target.id] = value
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
        elif operation == "execute":
            validate_execute(node, path, violations, constants)
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
