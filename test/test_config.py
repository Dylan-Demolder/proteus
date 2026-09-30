"""Configuration: config files, profiles and hot reload actually take effect.

Before this suite, `proteus proxy --config file.yaml` printed the path and
never read the file, and profiles were dicts nothing applied.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from proteus import config

config.CCR_CACHE_DIR = tempfile.mkdtemp(prefix="proteus-test-")
# configure()/reset() restore DEFAULTS, which include the real cache dir.
# Point the default at the temp dir too so no test writes to ~/.proteus.
config.DEFAULTS["CCR_CACHE_DIR"] = config.CCR_CACHE_DIR

from click.testing import CliRunner

from proteus import compress_tool_output
from proteus.cli.commands import cli
from proteus.compressors.diff import compress_diff
from proteus.compressors.search import compress_search
from proteus.profiles import PROFILES, use_profile
from proteus.proxy.handler import transform_request_body
from proteus.proxy.server import ProteusProxy

PASS = 0
FAIL = 0


def check(name: str, ok: bool):
    global PASS, FAIL
    if ok:
        print(f"  ✅ {name}")
        PASS += 1
    else:
        print(f"  ❌ {name}")
        FAIL += 1


def section(s: str):
    print(f"\n{'=' * 60}")
    print(f"  {s}")
    print(f"{'=' * 60}")


def raises(fn, *args, **kwargs) -> str:
    try:
        fn(*args, **kwargs)
    except ValueError as e:
        return str(e)
    return ""


tmp = Path(tempfile.mkdtemp())


def write(name: str, text: str) -> Path:
    path = tmp / name
    path.write_text(text)
    return path


# =============================================================================
section("1. Loading files")
# =============================================================================

config.configure(ROOT / "config.yaml")
loaded = config.current()
check("repo config.yaml loads and matches the defaults",
      loaded.pop("CCR_CACHE_DIR") == "~/.proteus/cache/"
      and loaded == {k: v for k, v in config.DEFAULTS.items() if k != "CCR_CACHE_DIR"})
config.reset()  # back to this suite's temp cache dir

proxy_section = config.configure(write("a.yaml", """
min_compress_chars: 1234
json:
  drop_head: 3
search:
  max_total: 7
proxy:
  port: 9999
