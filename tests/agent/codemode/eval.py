#!/usr/bin/env python3
"""Agent evaluation for `aws codemode`: can an agent that has never seen the language read `aws codemode help`,
discover operations, and produce a program that validates — in few turns and without leaving the language?

Runs each case in tests/agent/codemode/cases.json through `claude -p` (Claude Code, non-interactive) with a
shim `aws` on PATH that points at this checkout. No AWS credentials are needed: `run` is forbidden and the
harness validates the agent's final program itself.

    .venv/bin/python tests/agent/codemode/eval.py                 # all cases
    .venv/bin/python tests/agent/codemode/eval.py top5-buckets    # one case
    .venv/bin/python tests/agent/codemode/eval.py --model sonnet --report /tmp/report.json

Exit status is the number of failed cases. Each case's transcript (stream-json) is kept under --workdir.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CASES = HERE / "cases.json"

SYSTEM_PROMPT = """You are operating the AWS CLI for a user. The only way you may touch AWS is the `aws codemode` feature.
Rules:
- Learn how it works ONLY by running `aws codemode help` (and its subcommands' help). Do not read any files.
- You have NO AWS credentials. Never run `aws codemode run` or any other `aws` command that would call AWS.
- Produce ONE TOWL program for the task, validate it with `aws codemode validate`, and fix every error. Read warnings
  and act on them only when they are right; do not add defaults just to silence a warning.
- When it validates, your final message must end with the complete program in a fenced block that starts
  with ```towl and contains nothing else. Do not write anything after that block."""

FENCE = re.compile(r"```towl\s*\n(.*?)```", re.S)
CODEMODE = re.compile(r"aws\s+codemode\s+(operation\s+search|schema|validate|run|help)")


def make_shim(bindir: Path):
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "aws"
    python = REPO / ".venv" / "bin" / "python"
    shim.write_text(f'#!/bin/sh\nexec "{python}" -m awscli "$@"\n')
    shim.chmod(0o755)
    return shim


def run_claude(prompt, model, budget, cwd, env, log_path):
    cmd = [
        "claude", "-p", "--verbose", "--output-format", "stream-json", "--no-session-persistence",
        "--permission-mode", "bypassPermissions", "--tools", "Bash",
        "--append-system-prompt", SYSTEM_PROMPT,
    ]
    if model:
        cmd += ["--model", model]
    if budget:
        cmd += ["--max-budget-usd", str(budget)]
    cmd.append(prompt)
    started = time.time()
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=900)
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    log_path.write_text(proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else ""))
    commands, final, meta = [], "", {}
    for ev in events:
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    commands.append(block.get("input", {}).get("command", ""))
                elif block.get("type") == "text":
                    final = block.get("text", "")  # the last text block is the final answer
        elif ev.get("type") == "result":
            meta = {k: ev.get(k) for k in ("num_turns", "total_cost_usd", "duration_ms", "is_error", "subtype")}
            if isinstance(ev.get("result"), str) and ev["result"]:
                final = ev["result"]
    meta["wall_s"] = round(time.time() - started, 1)
    meta["exit"] = proc.returncode
    return commands, final, meta


def validate(program, env):
    proc = subprocess.run(
        [str(REPO / ".venv" / "bin" / "python"), "-m", "awscli", "codemode", "validate", "--plan", program, "--output", "json"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"valid": False, "diagnostics": [{"code": "harness.unparseable", "message": (proc.stdout + proc.stderr)[-800:]}]}


def check_case(case, program, report, commands):
    failures = []
    if not program:
        return ["no ```towl block in the final message"]
    if not report.get("valid"):
        codes = [d.get("code") for d in report.get("diagnostics", [])]
        failures.append(f"program does not validate: {codes}")
    ops = {e["operation"] for e in report.get("effects", [])}
    effects = {e["effect"] for e in report.get("effects", [])}
    for op in case.get("expect_ops_all", []):
        if op not in ops:
            failures.append(f"expected operation {op}; program uses {sorted(ops)}")
    alternatives = case.get("expect_ops_any", [])
    if alternatives and not any(op in ops for op in alternatives):
        failures.append(f"expected one of {alternatives}; program uses {sorted(ops)}")
    for eff in case.get("expect_effects", []):
        if eff not in effects:
            failures.append(f"expected a {eff} effect; effects are {sorted(effects)}")
    for s in case.get("expect_substrings", []):
        if s not in program:
            failures.append(f"expected text {s!r}")
    for s in case.get("forbid_substrings", []):
        if s in program or any(s in c for c in commands):
            failures.append(f"forbidden text {s!r} present")
    warnings = {d.get("code") for d in report.get("warnings", []) + report.get("diagnostics", []) if d.get("severity") == "warning"}
    for w in case.get("forbid_warnings", []):
        if w in warnings:
            failures.append(f"forbidden warning {w}")
    n = sum(1 for c in commands if CODEMODE.search(c))
    if n > case.get("max_codemode_calls", 10):
        failures.append(f"{n} aws codemode invocations (limit {case.get('max_codemode_calls', 10)})")
    non_codemode = [c for c in commands if re.search(r"\baws\b", c) and not CODEMODE.search(c)]
    if non_codemode:
        failures.append(f"non-codemode aws commands: {non_codemode[:3]}")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="case ids to run (default: all)")
    ap.add_argument("--model", default=None, help="claude model alias (e.g. sonnet, opus)")
    ap.add_argument("--budget", type=float, default=1.0, help="max USD per case")
    ap.add_argument("--workdir", default=None, help="where transcripts and programs are kept (default: a temp dir)")
    ap.add_argument("--report", default=None, help="write a JSON report here")
    args = ap.parse_args()

    if shutil.which("claude") is None:
        sys.exit("claude CLI not found on PATH")
    cases = json.loads(CASES.read_text())["cases"]
    if args.ids:
        cases = [c for c in cases if c["id"] in args.ids]
        missing = set(args.ids) - {c["id"] for c in cases}
        if missing:
            sys.exit(f"unknown case ids: {sorted(missing)}")

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="codemode-eval-"))
    workdir.mkdir(parents=True, exist_ok=True)
    bindir = workdir / "bin"
    make_shim(bindir)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env.pop("AWS_PROFILE", None)
    env["AWS_DEFAULT_REGION"] = env.get("AWS_DEFAULT_REGION", "us-east-1")
    scratch = workdir / "scratch"  # an empty cwd: nothing to read, no project settings
    scratch.mkdir(exist_ok=True)

    results, failed = [], 0
    for case in cases:
        cid = case["id"]
        print(f"== {cid}", flush=True)
        commands, final, meta = run_claude(case["prompt"], args.model, args.budget, scratch, env, workdir / f"{cid}.stream.jsonl")
        m = FENCE.findall(final)
        program = m[-1].strip() if m else ""
        (workdir / f"{cid}.towl").write_text(program + "\n")
        report = validate(program, env) if program else {}
        failures = check_case(case, program, report, commands)
        cm = [c for c in commands if CODEMODE.search(c)]
        kinds = [CODEMODE.search(c).group(1).replace("operation ", "") for c in cm]
        status = "PASS" if not failures else "FAIL"
        failed += bool(failures)
        print(f"   {status}  turns={meta.get('num_turns')} codemode={len(cm)} {kinds} cost=${meta.get('total_cost_usd') or 0:.2f} wall={meta.get('wall_s')}s")
        if report:
            print(f"   result: {report.get('result_type')}  ops: {sorted({e['operation'] for e in report.get('effects', [])})}")
        for f in failures:
            print(f"   - {f}")
        results.append({"id": cid, "status": status, "failures": failures, "meta": meta, "commands": commands,
                        "program": program, "result_type": report.get("result_type"), "diagnostics": report.get("diagnostics", [])})
    print(f"\n{len(cases) - failed}/{len(cases)} passed; transcripts in {workdir}")
    if args.report:
        Path(args.report).write_text(json.dumps(results, indent=2))
    sys.exit(failed)


if __name__ == "__main__":
    main()
