-- Initial schema for the control-plane relational store (TECH-DESIGN section 11
-- plus the v2 tables the control plane projects: trial, grade, round, artifact,
-- incident, canary). Portable across SQLite and PostgreSQL: TEXT/INTEGER/REAL
-- only, JSON documents as TEXT, booleans as INTEGER 0/1, timestamps as
-- ISO-8601 UTC strings.

CREATE TABLE model_spec (
    slug TEXT NOT NULL,
    spec_version INTEGER NOT NULL,
    family TEXT NOT NULL,
    roles TEXT NOT NULL,
    source TEXT NOT NULL,
    deployment TEXT NOT NULL,
    declared_context_tokens INTEGER NOT NULL,
    declared_max_output_tokens INTEGER NOT NULL,
    pricing_ref TEXT NOT NULL,
    license TEXT NOT NULL,
    owner TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (slug, spec_version)
);

CREATE TABLE hf_artifact (
    repo TEXT NOT NULL,
    revision TEXT NOT NULL,
    mirror_repo TEXT,
    files_sha256 TEXT NOT NULL,
    safetensors_bytes INTEGER NOT NULL,
    license TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (repo, revision)
);

CREATE TABLE endpoint (
    endpoint_id TEXT NOT NULL PRIMARY KEY,
    model_slug TEXT NOT NULL,
    model_spec_version INTEGER NOT NULL,
    adapter TEXT NOT NULL,
    account TEXT NOT NULL,
    state TEXT NOT NULL,
    external_ids TEXT NOT NULL,
    observed TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE endpoint_ticket (
    ticket_id TEXT NOT NULL PRIMARY KEY,
    endpoint_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    instructions_md TEXT NOT NULL,
    status TEXT NOT NULL,
    operator TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE probe_report (
    report_id TEXT NOT NULL PRIMARY KEY,
    endpoint_id TEXT NOT NULL,
    gateway_version TEXT NOT NULL,
    results TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE provider_instance (
    instance TEXT NOT NULL PRIMARY KEY,
    adapter TEXT NOT NULL,
    version TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    isolation_profile TEXT NOT NULL,
    verified_isolation INTEGER NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL
);

CREATE TABLE host (
    host_id TEXT NOT NULL PRIMARY KEY,
    instance TEXT NOT NULL,
    os TEXT NOT NULL,
    hw TEXT NOT NULL,
    hostd_version TEXT NOT NULL,
    state TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL
);

CREATE TABLE template_build (
    env_id TEXT NOT NULL,
    env_version TEXT NOT NULL,
    oci_digest TEXT NOT NULL,
    provider_template_ids TEXT NOT NULL,
    fingerprints TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY (env_id, env_version)
);

CREATE TABLE sandbox_lease (
    lease_id TEXT NOT NULL PRIMARY KEY,
    batch_id TEXT NOT NULL,
    trial_id TEXT NOT NULL,
    role TEXT NOT NULL,
    provider_instance TEXT,
    host_id TEXT,
    template_kind TEXT NOT NULL,
    template_ref TEXT NOT NULL,
    template_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    destroyed_at TEXT,
    reaped INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE gateway_key (
    key_id TEXT NOT NULL PRIMARY KEY,
    trial_id TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    budget TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE cost_ledger (
    entry_id TEXT NOT NULL PRIMARY KEY,
    batch_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    amount_usd REAL NOT NULL,
    quantity REAL NOT NULL,
    unit TEXT NOT NULL,
    period TEXT NOT NULL,
    reconciled INTEGER NOT NULL DEFAULT 0
);

-- v2 tables projected by the control plane.

CREATE TABLE trial (
    trial_id TEXT NOT NULL PRIMARY KEY,
    batch_id TEXT NOT NULL,
    cell TEXT,
    task_id TEXT NOT NULL,
    model TEXT NOT NULL,
    shell TEXT NOT NULL,
    state TEXT NOT NULL,
    validity TEXT NOT NULL,
    epoch INTEGER NOT NULL DEFAULT 1,
    idx INTEGER NOT NULL DEFAULT 0,
    extensions TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE grade (
    trial_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    tier TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    rule_id TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (trial_id, version)
);

CREATE TABLE round (
    trial_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    agent_steps INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    PRIMARY KEY (trial_id, round_no)
);

CREATE TABLE artifact (
    artifact_id TEXT NOT NULL PRIMARY KEY,
    trial_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE incident (
    incident_id TEXT NOT NULL PRIMARY KEY,
    trial_id TEXT,
    error_class TEXT NOT NULL,
    disposition TEXT NOT NULL,
    detail TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE canary (
    canary_id TEXT NOT NULL PRIMARY KEY,
    trial_id TEXT NOT NULL,
    token TEXT NOT NULL,
    injected_at TEXT NOT NULL,
    scanned_at TEXT,
    hits INTEGER NOT NULL DEFAULT 0
);
