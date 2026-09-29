# Claude Code through LiteLLM

`claude-jb` runs Claude Code on non-Anthropic models through a LiteLLM proxy in
the dedicated `jailbee-litellm` Incus container. Plain `claude` remains native
and uses your Claude subscription; both commands can run side by side in the
same dev container. Gateway sessions share Claude Code's per-repo configuration
and history, but neither command changes the other's environment.

> **Unofficial subscription use:** LiteLLM's `chatgpt/` provider uses a ChatGPT
> subscription outside the Codex CLI. OpenAI may change the backend, reject
> requests or sanction the account without notice. Use it at your own risk.

This release supports **one implicit account (`default`) and `chatgpt/` models
only**. Other providers, multiple accounts, per-repo LiteLLM overrides,
`litellm.autostart` and `jailbee litellm ls` are not implemented. LiteLLM
settings belong in the host's `global.yaml`, not a committed repo config or a
host-local per-repo file. See [Configuration](config.md#litellm) and
[Commands](commands.md).

## Setup

1. On the host, set this in `~/.config/jailbee/global.yaml`:

   ```yaml
   litellm:
     enabled: true
   ```

2. Run `jailbee litellm up`, then `jailbee litellm login`. The latter starts
   LiteLLM's interactive ChatGPT device-code login for the `default` account.
3. Run `jailbee base build` in each repo that needs `claude-jb` (the wrapper is
   installed in the golden image). Run `jailbee apply` in each such repo to
   populate `/etc/jailbee/litellm.json` and its proxy key in existing running
   containers; new containers receive them at `jailbee new`. Recreate any old
   container whose image predates the wrapper: `apply` does not install binaries
   into existing containers.
4. Inside a dev container, run `claude-jb` instead of `claude`. Select a tier
   with Claude Code's `--model` flag, for example `claude-jb --model haiku`.
   Use `jailbee litellm status` on the host to inspect health and login state.

`jailbee litellm down` deletes the proxy container but keeps its host-side
login and settings. Run `jailbee apply` in each affected repo afterward: it
removes stale dev-container proxy settings, and `claude-jb` then fails clearly
instead of silently falling back to native Claude. Bring the proxy back with
`jailbee litellm up` and re-apply to restore access.

## Routes and profiles

The built-in `codex` profile maps Claude Code tiers to these routes:

| Tier | Route | Model | Effort |
|---|---|---|---|
| Fable | `astra` | `chatgpt/gpt-6-astra` | Follows Claude Code's session effort |
| Opus | `sol-xhigh` | `chatgpt/gpt-6-sol` | Fixed `xhigh` |
| Sonnet | `sol-medium` | `chatgpt/gpt-6-sol` | Fixed `medium` |
| Haiku | `luna-high` | `chatgpt/gpt-6-luna` | Fixed `high` |

Override only the fields you need in the host's `global.yaml`. A same-named
route overlays the built-in route field by field; a same-named profile overlays
its tiers field by field. New routes need a `model`, and profiles name routes:

```yaml
litellm:
  enabled: true
  default_profile: fast
  routes:
    sol-xhigh: {effort: max} # keeps the built-in chatgpt/gpt-6-sol model
    luna-floor: {model: chatgpt/gpt-6-luna, min_effort: medium}
  profiles:
    codex: {haiku: luna-floor} # other codex tiers keep their defaults
    fast: {sonnet: luna-floor, haiku: luna-floor, effort: low}
```

`effort` **fixes** the reasoning effort on every request; `min_effort` only
raises requests below its floor. They cannot be set together on a route. The
profile's `effort` sets the session's default `--effort` unless supplied by the
user. Claude Code always sends a session effort, so a LiteLLM deployment's
`reasoning_effort` alone cannot differentiate Opus and Sonnet when both use Sol:
fixed route efforts do. On a fixed-effort tier, Claude Code's `/effort` has no
effect. An explicitly `null` tier removes that mapping; unmapped requests may
hit the proxy's `claude-*` catch-all, which uses the default profile's cheapest
mapped tier.

Profile selection order is `claude-jb --profile NAME`, then
`JAILBEE_LITELLM_PROFILE`, then `litellm.default_profile` (default `codex`).
`--profile` is consumed by the wrapper, not passed to Claude Code. Use plain
`claude` for native access, not `--profile native`.

The three built-in GPT-6 models default to a **1,050,000-token total context
window**. A new model needs an explicit `context_window`; the wrapper exports
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` as the largest window among the selected
profile's mapped routes. That value is a total window, not guaranteed usable
input capacity on the ChatGPT subscription backend.

## Security and limitations

- ChatGPT OAuth tokens live under
  `~/.local/share/jailbee/litellm/default/auth/auth.json` (or the equivalent
  `$XDG_DATA_HOME` path), in an `auth/` directory with mode `0700`, bind-mounted
  into the dedicated proxy container. `jailbee litellm logout` deletes the
  token; `down` does not. Provider credentials are not copied to dev
  containers. They contain only a proxy key in `/etc/jailbee/litellm-default.key`
  (`0640`, readable by the dev group); its host copy is mode `0600`.
- The proxy has default-deny egress restricted to `chatgpt.com` and
  `auth.openai.com` after installation (package endpoints are needed during
  installation). Dev containers can reach its static address through the
  `jailbee-services` ACL; a proxy key is not a provider token.
- The default LiteLLM installation is pinned to version `1.103.0` and a
  hash-locked requirements file. Setting `litellm.version` bypasses the hash
  lock and emits a warning. Prompt/message logging and the remote model-cost
  map fetch are disabled. Treat this as risk reduction, not a guarantee that
  vendor traffic or the running service cannot expose sensitive prompts.
- Non-streaming requests to the ChatGPT backend fail with the current LiteLLM
  integration; Claude Code streams. Tool-call quality depends on the model;
  large tool arguments have been reported to corrupt on Codex routes.
- Claude Code's claude.ai connectors are disabled in gateway sessions.
  Resuming a native session with signed Opus thinking blocks through
  `claude-jb` is untested.
- Changing routes via `jailbee litellm up` restarts the proxy instance when
  its rendered files change, interrupting in-flight streams. `jailbee apply`
  does not restart or re-render the proxy in this phase: run `up` after edits.

## Troubleshooting

`claude-jb` fails rather than falling back to native:

| Error | Remedy |
|---|---|
| `no LiteLLM proxy configured for this container` | On the host run `jailbee litellm up`, then `jailbee apply` in this repo; rebuild the golden image if the wrapper itself is absent. |
| `cannot read ... (not valid JSON)` or `cannot read the proxy key` | Run `jailbee apply` in the repo on the host. |
| `unknown profile` | Check the names in global `litellm.profiles`; select a valid `--profile` or fix `JAILBEE_LITELLM_PROFILE`. |
| `--profile needs a name` | Pass `--profile NAME` or remove the flag. |

On the host, use `jailbee litellm status`, `jailbee litellm logs [-f]`, and
`jailbee doctor`. Doctor reports missing login, unhealthy service, mismatched
version and upstream reachability; `jailbee litellm up` re-resolves the proxy's
provider allowlist when upstream addresses change. `jailbee litellm login`
refreshes a missing login.
