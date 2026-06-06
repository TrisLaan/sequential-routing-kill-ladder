"""Phase 3 — code-agent ReAct loop for the smoke test.

Reuses mini-swe-agent's design and its swebench prompt (single bash command per
turn; THOUGHT + one ```mswea_bash_command block; submit via the
COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT sentinel). It is driven by OUR proven
OpenAIClient (handles gpt-5.4 reasoning_effort/temperature + retries + exact
token usage) instead of mini-swe-agent's litellm transport, because:
  * litellm.completion_cost can't price our 2026 model names, and
  * this path can be FULLY validated at $0 with MockClient before any paid call.

Every agent turn is logged to the existing tracer `step` table. The per-step
immediate check (command returncode: pass/fail) is stored in exec_error +
state_features so escalate-on-failure can be SIMULATED later without re-running.
Cost is realized (single arm at a time) — segregation invariant holds trivially.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass

import config
from agent.client import LLMResponse

BASH = shutil.which("bash") or r"C:\Program Files\Git\usr\bin\bash.exe"
SUBMIT_SENTINEL = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
_ACTION_RE = re.compile(r"```mswea_bash_command\s*\n(.*?)```", re.DOTALL)

SYSTEM_TEMPLATE = """\
You are a helpful assistant that can interact multiple times with a computer shell to solve programming tasks.
Your response must contain exactly ONE bash code block with ONE command (or commands connected with && or ||).

Include a THOUGHT section before your command where you explain your reasoning process.
Format your response as shown in <format_example>.

<format_example>
THOUGHT: Your reasoning and analysis here

```mswea_bash_command
your_command_here
```
</format_example>

Failure to follow these rules will cause your response to be rejected.\
"""

INSTANCE_TEMPLATE = """\
<pr_description>
Consider the following issue / PR description:
{task}
</pr_description>

<instructions>
# Task Instructions

## Overview
You're a software engineer interacting continuously with a computer by submitting commands.
Make changes to non-test source files in the working directory to fix the issue described
above, in a way that is general and consistent with the codebase.

<IMPORTANT>This is interactive: you think and issue ONE command, see its result, then think
and issue your next command.</IMPORTANT>

## Boundaries
- MODIFY: regular source files under the working directory: {cwd}
- DO NOT MODIFY: tests or configuration files (pyproject.toml, setup.cfg, etc.)

## Recommended workflow
1. Explore and read the relevant source files.
2. Write a small script to reproduce the issue and run it.
3. Edit the source to resolve the issue.
4. Re-run your reproduction to verify the fix.

## Environment details
- You are in a bash shell (Git Bash on Windows). The working directory for every command is {cwd}.
- The project's virtualenv is already on PATH: use `python` (NOT `python3`) and `pytest`.
- Directory / env-var changes do NOT persist between commands (each runs in a fresh subshell).
  Prefix with `cd {cwd} && ...` if you rely on the working directory.
- Use non-interactive flags; avoid vi/nano or anything needing user input.

## Command execution rules
1. You write a single command. 2. The system runs it in a subshell. 3. You see the result.
4. You write your next command. Each response: a THOUGHT section + exactly ONE
```mswea_bash_command block with exactly ONE command (use && / || to chain).

## Submission
When done, submit your changes as a git patch in TWO SEPARATE commands:
Step 1: `git diff > patch.txt` (only your source changes; do not commit).
Step 2 (EXACT, must be its own command):
```mswea_bash_command
echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt
```
If that command exits nonzero it will not submit. You cannot continue working after submitting.
</instructions>\
"""

OBSERVATION_TEMPLATE = (
    "<returncode>{rc}</returncode>\n<output>\n{out}</output>"
)
FORMAT_ERROR = (
    "Format error: your response must contain EXACTLY ONE "
    "```mswea_bash_command code block with exactly one command. "
    "Found {n} blocks. Please retry with the correct format."
)
MAX_OBS_CHARS = 10000


@dataclass
class StepRecord:
    index: int
    command: str
    returncode: int
    is_test_run: bool
    check: str            # "pass" | "fail" | "none"
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int


@dataclass
class RunResult:
    instance_id: str
    model: str
    resolved: int | None          # filled by grader later; None here
    exit_status: str              # submitted | step_limit | cost_capped | wall_limit | error
    submission: str               # patch text the agent submitted (may be "")
    num_steps: int
    total_cost_usd: float
    steps: list[StepRecord]
    trajectory_id: int


def _parse_action(text: str) -> tuple[str | None, int]:
    blocks = _ACTION_RE.findall(text)
    if len(blocks) != 1:
        return None, len(blocks)
    return blocks[0].strip(), 1


def _is_test_run(cmd: str) -> bool:
    c = cmd.lower()
    return ("pytest" in c) or ("python -m unittest" in c) or (" test" in c and ".py" in c)


