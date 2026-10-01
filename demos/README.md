# Demos

Two end-to-end demos that run Proteus over a realistic project's files and check the originals come back intact. Each runs in a few seconds from a clone:

```bash
pip install -e .                                          # from the repo root
cd demos/weather-dashboard && python run_proteus_test.py
cd demos/log-analyzer && python run_demo.py
```

Each demo compresses every file, restores each original from the cache by hash, and checks it matches byte for byte. That proves storage is lossless. It doesn't show what the model sees: for that, see [What your model sees](../README.md#what-your-model-sees) in the main README.

## Weather dashboard

[`weather-dashboard/`](weather-dashboard/) is a small HTML/CSS/JS app plus four weather API responses. One response is fetched live from wttr.in, falling back to a bundled copy offline.

| File | Content type | Size | Saved |
|---|---|---|---|
| `combined-project.txt` (all source, concatenated) | text | 130.9KB → 4.6KB | 96.5% |
| `weather-api-*.json` (×4) | json | 38.7KB → 23.1KB each | 40% |
| `app.js` | code_javascript | 5.1KB → 4.6KB | 11% |
| `index.html`, `styles.css`, `build-output.log` | text, logs | 9.3KB | unchanged |
| **Total, 9 files** | | **300.2KB → 111.2KB** | **62.9%** |

`combined-project.txt` shows the limits of the text compressor. It keeps the first and last ~2,000 chars and leaves the middle out, so the 96.5% comes mostly from content the model doesn't see until it calls `proteus_retrieve`. The weather JSON is compacted losslessly, and the demo checks 7 weather values in each response.

## Log analyzer

[`log-analyzer/`](log-analyzer/) generates logs from three services, compresses them, then runs the same 14-metric analysis (error counts, IPs, status codes, durations) on the originals restored from the cache.

| File | Compressor | Size | Saved |
|---|---|---|---|
| `nginx.log` (500 lines) | log_deduper | 55.2KB → 14.8KB | 73.2% |
| `combined.log` (3 services) | log_deduper | 84.6KB → 24.6KB | 70.9% |
| `metadata.json` (800 records) | json_crusher | 87.5KB → 28.2KB | 67.8% |
| `app.log` | log_deduper | 19.7KB → 16.5KB | 16.2% |
| `db.log` | log_deduper | 9.7KB → 8.9KB | 8.4% |
| **Total, 5 files** | | **256.6KB → 93.0KB** | **63.8%** |

The logs are generated randomly, so sizes vary slightly between runs.
