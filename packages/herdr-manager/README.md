# herdr-manager

Start a dedicated interactive manager for the current Herdr session:

```bash
npx --yes herdr-manager
```

Without an explicit harness, the launcher uses the first available CLI in this order:

```text
grok -> codex -> claude
```

Select one explicitly when needed:

```bash
npx --yes herdr-manager codex
npx --yes herdr-manager claude
```

Run the command inside a Herdr pane. It requires `HERDR_ENV=1`, Node.js 20 or newer, and
an installed and authenticated selected harness. The manager observes only the current Herdr
session and does not provide durable queue, retry, lease, or receipt behavior.

The runtime and manager policy are provided by
[`herdr-orchestrator`](https://www.npmjs.com/package/herdr-orchestrator).
