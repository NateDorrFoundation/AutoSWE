# Google Antigravity SDK / CLI Documentation

Mirror of the official docs at [antigravity.google/docs](https://antigravity.google/docs),
scoped to the programmatic surfaces (SDK, headless CLI, MCP, tools) that AutoSWE
would need to drive Antigravity as a coding backend.

> **Source:** `https://antigravity.google/docs/<path>.md` — the site serves raw
> markdown directly (the "View Markdown" button on each page). No scraping or
> HTML conversion; these are the canonical pages.
>
> **Refresh:**
> ```bash
> # one page:
> curl -sL https://antigravity.google/docs/sdk/mcp.md -o docs/antigravity/sdk-mcp.md
> # full mirror: re-fetch every page listed in the ToC below
> ```
> Mirrored 2026-11-18.

## TL;DR — calling information

**Python SDK** (`pip install google-antigravity`, needs `GEMINI_API_KEY`):

```python
import asyncio
from google.antigravity import Agent, LocalAgentConfig

async def main():
    config = LocalAgentConfig()
    async with Agent(config) as agent:
        response = await agent.chat("What files are in the current directory?")
        print(await response.text())

asyncio.run(main())
```

- Vertex/GCP mode: `LocalAgentConfig(vertex=True, project=..., location=...)`
- Built-in tools via `BuiltinTools` enum (`run_command`, `edit_file`, `start_subagent`, …);
  gate them with `CapabilitiesConfig(enabled_tools=..., disabled_tools=...)`
- MCP servers, safety policies, personas, and subagents are all configured on the `Agent`
- Managed cloud alternative: Gemini API "Antigravity Agent" REST/gRPC endpoint

**CLI** (`agy`): interactive TUI plus **headless/print mode** (`agy -p "prompt"`, with
`--input-format` for machine-readable streams) and ACP (Agent Client Protocol) server
mode — see `cli-headless.md`, `cli-reference.md`, and `cli-acp.md`.

## Table of Contents

### SDK (Python — `google-antigravity`)

| Document | Description |
|---|---|
| [SDK Overview](./sdk-overview.md) | Install, quickstart, Vertex mode, core concepts |
| [Tools & Skills](./sdk-tools.md) | `BuiltinTools` enum, custom Python tools, web tools, agent skills, tool filtering |
| [MCP](./sdk-mcp.md) | Model Context Protocol server configuration for SDK agents |
| [Lifecycle](./sdk-lifecycle.md) | `Agent` lifecycle, sessions, stateful runtime |
| [Policies](./sdk-policies.md) | Safety policies / permission gates |
| [Personas](./sdk-personas.md) | Agent personas and prompting profiles |
| [Subagents](./sdk-subagents.md) | Programmatic subagent delegation |
| [Structured Output](./sdk-structured-output.md) | Schema-constrained responses |
| [Local Models](./sdk-local-models.md) | Running the SDK against local/Ollama-style models |

### CLI (`agy`)

| Document | Description |
|---|---|
| [Using the CLI](./cli-using.md) | Interactive usage, flags |
| [Headless Mode](./cli-headless.md) | Non-interactive / scripted runs — the backend entry point |
| [Command Reference](./cli-reference.md) | Full flag + command reference |
| [ACP](./cli-acp.md) | Agent Client Protocol server mode (editor integration transport) |
| [Modes](./cli-modes.md) | Permission/behavior modes |
| [Artifacts](./cli-artifacts.md) | Artifact output handling |
| [Conversations](./cli-conversations.md) | Session/conversation management, resume |
| [Prompting](./cli-prompting.md) | Prompting guidance |
| [Best Practices](./cli-best-practices.md) | Recommended usage |
| [Credits](./cli-credits.md) | Quota/credits model |
| [gcli Migration](./cli-gcli-migration.md) | Migrating from the older CLI |
| [Troubleshooting](./cli-troubleshooting.md) | Common issues |
| [Statusline](./cli-statusline.md) / [Title](./cli-title.md) / [Vim Mode](./cli-vim-editor-mode.md) | TUI extras |
| Slash commands | `cli-commands-*.md` — agents, codesearch, context, diff, effort, permissions, resume, usage, voice, etc. |

### Cross-surface tooling (IDE + CLI + SDK shared concepts)

| Document | Description |
|---|---|
| [MCP](./mcp.md) | MCP configuration across Antigravity surfaces |
| [Tools](./tools.md) | Built-in tool catalog |
| [Subagents](./subagents.md) | Subagent concept (file-defined) |
| [Hooks](./hooks.md) | Lifecycle hooks |
| [Skills](./skills.md) | Agent skills format |
| [Plugins](./plugins.md) | Plugin system / custom agents |
| [Rules](./rules.md) | Project rules (AGENTS.md-style steering) |
| [Sandbox](./sandbox.md) | Sandbox / execution isolation |
| [Permissions](./permissions.md) | Permission model |
| [Models](./models.md) | Available Gemini models |
| [Install](./install.md) / [Getting Started](./getting-started.md) | Setup |

## Not mirrored (probably irrelevant to AutoSWE)

IDE-UI pages (`ide/*`, side panel, review editor, voice mode, screenshots),
billing/plans, enterprise admin, marketplace, walkthroughs, blog posts.
Full sitemap: `https://antigravity.google/sitemap.xml` (~116 doc pages; 49 mirrored here).
