# Fork note: `v2-shared`

This repository is `github.com/vasilvestre/opencode-a2a`, a fork of
`github.com/tbrandenburg/opencode-a2a`. The branch `v2-shared` adds three
settings on top of upstream. Month-long rebases stay possible: no module is
renamed, no file is moved, and each hunk stays small.

## Why the fork exists

Upstream pins V2 exclusive execution to OpenCode `2.0.28` and refuses any
workspace that carries plugins or MCP servers. The machine here runs OpenCode
`2.2.5` with a shared daily configuration, so both rules reject the only
upstream that exists:

| Lock | Upstream behaviour | Setting that lifts it |
|---|---|---|
| Version equality | `version == "2.0.28"` | `A2A_UPSTREAM_PIN_OVERRIDE=1` |
| Inventory ownership | refuses `/api/config` or `/api/plugin` entries | `A2A_SHARED_WORKSPACE=1` |
| Tool policy | `deny *`, `allow question`, `ask read` | `A2A_TOOL_POLICY=full` |

## The three settings

All three default to the upstream behaviour, so an operator who sets none sees
no change.

### `A2A_UPSTREAM_PIN_OVERRIDE=1`

Accepts an upstream version outside the `2.x` line without editing code. Leave
it unset to keep the pin. The error message names the version that the upstream
reported.

### `A2A_SHARED_WORKSPACE=1`

Skips only the `/api/config` and `/api/plugin` inventory checks. It keeps the
ownership lock, gateway-created sessions only, refusal of imported sessions, and
single-flight execution. By setting it the operator accepts two consequences:

1. Any local process that can reach this agent's credential can drive it.
2. The agent inherits the plugins and MCP servers of the shared workspace.

### `A2A_TOOL_POLICY=full`

Replaces the `deny *` rule with `allow *` and keeps `deny external_directory`.
`readonly` stays the default. Full mode runs shell commands, edits files and
calls other tools.

## Correlation limit

V2 terminal events carry the session ID, never the input ID. The gateway
therefore allows one active request per session, and sends follow-ups in the
same session in sequence. Two different A2A contexts use two different
sessions, which is what keeps their answers apart. See
[compatibility.md](compatibility.md) for the full chain.

## Agent Card caveat

The card still describes the upstream exclusive profile. With
`A2A_SHARED_WORKSPACE=1` the phrases "dedicated private upstream" and "no
external or autonomous writers" no longer hold.

## Verify

`scripts/probe-v2-shared.sh` starts a gateway against a live OpenCode 2.x and
checks four cases: a plain text task, a shell command with an unguessable
output, two sequential contexts, and a gateway restart. It needs an
`opencode serve` on `OPENCODE_BASE_URL` whose log prints its password, for
example:

```bash
cd /tmp/opencode/probe
opencode serve --hostname 127.0.0.1 --port 4097 > serve.log 2>&1 &
PROBE_DIR=/tmp/opencode/probe bash ~/src/opencode-a2a/scripts/probe-v2-shared.sh
```

The report lands in `$PROBE_DIR/report.txt`; the script exits non-zero when a
case fails.

## Install

```bash
uv tool install --force "opencode-a2a @ git+https://github.com/vasilvestre/opencode-a2a@v2-shared"
```

Then export `A2A_SHARED_WORKSPACE=1`, `A2A_TOOL_POLICY=full`, and point
`OPENCODE_BASE_URL` at the server root (`http://127.0.0.1:4096`), never at
`/api`: the client adds the path itself.
