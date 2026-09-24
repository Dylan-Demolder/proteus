# Security Policy

## Supported Versions

Proteus is in alpha. Security fixes are applied to the latest release on the
`main` branch only.

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| < 0.2.0 | :x:                |

## Reporting a Vulnerability

Please **do not report security vulnerabilities through public GitHub issues.**

Instead, report them privately via one of:

- **GitHub Security Advisories** — [Report a vulnerability](https://github.com/Dylan-Demolder/proteus/security/advisories/new) (preferred; keeps the report private until a fix ships)
- **Email** — `dylan.m.demolder@gmail.com` with the subject `[SECURITY] proteus`

Please include:

1. The affected version (or commit SHA)
2. A description of the issue and its impact
3. Steps to reproduce, or a proof of concept
4. Any known mitigations

### What to expect

| Stage | Target |
| --- | --- |
| Acknowledgement | Within 3 working days |
| Assessment | Within 10 working days |
| Fix or mitigation plan | Depends on severity, agreed privately |

You will receive a confirmation when the report is received, an assessment once
it's been triaged, and credit in the changelog if you want it (reporters are
attribution-hidden by default unless you ask to be named).

## Threat Model Notes

Proteus sits between your agent and the LLM API provider, so it is worth being
explicit about what it does and does not do:

- **The CCR cache stores original (uncompressed) tool output on local disk** at
  `~/.proteus/cache/`. This can include source code, terminal output, file
  contents, and anything else your agent has read. Treat that directory as
  sensitive — it inherits the sensitivity of your tool outputs.
- **Compression is not encryption.** Compressed output and cached originals are
  readable by anyone with filesystem access to your machine.
- **The proxy forwards requests to a configured upstream.** API keys are read
  from environment variables and are never written to the CCR cache or log file.
- **The proxy does not authenticate callers.** It binds to `127.0.0.1` by
  default. Do not bind to `0.0.0.0` on an untrusted network without putting an
  authenticating reverse proxy in front of it.
- **Request logging is off by default** and only enabled when `--log-file` is
  passed. Log entries are JSONL and contain metadata only (timestamp, path,
  status, timings, compression counts) — **not** request bodies or API keys.

If you find a case where Proteus writes secrets to the cache, logs, or
compressed output that it should not, that is a security bug — please report it
privately.
