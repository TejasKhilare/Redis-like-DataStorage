"""Benchmark tooling: load generator, scenario suite and report.

    python -m benchmarks.load_gen --port 6379 --clients 50 --duration 10
    python -m benchmarks.suite --out benchmarks/results/run.json
    python -m benchmarks.report benchmarks/results/run.json

See docs/BENCHMARKS.md for the methodology and the published numbers.
"""
