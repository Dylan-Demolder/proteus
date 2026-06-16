#!/usr/bin/env python3
"""Analyze server logs — produces identical results regardless of compression."""

import re
import os
import sys
from collections import Counter, defaultdict


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
STATUS_RE = re.compile(r'"\w+ /[^"]*" (\d{3})')
TIMESTAMP_RE = re.compile(r"(\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2})")
DURATION_MS_RE = re.compile(r"(\d+(?:\.\d+)?)ms")
LEVEL_RE = re.compile(r" (INFO|WARN|ERROR|DEBUG) ")
ERROR_MSG_RE = re.compile(r"ERROR\s+\[(.+?)\]\s+(.+)$", re.MULTILINE)
PATH_RE = re.compile(r'"(GET|POST|PUT|DELETE) (/[^" ]*)')


class AnalysisResult:
    def __init__(self, source_label: str):
        self.source = source_label
        self.total_lines = 0
        self.total_chars = 0
        self.status_codes = Counter()
        self.ip_counts = Counter()
        self.ip_requests_per_hour = defaultdict(int)
        self.top_paths = Counter()
        self.error_counts = {
            "5xx": 0,
            "4xx": 0,
            "nginx_errors": Counter(),
            "app_errors": [],
        }
        self.level_counts = Counter()
        self.slow_queries = []  # (>100ms)
        self.hourly_line_counts = Counter()
        self.duration_distribution = {"<10ms": 0, "10-100ms": 0, "100-500ms": 0, ">500ms": 0}
        self.error_messages = []
        self.unique_ips_at_hour_peak = set()


def analyze_log(text: str, source_label: str) -> AnalysisResult:
    result = AnalysisResult(source_label)

    lines = text.splitlines(keepends=True) if text.endswith("\n") else text.split("\n")
    result.total_lines = len(lines)
    result.total_chars = len(text)

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # IPs
        ips = IP_RE.findall(line)
        for ip in ips:
            result.ip_counts[ip] += 1

        # HTTP status codes (nginx format)
        status_match = STATUS_RE.search(line)
        if status_match:
            code = int(status_match.group(1))
            result.status_codes[code] += 1
            if code >= 500:
                result.error_counts["5xx"] += 1
                result.error_counts["nginx_errors"][code] += 1
            elif code >= 400:
                result.error_counts["4xx"] += 1
                result.error_counts["nginx_errors"][code] += 1

        # HTTP paths
        path_match = PATH_RE.search(line)
        if path_match:
            result.top_paths[f"{path_match.group(1)} {path_match.group(2)}"] += 1

        # Log levels (app format)
        level_match = LEVEL_RE.search(line)
        if level_match:
            level = level_match.group(1)
            result.level_counts[level] += 1
            if level == "ERROR":
                em = ERROR_MSG_RE.search(line)
                if em:
                    result.error_messages.append((em.group(1), em.group(2)))

        # Timestamp → hourly bucket
        ts_match = TIMESTAMP_RE.search(line)
        if ts_match:
            hour = ts_match.group(1)[13:15]  # Extract HH from DD/Mon/YYYY:HH:MM:SS
            result.hourly_line_counts[hour] += 1

        # Duration extraction (nginx $request_time or DB ms)
        durations = DURATION_MS_RE.findall(line)
        for d_str in durations:
            d = float(d_str)
            if d < 10:
                result.duration_distribution["<10ms"] += 1
            elif d < 100:
                result.duration_distribution["10-100ms"] += 1
            elif d < 500:
                result.duration_distribution["100-500ms"] += 1
            else:
                result.duration_distribution[">500ms"] += 1
            if d > 100:
                result.slow_queries.append(d)

    # Peak hour IPs
    if result.hourly_line_counts:
        peak_hour = result.hourly_line_counts.most_common(1)[0][0]
        for line in lines:
            if f":{peak_hour}:" in line:
                ips = IP_RE.findall(line)
                result.unique_ips_at_hour_peak.update(ips)

    return result


def compute_delta(a: AnalysisResult, b: AnalysisResult) -> dict:
    """Compare two analyses and return any differences."""
    diffs = {}

    if a.total_lines != b.total_lines:
        diffs["total_lines"] = (a.total_lines, b.total_lines)
    if a.status_codes != b.status_codes:
        diffs["status_codes"] = (dict(a.status_codes), dict(b.status_codes))
    if a.ip_counts != b.ip_counts:
        diffs["ip_counts"] = (dict(a.ip_counts), dict(b.ip_counts))
    if a.top_paths != b.top_paths:
        diffs["top_paths"] = (dict(a.top_paths), dict(b.top_paths))
    if a.level_counts != b.level_counts:
        diffs["level_counts"] = (dict(a.level_counts), dict(b.level_counts))
    if a.error_counts != b.error_counts:
        diffs["error_counts"] = (a.error_counts, b.error_counts)
    if a.hourly_line_counts != b.hourly_line_counts:
        diffs["hourly_line_counts"] = (dict(a.hourly_line_counts), dict(b.hourly_line_counts))
    if a.duration_distribution != b.duration_distribution:
        diffs["duration_distribution"] = (a.duration_distribution, b.duration_distribution)
    if len(a.slow_queries) != len(b.slow_queries) or (a.slow_queries and abs(sum(a.slow_queries) - sum(b.slow_queries)) > 0.01):
        if a.slow_queries or b.slow_queries:
            diffs["slow_queries"] = (len(a.slow_queries), len(b.slow_queries))
    if a.unique_ips_at_hour_peak != b.unique_ips_at_hour_peak:
        diffs["peak_hour_ips"] = (list(a.unique_ips_at_hour_peak), list(b.unique_ips_at_hour_peak))
    if len(a.error_messages) != len(b.error_messages):
        diffs["error_message_count"] = (len(a.error_messages), len(b.error_messages))

    return diffs


def print_report(result: AnalysisResult):
    print(f"\n{'='*60}")
    print(f"  REPORT: {result.source}")
    print(f"{'='*60}")
    print(f"  Lines:       {result.total_lines:,}")
    print(f"  Size:        {result.total_chars:,} chars")
    print(f"  Unique IPs:  {len(result.ip_counts)}")
    top_ip = result.ip_counts.most_common(1)
    if top_ip:
        print(f"  Top IP:      {top_ip[0][0]} ({top_ip[0][1]} requests)")
    top_path = result.top_paths.most_common(1)
    if top_path:
        print(f"  Top path:    {top_path[0][0]} ({top_path[0][1]} hits)")
    print(f"  Status 2xx:  {sum(v for k,v in result.status_codes.items() if 200<=k<300)}")
    print(f"  Status 4xx:  {result.error_counts['4xx']}")
    print(f"  Status 5xx:  {result.error_counts['5xx']}")
    print(f"  Log levels:  {dict(result.level_counts) if result.level_counts else '(none)'}")
    print(f"  Slow ops:    {len(result.slow_queries)} (>100ms)")
    print(f"  Error msgs:  {len(result.error_messages)}")
    if result.hourly_line_counts:
        peak = result.hourly_line_counts.most_common(1)[0]
        print(f"  Peak hour:   {peak[0]}:00 ({peak[1]} lines)")
        print(f"  Peak IPs:    {len(result.unique_ips_at_hour_peak)} unique")


if __name__ == "__main__":
    import glob
    for path in glob.glob("/tmp/proteus-demo/data/*.log") + glob.glob("/tmp/proteus-demo/data/*.json"):
        with open(path) as f:
            text = f.read()
        result = analyze_log(text, os.path.basename(path))
        print_report(result)