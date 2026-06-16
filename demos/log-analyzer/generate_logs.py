#!/usr/bin/env python3
"""Generate realistic multi-service log data with repeated patterns."""

import random
import datetime
import os
import json
import string

random.seed(42)

SERVICES = {
    "nginx": {
        "ips": ["10.0.1.1", "10.0.1.15", "192.168.1.100", "203.0.113.42", "198.51.100.7"],
        "methods": ["GET", "POST", "PUT", "DELETE"],
        "paths": ["/api/users", "/api/orders", "/api/products", "/health", "/static/css/main.css",
                  "/api/search?q=", "/api/auth/login", "/api/auth/refresh", "/api/inventory",
                  "/static/js/bundle.js", "/api/metrics", "/favicon.ico"],
        "statuses": [200, 200, 200, 200, 201, 301, 400, 401, 403, 404, 500, 502, 503],
    },
    "app": {
        "ips": ["10.0.2.10", "10.0.2.11", "10.0.2.12"],
        "levels": ["INFO", "INFO", "INFO", "INFO", "WARN", "WARN", "ERROR"],
        "modules": ["auth.handler", "order.processor", "payment.gateway", "user.cache",
                     "search.indexer", "queue.worker", "db.session", "rate.limiter"],
        "messages": [
            "Request processed in {:.0f}ms",
            "Cache miss for key={}",
            "User session refreshed: user_id={}",
            "DB query took {:.0f}ms — slow query threshold exceeded",
            "Payment processed: order={}, amount=${:.2f}",
            "Rate limit hit for IP {}: {} requests in window",
            "Connection pool exhausted — {} active connections",
            "Failed to deserialize payload: {}",
            "Retry attempt {} of {} for order {}",
            "Index rebuilt: {} documents in {:.0f}s",
            "Heartbeat OK — {} workers active",
            "Job queue depth: {} — processing rate {}/s",
        ],
    },
    "db": {
        "queries": [
            "SELECT * FROM users WHERE id = ?",
            "INSERT INTO orders (user_id, total) VALUES (?, ?)",
            "UPDATE inventory SET stock = stock - ? WHERE product_id = ?",
            "SELECT COUNT(*) FROM sessions WHERE expires_at < NOW()",
            "DELETE FROM temp_tokens WHERE created_at < ?",
            "SELECT p.*, COALESCE(AVG(r.rating), 0) FROM products p LEFT JOIN reviews r ON ...",
            "BEGIN TRANSACTION; INSERT INTO audit_log ...; COMMIT;",
        ],
        "durations": [0.5, 1.2, 3.8, 15.2, 45.7, 120.3, 250.1],
        "rows": [1, 5, 42, 157, 1024, 5230],
    },
}


def make_nginx_line(timestamp):
    ip = random.choice(SERVICES["nginx"]["ips"])
    method = random.choice(SERVICES["nginx"]["methods"])
    path = random.choice(SERVICES["nginx"]["paths"])
    if path.endswith("?q="):
        path += "+".join(random.choice(["iphone", "laptop", "shoes", "widget"]) for _ in range(random.randint(1, 3)))
    status = random.choices(SERVICES["nginx"]["statuses"], weights=[40, 25, 10, 5, 3, 2, 3, 4, 2, 3, 1, 1, 1])[0]
    size = random.randint(100, 50000)
    ua = random.choice(["Mozilla/5.0 ... Chrome/120", "curl/8.4", "python-requests/2.31", "Mobile Safari/17"])
    rt = random.uniform(0.01, 5.0)
    return f'{ip} - - [{timestamp.strftime("%d/%b/%Y:%H:%M:%S +0000")}] "{method} {path} HTTP/1.1" {status} {size} "{ua}" {rt:.3f}\n'


def make_app_line(timestamp):
    ip = random.choice(SERVICES["app"]["ips"])
    level = random.choices(SERVICES["app"]["levels"], weights=[30, 25, 15, 10, 10, 5, 5])[0]
    module = random.choice(SERVICES["app"]["modules"])
    msg_template = random.choice(SERVICES["app"]["messages"])

    placeholders = []
    for _, field_name, fmt_spec, _ in string.Formatter().parse(msg_template):
        if field_name is not None:
            placeholders.append(fmt_spec)
    
    if placeholders:
        args = []
        for fmt_spec in placeholders:
            if fmt_spec.startswith(".0f") or fmt_spec == "s":
                args.append(random.uniform(1, 500))
            elif fmt_spec.startswith(".2f"):
                args.append(random.uniform(5, 999.99))
            else:
                args.append(random.randint(1, 10000))
        msg = msg_template.format(*args)
    else:
        msg = msg_template

    return f"[{timestamp.isoformat()}] {level} [{module}] {msg} (from {ip})\n"


def make_db_line(timestamp):
    query = random.choice(SERVICES["db"]["queries"])
    duration = random.choice(SERVICES["db"]["durations"])
    rows = random.choice(SERVICES["db"]["rows"])
    return f"[{timestamp.isoformat()}] {duration:7.1f}ms {rows:5d} rows | {query}\n"


def generate_output_dir(base_dir, num_nginx=500, num_app=200, num_db=100):
    """Generate multi-service log output and save to files."""
    os.makedirs(base_dir, exist_ok=True)
    base_time = datetime.datetime(2025, 6, 17, 8, 0, 0)

    lines = []
    # Mix up the entries chronologically
    all_entries = []
    for i in range(num_nginx):
        ts = base_time + datetime.timedelta(seconds=i * 2 + random.randint(0, 3))
        all_entries.append((ts, "nginx", make_nginx_line(ts)))
    for i in range(num_app):
        ts = base_time + datetime.timedelta(seconds=i * 3 + random.randint(0, 5))
        all_entries.append((ts, "app", make_app_line(ts)))
    for i in range(num_db):
        ts = base_time + datetime.timedelta(seconds=i * 10 + random.randint(0, 15))
        all_entries.append((ts, "db", make_db_line(ts)))

    all_entries.sort(key=lambda x: x[0])

    # Write combined log
    with open(os.path.join(base_dir, "combined.log"), "w") as f:
        for _, _, line in all_entries:
            f.write(line)
    print(f"  combined.log: {len(all_entries)} lines")

    # Write per-service files
    for svc in ["nginx", "app", "db"]:
        svc_lines = [line for ts, s, line in all_entries if s == svc]
        with open(os.path.join(base_dir, f"{svc}.log"), "w") as f:
            f.writelines(svc_lines)
        print(f"  {svc}.log: {len(svc_lines)} lines")

    # Write a JSON version for the JSON compressor
    records = []
    for ts, svc, line in all_entries:
        records.append({
            "timestamp": ts.isoformat(),
            "service": svc,
            "size": len(line),
            "source": f"{svc}-{random.randint(1,3)}",
        })
    with open(os.path.join(base_dir, "metadata.json"), "w") as f:
        json.dump(records, f, indent=2)
    print(f"  metadata.json: {len(records)} records")

    return all_entries


if __name__ == "__main__":
    print("Generating demo log data...")
    generate_output_dir("/tmp/proteus-demo/data")
    print("Done.")