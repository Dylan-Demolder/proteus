# Proteus Integration Scripts

## auto_compress.py
Pre-compresses files for use with Proteus::
    python auto_compress.py <path>
    
Creates a `.compressed` companion file. The original is left untouched.

## Hermes Hook Integration
To automatically compress large tool outputs in Hermes:
    from proteus import compress_tool_output
    compressed, stats = compress_tool_output(large_tool_result)

## proteus-health-watchdog.sh
**Graceful proxy failover** — auto-bypasses Proteus when the proxy is down, restores it when healthy.

Use as a systemd timer or cron job (every 1 minute, no agent needed):

    # Copy to cron scripts dir
    cp proteus-health-watchdog.sh /etc/cron.d/  # or add to your scheduler
    
    # Or run as a Hermes no_agent cron
    hermes cron create --schedule "1m" --no-agent --script proteus-health-watchdog.sh

**What it does:**
1. Pings `http://127.0.0.1:8787/readyz` every minute
2. After 2 consecutive failures: clears `model.base_url` (bypasses proxy, goes direct)
3. When proxy recovers: restores `model.base_url` to `http://127.0.0.1:8787/v1`
4. Silent when healthy — only produces output on state transitions

**Failure modes handled:** proxy crash, port conflict, upstream API failure, OOM kill.

**Requirements:** `curl`, `hermes` CLI in PATH, config at `~/.hermes/config.yaml`.
