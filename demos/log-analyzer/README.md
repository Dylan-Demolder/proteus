# Log analyzer demo

Runs Proteus over realistic server logs, the kind of output an agent gets from `kubectl logs` or `cat /var/log/...`.

```bash
pip install -e ../..        # once, from this directory
python run_demo.py
```

What it does:

1. Generates about 800 log entries across nginx, an app server and a database, plus a metadata JSON file, in `/tmp/proteus-demo/data`.
2. Compresses each file with the compressor Proteus picks for it.
3. Restores each original from the cache by hash and checks it byte for byte.
4. Runs the same 14-metric analysis (error counts, unique IPs, status codes, slow operations, durations) on the restored files and compares it with the originals.
5. Prints compression ratios and a token-cost estimate at $2 per million input tokens.

Example output (sizes vary slightly, since the logs are random):

```
  app.log                19.7KB →  16.5KB  ( 16.2%)  [    log_deduper]
  combined.log           84.6KB →  24.6KB  ( 70.9%)  [    log_deduper]
  db.log                  9.7KB →   8.9KB  (  8.4%)  [    log_deduper]
  metadata.json          87.5KB →  28.2KB  ( 67.8%)  [   json_crusher]
  nginx.log              55.2KB →  14.8KB  ( 73.2%)  [    log_deduper]
  TOTAL                 256.6KB →  93.0KB  ( 63.8%)

  🟢 ALL FILES: 100% lossless roundtrip — every byte preserved
  🟢 ALL ANALYSES IDENTICAL
```

The analysis runs on originals restored from the cache, so matching metrics show that nothing is lost in storage. What the model sees is the compressed form. For logs, that means routine lines are counted rather than listed, and every error line is kept.

| File | Purpose |
|---|---|
| `run_demo.py` | Generates, compresses, restores, analyzes and verifies |
| `generate_logs.py` | Multi-service log generator |
| `analyze_logs.py` | The 14-metric log analysis |