"""))
check("top-level key applied", config.MIN_COMPRESS_CHARS == 1234)
check("section key applied", config.JSON_DROP_HEAD == 3 and config.SEARCH_MAX_TOTAL == 7)
check("proxy section returned", proxy_section == {"port": 9999})

config.configure(write("b.yaml", "json:\n  drop_tail: 4\n"))
check("configure() starts from defaults, not the previous file",
      config.DEFAULTS["MIN_COMPRESS_CHARS"] == config.MIN_COMPRESS_CHARS and config.JSON_DROP_TAIL == 4)

before = config.current()
msg = raises(config.configure, write("typo.yaml", "json:\n  drop_haed: 5\n"))
check("unknown key is an error naming it", "JSON_DROP_HAED" in msg)
msg = raises(config.configure, write("type.yaml", "json:\n  drop_head: five\n"))
check("wrong type is an error naming the key", "JSON_DROP_HEAD" in msg and "int" in msg)
msg = raises(config.configure, write("bool.yaml", "json:\n  drop_head: true\n"))
check("bool is not accepted for an int", "JSON_DROP_HEAD" in msg)
msg = raises(config.configure, write("proxy.yaml", "proxy:\n  prot: 1\n"))
check("unknown proxy key is an error", "prot" in msg)
check("a failed load changes nothing", config.current() == before)

config.reset()
check("reset() restores defaults", config.current() == config.DEFAULTS)
check("update() rejects unknown settings", "NOPE" in raises(config.update, {"NOPE": 1}))

# =============================================================================
section("2. Profiles take effect")
# =============================================================================

for name, values in PROFILES.items():
    check(f"profile {name!r} passes validation", raises(config.update, values) == "")
config.reset()

use_profile("aggressive")
check("use_profile() changes the live settings", PROFILES["aggressive"]["MIN_COMPRESS_CHARS"] == config.MIN_COMPRESS_CHARS)
mid = "The server returned results. " * 90  # ~2.6K chars: below 3K, above 2K
_, stats = compress_tool_output(mid + "\n" + "ERROR: timeout occurred.\n" * 10)
check("aggressive threshold is honoured by compress_tool_output", stats["content_type"] != "too_small")

rows = json.dumps([{"id": i, "name": f"n{i}"} for i in range(60)], indent=1)
body = {"messages": [{"role": "tool", "tool_call_id": "t", "content": rows}]}
check("aggressive threshold is honoured by the proxy handler",
      2000 < len(rows) < 3000 and transform_request_body(body)[2]["compressed"] == 1)

use_profile("conservative")
check("switching profiles resets first", PROFILES["conservative"]["JSON_DROP_HEAD"] == config.JSON_DROP_HEAD
      and config.MIN_COMPRESS_CHARS == 5000)

config.configure(write("p.yaml", "profile: aggressive\njson:\n  drop_head: 3\n"))
check("file profile applies", config.MIN_COMPRESS_CHARS == 2000)
check("file keys override the file's profile", config.JSON_DROP_HEAD == 3)
config.configure(tmp / "p.yaml", profile="conservative")
check("explicit profile overrides the file's profile", config.MIN_COMPRESS_CHARS == 5000)
check("...but file keys still win", config.JSON_DROP_HEAD == 3)
config.reset()

# =============================================================================
section("3. Search and diff limits come from config")
# =============================================================================

grep = "\n".join(f"src/m{i % 3}.py:{i}: value = compute({i})" for i in range(100))
config.update({"SEARCH_MAX_TOTAL": 4, "SEARCH_MAX_PER_FILE": 2})
_, stats = compress_search(grep)
check("search honours SEARCH_MAX_* settings", stats["compressed_matches"] <= 4)
config.reset()

diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n" + "\n".join(
    f"@@ -{i * 10 + 1},1 +{i * 10 + 1},1 @@\n-a{i}\n+b{i}" for i in range(6)
)
config.update({"DIFF_MAX_HUNKS_PER_FILE": 2})
out, stats = compress_diff(diff)
check("diff honours DIFF_MAX_HUNKS_PER_FILE", stats["hunks_kept"] == 2 and "4 more hunks" in out)
config.reset()

# =============================================================================
section("4. CLI")
# =============================================================================

runner = CliRunner()
sample = write("sample.json", "[" + ",".join(f'{{"id": {i}, "name": "n{i}"}}' for i in range(400)) + "]")
result = runner.invoke(cli, ["file", str(sample), "--config", str(tmp / "typo.yaml")])
check("bad --config exits 2 with the reason", result.exit_code == 2 and "JSON_DROP_HAED" in result.output)
result = runner.invoke(cli, ["file", str(sample), "--profile", "nonsense"])
check("unknown --profile is rejected", result.exit_code == 2)
result = runner.invoke(cli, ["file", str(sample), "--profile", "aggressive"])
check("--profile is applied", result.exit_code == 0 and config.MIN_COMPRESS_CHARS == 2000)
config.reset()
result = runner.invoke(cli, ["proxy", "--help"])
check("proxy --help lists --profile", "--profile" in result.output)

from unittest.mock import patch

proxy_yaml = write("proxy.yaml", "proxy:\n  port: 9100\n  backend: openai\n  host: 0.0.0.0\n")
with patch("proteus.proxy.server.start_proxy") as start:
    result = runner.invoke(cli, ["proxy", "--config", str(proxy_yaml), "--port", "9200"])
    kwargs = start.call_args.kwargs if start.called else {}
check("proxy: file's proxy: section supplies defaults",
      result.exit_code == 0 and kwargs.get("backend") == "openai" and kwargs.get("host") == "0.0.0.0")
check("proxy: command-line flags override the file", kwargs.get("port") == 9200)
check("proxy: config path is passed on for hot reload", kwargs.get("config_path") == str(proxy_yaml))
with patch("proteus.proxy.server.start_proxy") as start:
    result = runner.invoke(cli, ["proxy"])
    kwargs = start.call_args.kwargs if start.called else {}
check("proxy: built-in defaults without a file",
      (kwargs.get("host"), kwargs.get("port"), kwargs.get("backend")) == ("127.0.0.1", 8787, "openrouter"))
bad_backend = write("badbackend.yaml", "proxy:\n  backend: nope\n")
with patch("proteus.proxy.server.start_proxy") as start:
    result = runner.invoke(cli, ["proxy", "--config", str(bad_backend)])
check("proxy: unknown backend in the file is rejected",
      result.exit_code == 2 and "nope" in result.output and not start.called)
config.reset()

# =============================================================================
section("5. Proxy hot reload")
# =============================================================================

live = write("live.yaml", "min_compress_chars: 4000\n")
proxy = ProteusProxy(backend="openrouter", config_path=str(live))
check("proxy applies its config file at startup", config.MIN_COMPRESS_CHARS == 4000)
check("no reload when the file is unchanged", proxy.maybe_reload_config() is False)

live.write_text("min_compress_chars: 5000\n")
future = time.time() + 5
os.utime(live, (future, future))
check("edited file is reloaded", proxy.maybe_reload_config() is True and config.MIN_COMPRESS_CHARS == 5000)

live.write_text("min_compress_chars: nope\n")
os.utime(live, (future + 5, future + 5))
check("invalid edit is rejected", proxy.maybe_reload_config() is False)
check("...and the previous settings stay live", config.MIN_COMPRESS_CHARS == 5000)

check("invalid config fails at proxy startup",
      "MIN_COMPRESS_CHARS" in raises(ProteusProxy, backend="openrouter", config_path=str(live)))
config.reset()

# =============================================================================
section(f"RESULTS: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    print("\n  ❌ Some tests failed!")
    sys.exit(1)
else:
    print("\n  ✅ All config tests pass!")
