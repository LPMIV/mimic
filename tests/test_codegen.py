"""Offline generator tests. All traffic, credentials, and model output are synthetic."""
import copy
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock
from urllib.parse import parse_qs

import pytest

from mimic import cli, codegen


@pytest.fixture
def endpoints():
    return [{
        "method": "POST", "path": "/v1/messages", "status": 200,
        "query": "page=1&access_token=fake-query-token&api-key=fake-key&tag=a&tag=b&blank=",
        "request_body": json.dumps({
            "nested": [{"refreshToken": "fake-refresh", "email": "synthetic@example.com"}],
            "password": "fake-password", "text": "Example message", "count": 2,
        }),
        "response_body": '{"result":{"session_id":"fake-session","ok":true}}',
        "headers": {"Authorization": "Bearer fake-header"},
    }]


def test_review_redaction_preserves_structure_and_originals(endpoints):
    original = copy.deepcopy(endpoints)
    prompt = codegen.build_review_prompt("api.example.com", endpoints)
    for private in ("fake-query-token", "fake-key", "fake-refresh", "synthetic@example.com",
                    "fake-password", "fake-session", "fake-header"):
        assert private not in prompt
    assert "Example message" in prompt  # Free text still needs human review.
    assert '"count": 2' in prompt
    assert '"ok": true' in prompt
    assert endpoints == original
    query = next(line[7:] for line in prompt.splitlines() if line.startswith("query: "))
    parsed = parse_qs(query, keep_blank_values=True)
    assert parsed["access_token"] == [codegen.REDACTED]
    assert parsed["tag"] == ["a", "b"]
    assert parsed["blank"] == [""]


@pytest.mark.parametrize("body", ["password=fake&other=example", "plain private text", '{"token":"fake"'])
def test_review_omits_unstructured_or_truncated_bodies(endpoints, body):
    endpoints[0]["request_body"] = body
    assert body not in codegen.build_review_prompt("api.example.com", endpoints)
    assert "body omitted" in codegen.build_review_prompt("api.example.com", endpoints)


def test_original_prompt_unchanged(endpoints):
    prompt = codegen.build_prompt("api.example.com", endpoints)
    assert "fake-query-token" in prompt
    assert "fake-password" in prompt
    assert "fake-header" not in prompt


def fake_codex(monkeypatch, source="class Client: pass\n", returncode=0, write_output=True):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command == ["codex", "exec", "--help"]:
            assert "input" not in kwargs
            return subprocess.CompletedProcess(command, 0, "--ignore-user-config", "")
        if write_output:
            Path(command[command.index("--output-last-message") + 1]).write_text(source, encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode, "NOT THE FINAL RESPONSE", "private diagnostics")

    monkeypatch.setattr(codegen.subprocess, "run", run)
    return calls


def test_codex_requires_reviewed_prompt_before_starting(monkeypatch, endpoints):
    run = Mock()
    monkeypatch.setattr(codegen.subprocess, "run", run)
    with pytest.raises(SystemExit, match="requires a locally reviewed"):
        codegen.generate("api.example.com", endpoints, generator="codex")
    run.assert_not_called()


@pytest.mark.parametrize("model", [None, "example-model"])
def test_codex_argv_stdin_final_output_and_temp_cleanup(monkeypatch, endpoints, model):
    calls = fake_codex(monkeypatch, "```python\nclass Client: pass\n```\n")
    reviewed = "Reviewed synthetic prompt with Unicode: café\r\n"
    result = codegen.generate("api.example.com", endpoints, generator="codex",
                              model=model, reviewed_prompt=reviewed)
    assert result == "class Client: pass\n"
    assert len(calls) == 2
    command, kwargs = calls[1]
    assert command[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert command[-1] == "-"
    assert kwargs["input"] == reviewed
    assert reviewed not in command
    assert kwargs["timeout"] == 300
    assert "shell" not in kwargs
    assert "env" not in kwargs  # Preserve normal HOME/CODEX_HOME login lookup.
    assert "--ignore-user-config" in command and "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--cd") + 1] == kwargs["cwd"]
    assert 'web_search="disabled"' in command
    for feature in ("shell_tool", "apps", "plugins", "hooks", "multi_agent", "memories", "view_image"):
        assert command[command.index(feature) - 1] == "--disable"
    assert not Path(kwargs["cwd"]).exists()
    if model:
        assert command[command.index("--model") + 1] == model
    else:
        assert "--model" not in command and "sonnet" not in command


