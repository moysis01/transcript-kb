"""Imports may only point down the layers, so the structure survives future edits:

    config, prompts  <-  core  <-  pipeline  <-  services  <-  app  <-  cli
"""
import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "tkb"
RANK = {"config": 0, "prompts": 0, "core": 1, "pipeline": 2, "services": 3, "app": 4, "cli": 5}


def layer(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else "cli"


def test_imports_only_point_down():
    bad = []
    for f in PKG.rglob("*.py"):
        mod = "tkb." + ".".join(f.relative_to(PKG).with_suffix("").parts).removesuffix(".__init__")
        if mod == "tkb.__init__" or mod == "tkb":
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            targets = []
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tkb"):
                targets = [node.module] if node.module != "tkb" else [f"tkb.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                targets = [a.name for a in node.names if a.name.startswith("tkb.")]
            for t in targets:
                if RANK[layer(t)] > RANK[layer(mod)]:
                    bad.append(f"{mod} imports {t}")
    assert not bad, "upward imports:\n" + "\n".join(bad)


def test_no_relative_or_private_cross_module_imports():
    bad = []
    for f in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    bad.append(f"{f.name}: relative import")
                elif node.module and node.module.startswith("tkb"):
                    bad += [f"{f.name}: imports private {a.name}" for a in node.names if a.name.startswith("_")]
    assert not bad, "\n".join(bad)
