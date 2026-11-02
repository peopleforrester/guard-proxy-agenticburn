# ABOUTME: Checks, on GitHub, that a comment on guard-proxy-agenticburn#39 names the cause of its hung wait.
# ABOUTME: Needs gh and the network, so it runs from the E2E command and fails loudly without them.
from __future__ import annotations

import json
import shutil
import subprocess

REPO = "peopleforrester/guard-proxy-agenticburn"
ISSUE = 39
# The cause #39 found: waiting on the mutation check by process name, which matches the waiter itself.
CAUSE = ("by process name", "pgrep -f mutation_check.py", "own command line")


def issue_comments(repo: str, number: int) -> list[str]:
    gh = shutil.which("gh")
    assert gh, "gh is not on PATH; this test reads the issue's comments through it"
    done = subprocess.run(
        [gh, "issue", "view", str(number), "-R", repo, "--json", "comments"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, f"gh issue view {repo}#{number} failed:\n{done.stderr}"
    return [c["body"] for c in json.loads(done.stdout)["comments"]]


def names_cause(body: str) -> bool:
    return all(term in body for term in CAUSE)


def test_a_comment_on_issue_39_names_the_cause() -> None:
    bodies = issue_comments(REPO, ISSUE)
    assert any(names_cause(b) for b in bodies), (
        f"no comment on {REPO}#{ISSUE} names the cause; each must contain all of {CAUSE}, "
        f"{len(bodies)} comment(s) read"
    )
