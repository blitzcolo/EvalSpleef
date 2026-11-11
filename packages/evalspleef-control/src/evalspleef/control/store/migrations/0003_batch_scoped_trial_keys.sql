-- Batch-scope the trial projections (trial, grade, round).
--
-- The trial id format is frozen (PRD appendix A.1, docs/00-conventions 2), so two
-- batches planned from the same config mint identical trial ids and rows keyed by
-- trial_id alone collide in one store. The projections therefore key by
-- (batch_id, trial_id), matching the trial record schema, which already carries
-- batch_id next to trial_id (schemas/trial.schema.json). Existing rows keep their
-- ids; grade and round gain batch_id backfilled from their trial row (the old
-- trial primary key guaranteed exactly one trial row per trial_id, and the app
-- only writes grade/round rows after the trial row, so the join loses nothing).

CREATE TABLE trial_next (
    trial_id TEXT NOT NULL,
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
    updated_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, trial_id)
);
INSERT INTO trial_next (trial_id, batch_id, cell, task_id, model, shell, state, validity, epoch, idx, extensions, created_at, updated_at)
    SELECT trial_id, batch_id, cell, task_id, model, shell, state, validity, epoch, idx, extensions, created_at, updated_at
    FROM trial;
DROP TABLE trial;
ALTER TABLE trial_next RENAME TO trial;

CREATE TABLE grade_next (
    trial_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    tier TEXT NOT NULL,
    status TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    rule_id TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, trial_id, version)
);
INSERT INTO grade_next (trial_id, batch_id, version, tier, status, decided_by, rule_id, payload, created_at)
    SELECT g.trial_id, t.batch_id, g.version, g.tier, g.status, g.decided_by, g.rule_id, g.payload, g.created_at
    FROM grade g JOIN trial t ON t.trial_id = g.trial_id;
DROP TABLE grade;
ALTER TABLE grade_next RENAME TO grade;

CREATE TABLE round_next (
    trial_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    agent_steps INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    PRIMARY KEY (batch_id, trial_id, round_no)
);
INSERT INTO round_next (trial_id, batch_id, round_no, started_at, stopped_at, agent_steps, payload)
    SELECT r.trial_id, t.batch_id, r.round_no, r.started_at, r.stopped_at, r.agent_steps, r.payload
    FROM round r JOIN trial t ON t.trial_id = r.trial_id;
DROP TABLE round;
ALTER TABLE round_next RENAME TO round;