def test_old_codex_fails_before_upload(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "older exec help", ""))
    monkeypatch.setattr(codegen.subprocess, "run", run)
    with pytest.raises(SystemExit, match="no prompt was sent"):
        codegen.generate("api.example.com", [], generator="codex", reviewed_prompt="example")
    assert run.call_count == 1
    assert "input" not in run.call_args.kwargs


@pytest.mark.parametrize("failure, message", [
    (FileNotFoundError(), "CLI not found"),
    (subprocess.TimeoutExpired("codex", 300), "timed out"),
])
def test_missing_cli_and_timeout(monkeypatch, failure, message):
    monkeypatch.setattr(codegen.subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(SystemExit, match=message):
        codegen.generate("api.example.com", [], generator="codex", reviewed_prompt="example")


@pytest.mark.parametrize("source, message", [("", "empty Python"), ("# comment only", "empty Python"),
                                            ("return 1", "invalid Python"), ("break", "invalid Python"),
                                            ("nonlocal x", "invalid Python"),
                                            ("Here is your client!", "invalid Python")])
def test_invalid_codex_output(monkeypatch, source, message):
    calls = fake_codex(monkeypatch, source)
    with pytest.raises(SystemExit, match=message):
        codegen.generate("api.example.com", [], generator="codex", reviewed_prompt="example")
    assert not Path(calls[-1][1]["cwd"]).exists()


def test_missing_codex_output(monkeypatch):
    fake_codex(monkeypatch, write_output=False)
    with pytest.raises(SystemExit, match="readable final response"):
        codegen.generate("api.example.com", [], generator="codex", reviewed_prompt="example")


def test_codex_failure_does_not_echo_private_logs(monkeypatch):
    fake_codex(monkeypatch, returncode=1)
    with pytest.raises(SystemExit, match="codex failed") as error:
        codegen.generate("api.example.com", [], generator="codex", reviewed_prompt="example")
    assert "private diagnostics" not in str(error.value)


def test_codex_read_only_startup_error_is_not_misreported_as_login(monkeypatch):
    def run(command, **kwargs):
        if "--help" in command:
            return subprocess.CompletedProcess(command, 0, "--ignore-user-config", "")
        return subprocess.CompletedProcess(
            command, 1, "", "private diagnostic data\n"
            "Error: failed to initialize in-process app-server client: Read-only file system (os error 30)",
        )

    monkeypatch.setattr(codegen.subprocess, "run", run)
    with pytest.raises(SystemExit, match="read-only filesystem") as error:
        codegen.generate("api.example.com", [], generator="codex", reviewed_prompt="example")
    assert "private diagnostic data" not in str(error.value)
    assert "codex login status" not in str(error.value)


@pytest.mark.parametrize("generator, model, expected", [
    ("claude", None, ["claude", "-p", "--model", "sonnet"]),
    ("claude", "opus", ["claude", "-p", "--model", "opus"]),
    ("opencode", None, ["opencode", "run"]),
])
def test_existing_generators(monkeypatch, endpoints, generator, model, expected):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "class Client: pass", ""))
    monkeypatch.setattr(codegen.subprocess, "run", run)
    source = codegen.generate("api.example.com", endpoints, generator=generator, model=model)
    assert source == "class Client: pass\n"
    command = run.call_args.args[0]
    assert command[:len(expected)] == expected
    prompt = codegen.build_prompt("api.example.com", endpoints)
    assert (run.call_args.kwargs["input"] if generator == "claude" else command[-1]) == prompt


def test_cli_refuses_unreviewed_codex_without_reading_capture(monkeypatch):
    capture = Mock()
    monkeypatch.setattr(cli, "_flows_from_args", capture)
    with pytest.raises(SystemExit, match="local review before upload"):
        cli.main(["gen", "api.example.com", "--generator", "codex"])
    capture.assert_not_called()


