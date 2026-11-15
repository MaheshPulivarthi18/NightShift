from nightshift.sandbox.docker import missing_script


def test_python_script_the_commit_lacks():
    out = ".venv/bin/python: can't open file '/work/repo/scripts/upstream.py': [Errno 2] No such file or directory"
    assert missing_script(" .venv/bin/python scripts/upstream.py checkout", out) == "scripts/upstream.py"


def test_shell_script_the_commit_lacks():
    out = "bash: line 1: ./scripts/bootstrap.sh: No such file or directory"
    assert missing_script("./scripts/bootstrap.sh --dev", out) == "scripts/bootstrap.sh"


def test_a_missing_file_the_command_does_not_name_is_a_real_failure():
    out = "can't open file '/work/repo/tools/gen.py': [Errno 2] No such file or directory"
    assert missing_script("make build", out) is None
    assert missing_script("pytest -q", "ERROR: file or directory not found: tests/unit") is None
