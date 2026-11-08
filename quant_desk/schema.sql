PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS cycles(
 id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT, code_sha TEXT,
 config_sha TEXT NOT NULL, mode TEXT NOT NULL, health TEXT, summary_json TEXT);
CREATE TABLE IF NOT EXISTS sources(
 id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES cycles(id), market TEXT,
 kind TEXT NOT NULL, url TEXT, received_at TEXT NOT NULL, source_time TEXT,
 historical_availability TEXT NOT NULL, sha256 TEXT, raw_path TEXT,
 status TEXT NOT NULL, error TEXT, UNIQUE(cycle_id,market,kind));
CREATE TABLE IF NOT EXISTS strategies(
 id TEXT NOT NULL, version INTEGER NOT NULL, owner TEXT NOT NULL,
 family TEXT NOT NULL, hypothesis TEXT NOT NULL, mechanism TEXT NOT NULL,
 market TEXT NOT NULL, timeframe TEXT NOT NULL, fingerprint TEXT NOT NULL,
 spec_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN('IDEA','SPEC','BACKTESTED','VALIDATED','PAPER','LIVE-SMALL','LIVE','RETIRED')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reason TEXT NOT NULL,
 execution_paused INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS history(
 seq INTEGER PRIMARY KEY, strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
 at TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
 actor TEXT NOT NULL, reason TEXT NOT NULL, evidence_sha256 TEXT,
 previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
 FOREIGN KEY(strategy_id,version) REFERENCES strategies(id,version));
CREATE TABLE IF NOT EXISTS graveyard(
 id INTEGER PRIMARY KEY, strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
 family TEXT NOT NULL, fingerprint TEXT NOT NULL, hypothesis TEXT NOT NULL,
 retired_at TEXT NOT NULL, cause TEXT NOT NULL, results_json TEXT NOT NULL,
 UNIQUE(strategy_id,version));
CREATE TABLE IF NOT EXISTS experiments(
 id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES cycles(id),
 strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
 split TEXT NOT NULL, parameters_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
 dataset_sha256 TEXT NOT NULL, started_at TEXT NOT NULL, result_json TEXT NOT NULL,
 UNIQUE(strategy_id,version,split,parameters_json,dataset_sha256));
CREATE TABLE IF NOT EXISTS paper_fills(
 fill_id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
 spec_sha256 TEXT NOT NULL, decision_at TEXT NOT NULL, filled_at TEXT NOT NULL,
 market TEXT NOT NULL, side TEXT NOT NULL, quantity REAL NOT NULL,
 price REAL NOT NULL, fee REAL NOT NULL, slippage_bps REAL NOT NULL,
 funding REAL NOT NULL, order_id TEXT NOT NULL, source_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_snapshots(
 cycle_id TEXT PRIMARY KEY REFERENCES cycles(id), at TEXT NOT NULL,
 accounting_label TEXT NOT NULL, equity REAL NOT NULL, realized REAL NOT NULL,
 unrealized REAL NOT NULL, open_risk REAL NOT NULL, gross_notional REAL NOT NULL,
 state TEXT NOT NULL, detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 id INTEGER PRIMARY KEY, strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
 spec_sha256 TEXT NOT NULL, stage TEXT NOT NULL, approved_by TEXT NOT NULL,
 at TEXT NOT NULL, evidence_sha256 TEXT NOT NULL, approval_reference TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS immutable_spec BEFORE UPDATE OF
 spec_json,spec_sha256,fingerprint,market,timeframe,hypothesis,mechanism ON strategies
 BEGIN SELECT RAISE(ABORT,'A spec change requires a new version'); END;
CREATE TRIGGER IF NOT EXISTS immutable_history_update BEFORE UPDATE ON history
 BEGIN SELECT RAISE(ABORT,'History is append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_history_delete BEFORE DELETE ON history
 BEGIN SELECT RAISE(ABORT,'History is append-only'); END;
