import ast, json, re, tomllib
from conftest import REPO

def _tool_names():
    src = (REPO / "src").glob("*/server.py")
    tree = ast.parse(next(src).read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                target = d.func if isinstance(d, ast.Call) else d
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.append(node.name)
    return names

def test_manifest_tools_match_server():
    manifest = json.loads((REPO / "manifest.json").read_text())
    assert [t["name"] for t in manifest["tools"]] == _tool_names()

def test_versions_and_names_agree():
    py = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    manifest = json.loads((REPO / "manifest.json").read_text())
    server = json.loads((REPO / "server.json").read_text())
    assert py["version"] == manifest["version"] == server["version"]
    pypi = [p for p in server["packages"] if p["registryType"] == "pypi"][0]
    assert pypi["identifier"] == py["name"] and pypi["version"] == py["version"]
    assert manifest["manifest_version"] == "0.4"
    assert server["$schema"] == "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
    assert server["name"].startswith("io.scalably/")

def test_readme_carries_registry_marker_and_no_em_dash():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    server = json.loads((REPO / "server.json").read_text())
    assert re.search(rf"^<!-- mcp-name: {re.escape(server['name'])} -->$", readme, re.M)
    assert "—" not in readme

def test_secrets_marked_sensitive():
    manifest = json.loads((REPO / "manifest.json").read_text())
    for key, cfg in manifest["user_config"].items():
        if any(w in key for w in ("key", "token", "secret", "password")):
            assert cfg["sensitive"] is True, key
