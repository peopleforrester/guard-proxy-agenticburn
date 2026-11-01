# Release notes

What each guard-proxy build a consumer can pin changes, newest first, naming its commit. The workshop
repo repins its `gitops/ai-layer/guard-proxy` submodule from these entries.

## 2026-11-01: `guard-proxy reset-spend` clears the Go build's spend store between deliveries

Go build only; `proxy.py` keeps its tally in memory and is unchanged (guard-proxy-agenticburn#33). The
workshop needs it at the Go cutover (Unleash_an_Agent_Watch_It_Burn#738) if deliveries share a volume.

- `guard-proxy reset-spend --store PATH` clears the cluster tally, every session's spend and the runtime
  cost cap, keeps the file, and prints what it cleared. Run it with the Deployment scaled to 0, from the
  same image with the volume mounted, then scale back to 1.
- It exits 1 without clearing anything when the proxy still holds the store, or when no store exists at
  `PATH`.

## 2026-11-01: /chat forwards only a storefront-shaped witch name

Repin the workshop's `gitops/ai-layer/guard-proxy` submodule to this entry's commit (or a later one) to
pick this up (guard-proxy-agenticburn#35). It needs no change in the workshop repo.

- `POST /chat` forwards `witch` only when, trimmed, it is `<First> the <Creature>`, each word one capital
  ASCII letter then 1 to 15 lowercase ones. All 768 names the storefront and the desk hand out pass.
- Any other `witch`, such as an injection POSTed straight to `/chat`, is no name: the message is exactly
  the prompt and the proxy logs `witch_name_refused` with the name's length.

## 2026-11-01: /chat forwards the visitor's witch name to the agent

Repin the workshop's `gitops/ai-layer/guard-proxy` submodule to this entry's commit (or a later one) to
pick this up (guard-proxy-agenticburn#34, asked by Unleash_an_Agent_Watch_It_Burn#762).

- `POST /chat` reads an optional `witch` string beside `prompt` and `session`. When it is set, the A2A
  message carries a second text part after the prompt: `The person chatting with you gave their name as
  Mortimer the Witch.` kagent passes every text part to the model, so HC-666 can name its Deployment
  `joker-<slug>` without the visitor typing their name.
- The name is not scanned or scrubbed: the block-list, the input classifier, the topic guard and the
  prompt feed see only the prompt, exactly as before.
- A request without `witch`, or with a blank or non-string one, sends exactly the prompt, as today.
- Not yet checked on a live cluster: an HC-666 run creating `joker-<visitor slug>`.

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
