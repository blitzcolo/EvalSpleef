-- Inference benchmarks of an endpoint (spleef models bench). Additive: no existing table
-- changes. Rows are immutable like probe reports; the raw per-request samples live in a JSON
-- file next to the computed result (raw_path) and raw_sha256 pins its canonical content.

CREATE TABLE endpoint_benchmark (
    bench_id TEXT NOT NULL PRIMARY KEY,
    endpoint_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    plan TEXT NOT NULL,
    facts TEXT NOT NULL,
    result TEXT NOT NULL,
    raw_path TEXT,
    raw_sha256 TEXT NOT NULL
);

CREATE INDEX endpoint_benchmark_endpoint_idx ON endpoint_benchmark (endpoint_id, created_at);
