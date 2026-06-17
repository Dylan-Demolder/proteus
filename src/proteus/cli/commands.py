"""Proteus CLI — command-line interface for compression and proxy."""

import json
import os
import sys
from pathlib import Path

import click

from proteus import compress_tool_output, compress_summary_line
from proteus.ccr import retrieve, stats as ccr_stats, clear as ccr_clear
from proteus.proxy.backends import list_backends


BACKEND_CHOICES = list(list_backends().keys())


@click.group()
def cli():
    """Proteus — shape-shifting compression for LLM tool outputs.

    Compresses large tool outputs before they reach the LLM context,
    saving 40-60% tokens. All compression is reversible via CCR cache.
    """
    pass


@cli.command()
@click.option("--port", default=8787, help="Port to bind to")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--backend", default="openrouter",
              type=click.Choice(BACKEND_CHOICES),
              help="Upstream API backend")
@click.option("--upstream-url", default=None,
              help="Override upstream API URL (for generic backend)")
@click.option("--api-key-env", default=None,
              help="Environment variable name for the API key (for generic backend)")
@click.option("--auto-detect", is_flag=True,
              help="Auto-detect backend from available API keys")
@click.option("--config", "config_path", default=None,
              help="Path to config YAML file")
@click.option("--log-file", default=None,
              help="Path to write request/response JSONL log")
def proxy(port, host, backend, upstream_url, api_key_env, auto_detect, config_path, log_file):
    """Start the Proteus compression proxy.

    Sits between your LLM client and API provider, compressing
    large tool outputs before they reach the LLM.

    Available backends:
    \b
    """
    # Print backend descriptions
    descriptions = list_backends()
    for name, desc in descriptions.items():
        click.echo(f"      {name}: {desc}", err=True)

    # Auto-detect if requested
    if auto_detect:
        from proteus.proxy.backends import auto_detect_backend
        detected = auto_detect_backend(prefer=backend)
        backend = detected.name
        click.echo(f"   Auto-detected backend: {backend}", err=True)
        # If generic was auto-detected without upstream-url, that's a problem
        if backend == "generic" and not upstream_url:
            click.echo("   Warning: generic backend requires --upstream-url", err=True)

    try:
        from proteus.proxy.server import start_proxy
    except ImportError as e:
        click.echo(f"Error: proxy dependencies not available: {e}", err=True)
        click.echo("Install with: pip install proteus[proxy]", err=True)
        sys.exit(1)

    click.echo(f"🚀 Proteus proxy starting on {host}:{port}", err=True)
    click.echo(f"   Backend: {backend}", err=True)
    click.echo(f"   Config:  {config_path or 'defaults'}", err=True)

    start_proxy(
        host=host,
        port=port,
        backend=backend,
        upstream_url=upstream_url,
        api_key_env=api_key_env,
        config_path=config_path,
        log_file=log_file,
    )


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--no-ccr", is_flag=True, help="Skip CCR cache storage")
def file(path, no_ccr):
    """Compress a single file and show compression stats."""
    content = Path(path).read_text()
    compressed, stats = compress_tool_output(content)

    if stats["was_compressed"]:
        ratio = stats["compression_pct"]
        saved = stats.get("estimated_token_savings", 0)
        ct = stats["content_type"]
        click.echo(f"📦 {Path(path).name}")
        click.echo(f"   Type:     {ct}")
        click.echo(f"   Size:     {stats['original_chars']:,} → {stats.get('compressed_chars', 0):,} chars ({ratio:.1f}%)")
        click.echo(f"   Savings:  ~{saved:,} tokens")
        click.echo(f"   Mode:     {stats.get('mode', '?')}")
        if stats.get("hash"):
            click.echo(f"   Hash:     {stats['hash']}")
        click.echo(f"\n{compressed[:2000]}")
        if len(compressed) > 2000:
            click.echo(f"\n... ({len(compressed) - 2000} more chars truncated)")
    else:
        click.echo(f"⏭️  {Path(path).name} — too small or uncompressible ({len(content):,} chars)")


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Show what would be compressed without writing")
def cache(path, dry_run):
    """Pre-compress a cache file on disk (creates .compressed companion).

    The original file is left untouched. The .compressed file is read
    instead of the original when available.
    """
    import shutil
    content = Path(path).read_text()
    compressed, stats = compress_tool_output(content)

    if not stats["was_compressed"]:
        click.echo(f"⏭️  {Path(path).name} — too small or uncompressible")
        return

    out_path = Path(path).with_suffix(Path(path).suffix + ".compressed")
    if dry_run:
        click.echo(f"📦 Would create: {out_path}")
        click.echo(f"   {stats['original_chars']:,} → {stats.get('compressed_chars', 0):,} chars ({stats['compression_pct']:.1f}%)")
        return

    out_path.write_text(compressed)
    # Also copy original alongside for easy retrieval
    orig_path = Path(path).with_suffix(Path(path).suffix + ".original")
    if not orig_path.exists():
        shutil.copy2(path, orig_path)

    click.echo(f"📦 Created: {out_path}")
    click.echo(f"   {stats['original_chars']:,} → {stats.get('compressed_chars', 0):,} chars ({stats['compression_pct']:.1f}%)")
    click.echo(f"   Original backed up: {orig_path}")


@cli.command()
def stats():
    """Show compression cache statistics."""
    s = ccr_stats()
    click.echo(f"📊 Proteus CCR Cache")
    click.echo(f"   Entries:    {s['entries']:,} / {s['max_entries']:,}")
    click.echo(f"   Cache dir:  {s['cache_dir']}")
    click.echo(f"   Disk usage: {s['total_size_bytes'] / 1024:.1f} KB")


@cli.command()
def clear():
    """Clear all cached compressed content."""
    count = ccr_stats()["entries"]
    ccr_clear()
    click.echo(f"🗑️  Cleared {count} entries from CCR cache")


@cli.command()
@click.argument("hash_key")
def retrieve(hash_key):
    """Retrieve original content from cache by hash."""
    original = retrieve(hash_key)
    if original is None:
        click.echo(f"❌ Hash '{hash_key}' not found in cache")
        sys.exit(1)
    click.echo(original)


if __name__ == "__main__":
    cli()
