# Wire-sweep latency envelope

Live samples against `http://tools-gateway.localhost:3009`, 5 requests per route, captured in one `sweep.py` run. Regenerate with the command in README.md.

| Route | p50 (s) | max (s) | samples (s) | vs. observed envelope |
|---|---|---|---|---|
| search | 3.18 | 3.30 | 3.18, 3.16, 3.24, 3.30, 2.92 | within observed 2.5-4.6s |
| schemas (9-slug batch) | 0.53 | 0.78 | 0.78, 0.49, 0.70, 0.45, 0.53 | no observed baseline given for this route |
| execute (single tool) | 0.90 | 1.57 | 0.83, 1.04, 0.87, 0.90, 1.57 | within observed 1.0-1.3s |
| execute (10-tool batch) | 2.02 | 2.47 | 2.02, 2.47, 1.98, 1.78, 2.03 | REGRESSION: p50 2.02s > observed high 1.30s (delta +0.72s) |
| connections (status) | 0.45 | 0.48 | 0.37, 0.45, 0.48, 0.41, 0.48 | no observed baseline given for this route |
