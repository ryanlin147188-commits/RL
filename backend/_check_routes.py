import re

base = "app/routers/"
files = ["projects.py", "tree_nodes.py", "testcases.py", "executions.py", "reports.py", "upload.py"]

for f in files:
    src = open(base + f, encoding="utf-8").read()
    hits = re.findall(r'@\S+\.(get|post|put|patch|delete|websocket)\(["\']([^"\']+)', src, re.I)
    print(f"\n{f}")
    for m, p in hits:
        print(f"  {m.upper():<10} {p}")
