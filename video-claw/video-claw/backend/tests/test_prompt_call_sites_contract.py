"""C3：调用点传参完整性——模板需要的占位符必须由真实 format 调用提供。

只做静态 AST 核对，不调模型：
- 直接链式 `load_prompt(...).format(...)`；
- 先赋值 `tpl = load_prompt(...)` 再 `tpl.format(...)`；
- 语言参数为 `zh if ... else en` 时两种模板都检查。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if not (BACKEND / "prompts").exists():
    BACKEND = Path("/app")
AGENTS = BACKEND / "core"
PROMPTS = BACKEND / "prompts"
_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def _langs(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return sorted(set(_langs(node.body) + _langs(node.orelse)))
    raise ValueError("unsupported lang expression")


def _load_target(node: ast.AST) -> list[Path] | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not (isinstance(func, ast.Name) and func.id == "load_prompt"):
        return None
    if len(node.args) < 2:
        return None
    try:
        category = ast.literal_eval(node.args[0])
        name = ast.literal_eval(node.args[1])
        langs = _langs(node.args[2]) if len(node.args) >= 3 else ["zh"]
    except (ValueError, SyntaxError):
        return None
    paths = []
    for lang in langs:
        path = PROMPTS / str(category) / f"{name}_{lang}.txt"
        if path.exists():
            paths.append(path)
    return paths or None


def _kwargs(call: ast.Call) -> tuple[set[str], bool]:
    names = {kw.arg for kw in call.keywords if kw.arg}
    has_kwargs = any(kw.arg is None for kw in call.keywords)
    return names, has_kwargs


def _audit_nodes(nodes, label: str):
    assigned: dict[str, list[Path]] = {}
    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            targets = _load_target(node.value)
            if targets:
                assigned[node.targets[0].id] = targets
    results = []
    for node in nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"):
            continue
        targets = _load_target(node.func.value)
        if targets is None and isinstance(node.func.value, ast.Name):
            targets = assigned.get(node.func.value.id)
        if not targets:
            continue
        names, has_kwargs = _kwargs(node)
        results.append((f"{label}:{node.lineno}", targets, names, has_kwargs))
    return results


def _audit_file(path: Path) -> tuple[list[tuple[str, list[Path], set[str], bool]], int]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    results = _audit_nodes(tree.body, path.name)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes = list(ast.walk(node))
            results.extend(_audit_nodes(nodes, f"{path.name}:{node.name}"))
    return results, len(results)


def test_every_prompt_call_site_supplies_required_placeholders():
    failures = []
    skipped = []
    checked = 0
    for path in sorted(AGENTS.rglob("*.py")):
        results, count = _audit_file(path)
        checked += count
        for where, targets, names, has_kwargs in results:
            if has_kwargs:
                skipped.append(where)
                continue
            for template in targets:
                required = set(_PLACEHOLDER.findall(template.read_text(encoding="utf-8")))
                missing = sorted(required - names)
                if missing:
                    failures.append(f"{where} -> {template.name} 缺 {missing}")
    assert checked >= 8, f"调用点扫描数量异常：{checked}"
    assert failures == [], "调用点漏传模板变量：\n" + "\n".join(failures)
    assert len(skipped) <= 1, f"过多调用点使用 **kwargs 无法静态核对：{skipped}"
