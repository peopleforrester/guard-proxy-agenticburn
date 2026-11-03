# ABOUTME: Checks that #43's /chat rate-limit replies are on main: it fetches origin's main, then runs
# ABOUTME: this checkout's assertions for them against main's proxy.py and main's Go build.
from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# conformance/test_spend.py's assertions of the two replies #43 changed, word for word.
REPLY_TESTS = (
    "conformance/test_spend.py::test_rate_limit_chat_is_200_guarded",
    "conformance/test_spend.py::test_session_rate_limit_refuses_that_session_only",
)


def run(cmd: list[str], cwd: Path, timeout: float) -> bytes:
    assert shutil.which(cmd[0]), f"{cmd[0]} is not on PATH; this test needs it"
    done = subprocess.run(cmd, cwd=cwd, check=False, capture_output=True, timeout=timeout)
    assert done.returncode == 0, (
        f"{shlex.join(cmd)} failed:\n{done.stderr.decode(errors='replace')}"
    )
    return done.stdout


@pytest.fixture(scope="module")
def main_tree(tmp_path_factory) -> Path:
    """origin's main as it is now, extracted to a scratch directory."""
    run(["git", "fetch", "--quiet", "origin", "main"], ROOT, 120)
    sha = run(["git", "rev-parse", "FETCH_HEAD"], ROOT, 30).decode().strip()
    tree = tmp_path_factory.mktemp(f"main-{sha[:12]}")
    with tarfile.open(fileobj=io.BytesIO(run(["git", "archive", sha], ROOT, 120))) as archive:
        archive.extractall(tree, filter="data")
    return tree


def run_reply_tests(guard_proxy_cmd: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "GUARD_PROXY_CMD"}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--tb=line", *REPLY_TESTS],
        cwd=ROOT,
        env={**env, "GUARD_PROXY_CMD": guard_proxy_cmd},
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_main_proxy_py_sends_the_wait_and_send_again_replies(main_tree):
    launcher = ROOT / "conformance" / "launch_python.py"
    done = run_reply_tests(shlex.join([sys.executable, str(launcher), "{port}", str(main_tree)]))
    if done.returncode:
        pytest.fail(f"main's proxy.py does not send #43's replies:\n{done.stdout}", pytrace=False)


def test_main_go_build_sends_the_wait_and_send_again_replies(main_tree):
    binary = main_tree / "bin" / "guard-proxy"
    run(["go", "build", "-o", str(binary), "./cmd/guard-proxy"], main_tree, 600)
    done = run_reply_tests(f"{shlex.quote(str(binary))} --listen 127.0.0.1:{{port}}")
    if done.returncode:
        pytest.fail(f"main's Go build does not send #43's replies:\n{done.stdout}", pytrace=False)