def _exec_bash(command: str, cwd: str, env_path_prepend: str, timeout: int) -> tuple[int, str]:
    import os
    env = os.environ.copy()
    env["PATH"] = env_path_prepend + os.pathsep + env.get("PATH", "")
    env.update({"PAGER": "cat", "MANPAGER": "cat", "LESS": "-R",
                "PIP_PROGRESS_BAR": "off", "TQDM_DISABLE": "1", "PYTHONUTF8": "1",
                # non-interactive matplotlib: stops plt.show() from blocking a
                # command until the cmd-timeout (fake "horizon"/wall-cap artifact).
                "MPLBACKEND": "Agg"})
    # Popen + tree-kill on timeout. subprocess.run(timeout=) kills only the direct
    # child (bash) but NOT its grandchildren; on Windows a surviving grandchild that
    # inherited the stdout pipe makes the read block forever (observed hang). So on
    # timeout we taskkill the whole tree by PID, which closes the pipe and unblocks.
    proc = subprocess.Popen([BASH, "-c", command], cwd=cwd, env=env,
                            text=True, encoding="utf-8", errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            out, _ = proc.communicate(timeout=15)
        except Exception:
            out = ""
        return -1, f"[command timed out after {timeout}s; process tree killed]\n{out or ''}"


class CodeAgent:
    """One agent rollout on one task with one model, logging to the tracer."""

    def __init__(self, *, client, model: str, reasoning_effort: str | None,
                 tracer, policy_label: str, step_limit: int = 40,
                 per_task_cost_cap: float | None = None, cmd_timeout: int = 120,
                 wall_limit_s: int = 1800, on_spend=None):
        self.client = client
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.tracer = tracer
        self.policy_label = policy_label
        self.step_limit = step_limit
        self.per_task_cost_cap = per_task_cost_cap
        self.cmd_timeout = cmd_timeout
        self.wall_limit_s = wall_limit_s
        self.on_spend = on_spend          # callback(delta_cost) -> running cumulative

    def run(self, *, instance_id: str, repo: str, problem_statement: str,
            cwd: str, venv_scripts: str) -> RunResult:
        tid = self.tracer.start_trajectory(
            task_id=instance_id, db_id=repo, benchmark="swebench_live",
            policy_label=self.policy_label,
            notes=f"model={self.model} effort={self.reasoning_effort}",
        )
        messages = [
            {"role": "system", "content": SYSTEM_TEMPLATE},
            {"role": "user", "content": INSTANCE_TEMPLATE.format(
                task=problem_statement, cwd=cwd)},
        ]
        steps: list[StepRecord] = []
        total_cost = 0.0
        exit_status = "step_limit"
        submission = ""
        start = time.time()

        for idx in range(self.step_limit):
            if time.time() - start > self.wall_limit_s:
                exit_status = "wall_limit"
                break
            resp: LLMResponse = self.client.complete(
                self.model, messages, reasoning_effort=self.reasoning_effort)
            step_cost = config.cost_usd(self.model, resp.prompt_tokens, resp.completion_tokens)
            total_cost += step_cost
            if self.on_spend:
                self.on_spend(step_cost)
            messages.append({"role": "assistant", "content": resp.text})

            command, n = _parse_action(resp.text)
            if command is None:
                # log a no-action step, nudge format, continue
                self.tracer.log_step(
                    trajectory_id=tid, step_index=idx, decision_type="agent_step",
                    action_model=self.model, action_effort=self.reasoning_effort,
                    prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
                    cost_usd=step_cost, output=resp.text[:2000], exec_error="format_error",
                    state_features={"returncode": None, "check": "none",
                                    "is_test_run": False, "n_action_blocks": n})
                messages.append({"role": "user", "content": FORMAT_ERROR.format(n=n)})
                steps.append(StepRecord(idx, "", 0, False, "none", step_cost,
                                        resp.prompt_tokens, resp.completion_tokens))
                if self.per_task_cost_cap and total_cost > self.per_task_cost_cap:
                    exit_status = "cost_capped"
                    break
                continue

            rc, out = _exec_bash(command, cwd, venv_scripts, self.cmd_timeout)

            # Submission is detected from the OUTPUT's first line == SENTINEL (rc 0),
            # NOT from the command text — the agent legitimately prefixes the submit
            # command with `cd {cwd} && ...`, so a command-prefix gate misses it and
            # the agent then burns budget on no-ops. (Matches mini-swe-agent's
            # LocalEnvironment._check_finished, which also keys off the output.)
            first_lines = out.lstrip().splitlines()
            submitted_now = bool(rc == 0 and first_lines
                                 and first_lines[0].strip() == SUBMIT_SENTINEL)
            is_submit = submitted_now
            if submitted_now:
                submission = "\n".join(first_lines[1:])

            is_test = _is_test_run(command)
            check = "pass" if rc == 0 else "fail"
            decision = "submit" if submitted_now else "agent_step"
            self.tracer.log_step(
                trajectory_id=tid, step_index=idx, decision_type=decision,
                action_model=self.model, action_effort=self.reasoning_effort,
                prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
                cost_usd=step_cost, output=(command + "\n---\n" + out)[:4000],
                exec_error=(None if rc == 0 else f"returncode={rc}"),
                state_features={"returncode": rc, "check": check,
                                "is_test_run": is_test, "is_submit": is_submit})
            steps.append(StepRecord(idx, command, rc, is_test, check, step_cost,
                                    resp.prompt_tokens, resp.completion_tokens))

            if submitted_now:
                exit_status = "submitted"
                break

            # append observation
            obs = out
            if len(obs) > MAX_OBS_CHARS:
                obs = (obs[:5000] + f"\n... [{len(out) - 10000} chars elided] ...\n"
                       + obs[-5000:])
            messages.append({"role": "user",
                             "content": OBSERVATION_TEMPLATE.format(rc=rc, out=obs)})

            if self.per_task_cost_cap and total_cost > self.per_task_cost_cap:
                exit_status = "cost_capped"
                break

        self.tracer.finish_trajectory(
            tid, final_pred_sql=submission[:2000] if submission else None,
            final_success=None, success_method="pending_grade")
        return RunResult(instance_id=instance_id, model=self.model, resolved=None,
                         exit_status=exit_status, submission=submission,
                         num_steps=len(steps), total_cost_usd=total_cost,
                         steps=steps, trajectory_id=tid)
