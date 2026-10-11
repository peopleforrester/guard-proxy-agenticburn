# ABOUTME: Negative control for the conformance suite: runs it against deliberately broken copies of
# ABOUTME: proxy.py and fails unless every mutant is caught. Run: uv run python conformance/mutation_check.py
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "conformance" / "launch_python.py"

# (name, exact text in proxy.py, replacement). Each targets one guarantee the suite claims to pin. The
# text must occur exactly once, so a refactor of proxy.py makes this script fail loudly instead of
# silently testing nothing.
MUTANTS: list[tuple[str, str, str]] = [
    (
        "fail-open by default",
        'os.environ.get("PROXY_FAIL_CLOSED", "true")',
        'os.environ.get("PROXY_FAIL_CLOSED", "false")',
    ),
    (
        "block-list case-sensitive",
        "    low = text.lower()\n    return next(",
        "    low = text\n    return next(",
    ),
    ("classifier ignores verdict", 'return bool(verdict.get("is_valid", True))', "return True"),
    ("output scrub skips status message", '        scrub(status_msg.get("parts", []))\n', ""),
    (
        "output scrub touches user history",
        '            if entry.get("role") == "agent":\n                scrub(',
        "            if True:\n                scrub(",
    ),
    (
        "budget ignores session",
        "session_spend = _session_cost.get(session, 0.0) if session else spend",
        "session_spend = spend",
    ),
    (
        "rate limit off by one",
        "if len(_req_times) >= RATE_LIMIT_RPM:",
        "if len(_req_times) > RATE_LIMIT_RPM:",
    ),
    (
        "cost cap off by one",
        "if COST_CAP_USD > 0 and spend >= COST_CAP_USD:",
        "if COST_CAP_USD > 0 and spend > COST_CAP_USD:",
    ),
    (
        "budget cap off by one",
        "and session_spend >= BUDGET_CAP_USD:",
        "and session_spend > BUDGET_CAP_USD:",
    ),
    (
        "output price wrong",
        "(pout / 1000.0) * COST_PER_1K_OUT\n    with _cost_lock:",
        "(pout / 1000.0) * COST_PER_1K_IN\n    with _cost_lock:",
    ),
    ("probe recorded in feed", '    return PROBE_MARKER in (text or "")', "    return False"),
    (
        "no context rotation",
        "_ctx_gen[session] = _ctx_gen.get(session, 0) + 1",
        "_ctx_gen[session] = 0",
    ),
    (
        "no thinking retry",
        '            if _thinking_only:\n                log.info("model returned reasoning with no answer',
        '            if False:\n                log.info("model returned reasoning with no answer',
    ),
    (
        "cors echoes any origin",
        "        if _origin and _origin_allowed(self):",
        "        if _origin:",
    ),
    (
        "toggle ignores budget",
        'for k in ("input_blocklist", "input_classifier", "output", "budget"):',
        'for k in ("input_blocklist", "input_classifier", "output"):',
    ),
    (
        "client credentials forwarded",
        "                    req = urllib.request.Request(\n                        fwd_url, data=raw, headers=fwd_headers,",
        "                    req = urllib.request.Request(\n                        fwd_url, data=raw, headers={**dict(self.headers), **fwd_headers},",
    ),
]


def _run_mutant(
    source: str, work: Path, old: str, new: str, port_base: int
) -> tuple[bool, float, str]:
    """Run the suite against one mutant. Returns (caught, seconds, output tail on a harness error)."""
    with tempfile.TemporaryDirectory(dir=work) as d:
        (Path(d) / "proxy.py").write_text(source.replace(old, new))
        cmd = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(LAUNCHER))} {{port}} {shlex.quote(d)}"
        )
        t0 = time.monotonic()
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-x",
                "-q",
                "-p",
                "no:cacheprovider",
                str(ROOT / "conformance"),
            ],
            env={**os.environ, "GUARD_PROXY_CMD": cmd, "CONFORMANCE_PORT_BASE": str(port_base)},
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    summary = run.stdout.strip().splitlines()[-1] if run.stdout.strip() else ""
    # Caught means an assertion failed. A setup error or crash is a harness problem, and counting it
    # as a catch would let a broken harness report a perfect score.
    if run.returncode not in (0, 1) or " error" in summary:
        raise RuntimeError(
            f"harness error, not a verdict:\n{run.stdout[-3000:]}{run.stderr[-2000:]}"
        )
    return run.returncode == 1, time.monotonic() - t0, ""


def main() -> int:
    source = (ROOT / "proxy.py").read_text()
    for name, old, _ in MUTANTS:
        if source.count(old) != 1:
            print(
                f"{name}: target text found {source.count(old)} times; update MUTANTS to match "
                "proxy.py",
                file=sys.stderr,
            )
            return 2
    work = ROOT / ".mutants"
    work.mkdir(exist_ok=True)
    jobs = int(os.environ.get("MUTATION_JOBS", "4"))
    escaped: list[str] = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(_run_mutant, source, work, old, new, 10000 + 1000 * i): name
            for i, (name, old, new) in enumerate(MUTANTS)
        }
        for done, fut in enumerate(as_completed(futures), 1):
            name = futures[fut]
            caught, secs, _ = fut.result()
            elapsed = time.monotonic() - started
            eta = elapsed / done * (len(MUTANTS) - done)
            print(
                f"[{done}/{len(MUTANTS)}] {'caught' if caught else 'ESCAPED':8} {name} "
                f"({secs:.0f}s, ~{eta:.0f}s left)",
                flush=True,
            )
            if not caught:
                escaped.append(name)
    if escaped:
        print(f"{len(escaped)} mutant(s) escaped: {', '.join(escaped)}", file=sys.stderr)
        return 1
    print(f"all {len(MUTANTS)} mutants caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
