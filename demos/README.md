# Proteus Demos

Real-world projects demonstrating Proteus compression in action.

| Demo | Description | Run it | Avg Savings |
|---|---|---|---|
| [🌤 Weather Dashboard](weather-dashboard/) | HTML/CSS/JS weather app + live API | `python run_proteus_test.py` | 63% (9 files) |
| [📊 Log Analyzer](log-analyzer/) | Multi-service log pipeline | `python run_demo.py` | 69% (5 files) |

Each demo:
- Generates or fetches real data
- Compresses every file with the best-fit Proteus compressor
- Decompresses via CCR cache — verifies byte-for-byte identity
- Runs analysis on both original and decompressed — proves identical results
- Reports LLM cost savings at $2/M input tokens
