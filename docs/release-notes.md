# Release notes

What each guard-proxy build a consumer can pin changes, newest first, naming its commit. The workshop
repo repins its `gitops/ai-layer/guard-proxy` submodule from these entries.

## 2026-10-31: `5fe5a7c`, a timed-out /chat turn stops the agent's work

Repin the workshop's `gitops/ai-layer/guard-proxy` submodule to `5fe5a7c` (or a later commit) to pick this
up (guard-proxy-agenticburn#27, asked by Unleash_an_Agent_Watch_It_Burn#742).

- `POST /chat` now forwards the prompt as a non-blocking A2A `message/send` and polls the task with
  `tasks/get` until it finishes. `PROXY_TIMEOUT` bounds the whole forward, polls included, and
  `TURN_TIMEOUT` still cuts it shorter.
- When the forward fails or its deadline passes while the agent is still working, the room gets the
  same 502 sentence as before, and guard-proxy then sends the agent `tasks/cancel` for that turn's
  task. kagent (v0.10.1, `runtime: go`) stops the run, so the model and its tools stop being called.
- New log events: `task_cancel` (INFO, names the task id and the state the agent reports) and
  `task_cancel_failed` (ERROR, when the cancel cannot be sent or the agent refuses it). A Datadog
  monitor on `task_cancel_failed` would flag a turn left running.
- A turn answers up to about a second after the agent finishes, from the poll interval (0.25 s,
  doubling to 1 s). Each running turn adds about one `tasks/get` a second through agentgateway's
  `:3000` route.
- Not yet checked on a live cluster: that `tasks/get` and `tasks/cancel` reach `workshop-agent`
  through agentgateway, and that the cancelled run's tool calls stop in the trace.
