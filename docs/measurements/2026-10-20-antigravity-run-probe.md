# Antigravity cannot yet preserve the tool-less run contract

The existing ping invocation allows file access. On 2026-10-20 at 06:56 UTC,
agy completed a `read_file` tool step and returned a random token stored only
in a scratch file. This fails the no-tools requirement of backlog #20.
Implementation stopped at the probe. `quotapick run` still accepts only
Claude subscriptions and API credit pools.

## Scope and method

The probe used the first configured Gemini pool through `agy_argv_env`, the
same account-selection helper as the usage reader. Both invocations requested
`gemini-4-argon-high`. The sign-in identity matched the pool's configuration
on both calls; no credential was read directly by the harness.

The owner authorized two CLI invocations, including agy's internal steps.
A fresh reviewer returned `ship` before execution. The harness made no
retries. Each invocation had a 60-second CLI timeout and a 75-second outer
timeout. Neither timed out. The scratch workspace contained no repository
or project instructions. The second workspace held only a synthetic file
whose random 128-bit token was absent from the prompt and environment.

Both commands used this argument shape, through the configured launch recipe:

```text
agy -p <prompt> --model gemini-4-argon-high
    --output-format <format> --log-file /dev/stderr
    --mode plan --sandbox --disable-slash-commands --print-timeout 60s
```

The arithmetic call used `json`. The file-read call used `stream-json` to
expose actual tool events. Only the output serialization differed among the
execution controls. The CLI loaded its existing global settings; these were
not changed by the harness. The harness made no router installation or
scheduled-job changes. During startup, agy logged installation or update of
its embedded webm encoder.

The retained stderr reports language-server version `1.4.3`. The agy binary
SHA-256 was
`836c446f6118140e86679ec6966a785eeedbf56b3711a8eb1f41947483e9802a`.
The router source revision used for both calls was `350d8c3`.

## Results

Times below come from the harness's UTC request and completion records.
Token columns preserve the CLI's field values. Thinking and cached tokens
are shown separately; they must not be added to the reported total.

| Invocation | UTC interval | Prompt purpose and observed behavior | Input | Output | Thinking | Cache read | Total |
|---|---|---|---:|---:|---:|---:|---:|
| Arithmetic | 06:54:56–06:55:06 | Asked for exactly `2` without tools; returned `2`, `SUCCESS`, exit 0 | 8,368 | 30 | 25 | 0 | 8,398 |
| File read | 06:56:09–06:56:26 | Asked to read only the synthetic file; completed `read_file` and returned its exact token, `SUCCESS`, exit 0 | 10,821 | 343 | 256 | 6,380 | 11,164 |

Execution accounting: **2 attempted, 2 succeeded, 0 failed**; execution
failure kinds: none. The file-access prohibition check was **1 attempted,
0 passed, 1 failed**, with failure kind `tool_access`. Both calls lack a
served-model stamp, so model verification has **2 attempted, 0 verified,
2 unverified**, with kind `model_unverified`. There were no further probes.

Both stderr streams contain this warning verbatim:

```text
warning: --mode plan has no effect while slash command expansion is disabled.
```

The second stream's completed tool event includes:

```json
{
  "step_index": 2,
  "state": "DONE",
  "step_type": "tool",
  "tool_name": "read_file",
  "tool_info": {
    "name": "read_file",
    "output": "1 lines, 50 bytes"
  }
}
```

This excerpt omits the conversation id, scratch path and duration; the
unabridged event and the returned canary token remain in the private records.
The final response matched the file token, so this is evidence of a completed
read rather than a model's claim to have read it.

The stream's available-tool list includes `view_file`, `run_command`,
`call_mcp_tool`, browser tools, file-writing tools and subagent tools.
Shell and MCP execution were **not tested**. Their presence is not proof
that permissions would allow execution, but they were not removed.
One successful file read is enough to reject the no-tools guarantee.

The file-read result reports `num_turns: 1` despite containing two completed
`agent_response` steps with a tool step between them. One user turn therefore
does not establish a single inference without an agent loop.

## Identity and remaining requirements

The log propagates the selected label `Gemini 4 Argon (High)` in both calls.
Neither result contains a served-model stamp. The streaming `init.model`
echoes the requested model and is not independent evidence of which model
served the call. Unlike the relaxed window-ping rule, `run` must preserve
its served-model verification requirement.

The installed `agy --help` exposes no `--tools` equivalent, strict MCP
configuration flag, or settings-file override. Google's
[execution-mode documentation](https://www.antigravity.google/docs/cli/modes/)
describes read tools in plan mode. Its
[headless documentation](https://www.antigravity.google/docs/cli/headless/)
explains that workspace file access is allowed by default and that
`num_turns` counts user turns.

Google documents [global permission deny rules](https://antigravity.google/docs/permissions),
including file, command and MCP namespaces. They were not tested here:
mutating the operator's shared settings for every `run` would affect concurrent
sessions and would not provide isolated per-call configuration. No supported
per-invocation tool-removal mechanism was established. This finding applies
to the tested invocation; it does not claim all future agy versions or
configurations are incapable of isolation.

Before #20 can proceed, establish an isolated invocation that prohibits
file, command and MCP tools while preserving vendor-owned authentication.
Then qualify served-model evidence, schema output, cost accounting and the
other existing `run` requirements. A prompt asking the model not to use tools,
a check after tools have run, or accepting a selected-model echo cannot
replace those guarantees.

## Dry-run request

No Antigravity `run` target was added. The requested dry-run cannot route:
`run` deliberately rejects that flag. With scratch router state, this command:

```sh
printf '%s\n' '{"capability":"premium","prompt":"Reply with exactly 2.","allow_credit":false,"max_attempts":1}' \
  | ./.venv/bin/python -m quota_router.cli run --dry-run --only antigravity_gemini
```

returned exit 2 and:

```text
usage: quotapick [-h] COMMAND ...
quotapick: error: unrecognized arguments: --dry-run
```

It made no model call. `pick --dry-run` would exercise a different contract
and would not demonstrate support in `run`.

## Private evidence

Full stdout and stderr, exact argv and prompts, UTC request/completion
records, the synthetic canary, offline analysis, and both probe reviews are
retained outside Git. The public record contains neither account identity nor
machine-specific paths.

These hashes identify the original raw byte streams:

| Invocation | Stream | SHA-256 |
|---|---|---|
| Arithmetic | stdout | `4e2729f67535c6bde27ae62776fe04f3238c021b8f3ba4fc0626b4ebca720fca` |
| Arithmetic | stderr | `5b12d3b64c8973db073a6f4d8978480a4dc7a4dec0ac1dd579a2560e48b12f2a` |
| File read | stdout | `5de01066d86f4af2c0eae1ca31686969fe8c5d537dc1f02df33744f178c45686` |
| File read | stderr | `f7cda97cd6b882e91a817cf98f131968f76a0df67bc466202d8bc82452806c2b` |
