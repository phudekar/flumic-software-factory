"""Extract literal route declarations and included-router prefixes without imports."""
import ast

METHODS = {"get", "post", "put", "patch", "delete"}


def route_nodes(tree):
    prefixes = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "include_router" and node.args and isinstance(node.args[0], ast.Name):
            prefix = next((item.value for item in node.keywords if item.arg == "prefix"), None)
            if isinstance(prefix, ast.Constant) and isinstance(prefix.value, str):
                prefixes.setdefault(node.args[0].id, []).append(prefix.value)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr not in METHODS or not decorator.args:
                continue
            value = decorator.args[0]
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            receiver = decorator.func.value
            owner = receiver.id if isinstance(receiver, ast.Name) else ""
            for prefix in prefixes.get(owner, [""]):
                path = prefix + value.value
                if path.startswith("/api/"):
                    yield decorator.func.attr.upper(), path, node, decorator
