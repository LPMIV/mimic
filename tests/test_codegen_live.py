"""Opt-in authenticated smoke test using only reviewed synthetic examples.

Run MIMIC_LIVE_CODEX=1 python -m pytest tests/test_codegen_live.py -q after
signing in with Codex CLI. This consumes account usage. No captured API or
generated client is invoked. Normal offline runs skip this test.
"""
import ast
import json
import os

import pytest

from mimic import cli


@pytest.mark.skipif(os.environ.get("MIMIC_LIVE_CODEX") != "1",
                    reason="live Codex account test requires MIMIC_LIVE_CODEX=1")
def test_authenticated_synthetic_generation(tmp_path, capsys):
    capture = tmp_path / "synthetic.har"
    capture.write_text(json.dumps({"log": {"entries": [{
        "request": {
            "method": "GET",
            "url": "https://api.example.com/v1/widgets?limit=2&access_token=SYNTHETIC-DO-NOT-USE",
            "headers": [{"name": "Authorization", "value": "Bearer SYNTHETIC-DO-NOT-USE"}],
        },
        "response": {"status": 200, "content": {"mimeType": "application/json", "text": json.dumps({
            "widgets": [{"id": "example-widget-1", "title": "Synthetic example"}],
            "next_cursor": None,
        })}},
    }]}}), encoding="utf-8")
    cli.main(["gen", "api.example.com", "--generator", "codex", "--har", str(capture), "--prompt-only"])
    draft = capsys.readouterr().out
    assert "SYNTHETIC-DO-NOT-USE" not in draft
    assert "Authorization" not in draft
    # This entire fixture is intentionally synthetic and reviewed in source.
    # Exercise the edit-and-submit flow without handling any real traffic.
    reviewed = draft.replace("Below is real captured HTTP traffic", "Below is synthetic HTTP traffic")
    reviewed = reviewed.replace(
        "recorded by a proxy while the user exercised the app with their own account.",
        "created only for a generation test, with no real user or account data.",
    ).replace("query: limit=2&access_token=%5BREDACTED%5D", "query: limit=2")
    prompt = tmp_path / "reviewed.txt"
    prompt.write_text(reviewed, encoding="utf-8")
    output = tmp_path / "example_client.py"
    cli.main(["gen", "api.example.com", "--generator", "codex", "--reviewed-prompt", str(prompt), "-o", str(output)])
    source = output.read_text(encoding="utf-8")
    tree = ast.parse(source)
    compile(tree, "<generated client>", "exec", dont_inherit=True)
    modules = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.Import)
               for alias in node.names if alias.name == "mimic"}
    app_names = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
                 and node.module == "mimic" for alias in node.names if alias.name == "App"}
    clients = [node for node in tree.body if isinstance(node, ast.ClassDef) and any(
        (isinstance(base, ast.Name) and base.id in app_names) or
        (isinstance(base, ast.Attribute) and base.attr == "App" and
         isinstance(base.value, ast.Name) and base.value.id in modules)
        for base in node.bases
    )]
    assert clients, "expected a mimic.App subclass"
    assert any(isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
               for client in clients for node in client.body), "expected a public client method"
    assert "SYNTHETIC-DO-NOT-USE" not in source
    assert "api.example.com" in source