def test_prompt_only_is_offline_and_redacted(monkeypatch, capsys):
    run = Mock()
    monkeypatch.setattr(codegen.subprocess, "run", run)
    fixture = Path(__file__).parent / "fixtures" / "sample.har"
    cli.main(["gen", "api.example.com", "--generator", "codex", "--prompt-only", "--har", str(fixture)])
    output = capsys.readouterr()
    assert "Captured endpoints for api.example.com" in output.out
    assert "LOCAL REVIEW DRAFT" in output.err
    assert "NOT comprehensive" in output.err
    assert "token123" not in output.out
    run.assert_not_called()


def test_reviewed_cli_reads_only_exact_file(monkeypatch, tmp_path):
    prompt = tmp_path / "reviewed.txt"
    prompt.write_bytes("Reviewed synthetic data: café\r\n".encode())
    output = tmp_path / "client.py"
    capture = Mock(side_effect=AssertionError("must not access capture"))
    monkeypatch.setattr(cli, "_flows_from_args", capture)
    calls = fake_codex(monkeypatch)
    cli.main(["gen", "api.example.com", "--generator", "codex", "--reviewed-prompt", str(prompt), "-o", str(output)])
    assert calls[-1][1]["input"] == prompt.read_bytes().decode()
    assert output.read_text() == "class Client: pass\n"
    capture.assert_not_called()


@pytest.mark.parametrize("extra", [["--prompt-only"], ["--har", "capture.har"]])
def test_conflicting_review_flags_rejected(extra):
    with pytest.raises(SystemExit) as error:
        cli.main(["gen", "api.example.com", "--reviewed-prompt", "reviewed.txt"] + extra)
    assert error.value.code == 2


@pytest.mark.parametrize("content", [b"", b" \n", b"\xff"])
def test_unusable_review_file_never_starts_generator(monkeypatch, tmp_path, content):
    path = tmp_path / "review.txt"
    path.write_bytes(content)
    run = Mock()
    monkeypatch.setattr(codegen.subprocess, "run", run)
    with pytest.raises(SystemExit):
        cli.main(["gen", "api.example.com", "--generator", "codex", "--reviewed-prompt", str(path)])
    run.assert_not_called()


def test_failed_generation_keeps_existing_output(monkeypatch, tmp_path):
    prompt = tmp_path / "reviewed.txt"
    prompt.write_text("Synthetic reviewed prompt")
    output = tmp_path / "client.py"
    output.write_text("existing content")
    fake_codex(monkeypatch, "invalid Python!")
    with pytest.raises(SystemExit, match="invalid Python"):
        cli.main(["gen", "api.example.com", "--generator", "codex", "--reviewed-prompt", str(prompt), "-o", str(output)])
    assert output.read_text() == "existing content"


def test_doctor_accepts_codex_only(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/fake/codex" if name == "codex" else None)
    monkeypatch.setattr(cli, "_mitmweb_cmd", lambda: ["mitmweb"])
    monkeypatch.setattr(cli.mitm.Mitm, "flows", lambda self: [])
    monkeypatch.setattr(cli, "_lan_ip", lambda: "127.0.0.1")
    with pytest.raises(SystemExit) as error:
        cli.main(["doctor"])
    assert error.value.code == 0
    assert "[ok ] AI generator (claude, opencode, or codex)" in capsys.readouterr().out


def test_subprocess_roundtrip_without_executing_generated_code(monkeypatch, tmp_path):
    """Real subprocess plumbing, with a fake local CLI rather than a provider."""
    fake = tmp_path / "fake_codex.py"
    fake.write_text(
        "import pathlib, sys\n"
        "if '--help' in sys.argv:\n"
        "    print('--ignore-user-config')\n"
        "else:\n"
        "    assert sys.stdin.read() == 'SYNTHETIC REVIEWED PROMPT'\n"
        "    assert list(pathlib.Path.cwd().iterdir()) == []\n"
        "    output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n"
        "    output.write_text('raise RuntimeError(\"must never execute generated code\")\\n')\n"
        "    print('progress is not source')\n"
    )
    actual_run = subprocess.run

    def run(command, **kwargs):
        return actual_run([sys.executable, str(fake), *command[1:]], **kwargs)

    monkeypatch.setattr(codegen.subprocess, "run", run)
    source = codegen.generate("api.example.com", [], generator="codex",
                              reviewed_prompt="SYNTHETIC REVIEWED PROMPT")
    assert source == 'raise RuntimeError("must never execute generated code")\n'
