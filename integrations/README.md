# Integrations

Scripts for wiring Proteus into an agent you run. Most agents need none of these: point the agent's API base URL at `proteus proxy` instead (see the [main README](../README.md#quick-start)).

## `auto_compress.py`: pre-compress a file

```bash
python auto_compress.py path/to/big.json
```

This writes `big.json.compressed` next to the original and leaves the original untouched. It's useful for large cache files that an agent reads repeatedly. `proteus cache path/to/big.json` does the same, and also keeps a `.original` copy.

## Compress tool output inside your own agent

If you control the agent loop, call the library on each tool result before it goes into the conversation:

```python
from proteus import compress_tool_output

compressed, stats = compress_tool_output(tool_result)   # unchanged if small or incompressible
```

The original is kept in the local cache under `stats["hash"]`, and `proteus.ccr.retrieve(hash)` returns it. Without the proxy, nothing answers the model's `proteus_retrieve` calls, so your agent has to expose retrieval itself if the model needs dropped content.

## `proteus-health-watchdog.sh`: fail over if the proxy goes down

For Hermes Agent. The watchdog runs every minute (from cron, a systemd timer, or `hermes cron`) and keeps the agent working if the proxy dies:

1. It checks `http://127.0.0.1:8787/readyz`.
2. After 2 failures in a row, it clears Hermes's `model.base_url`, so the agent talks to the provider directly.
3. When the proxy is healthy again, it restores `model.base_url` to `http://127.0.0.1:8787/v1`.
4. It prints only on state changes.

```bash
hermes cron create --schedule "1m" --no-agent --script proteus-health-watchdog.sh
```

It needs `curl` and the `hermes` CLI, and reads `~/.hermes/config.yaml`. The Hermes binary path is set at the top of the script; adjust it for your install.
