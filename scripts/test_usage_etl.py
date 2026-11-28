import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ETL = Path(__file__).resolve().parent / "usage_etl" / "etl_behavior_ledger.py"


class UsageETLTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix="usage # ")
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        self.now = int(datetime.now(timezone.utc).timestamp())
        self.stats = self.root / "stats.json"
        self.snapshot = self.root / "usage_snapshot.json"
        self.ledger = self.root / "ledger.db"

    def run_etl(self, source=""):
        return subprocess.run(
            [sys.executable, str(ETL)], check=False, capture_output=True, text=True,
            env={**os.environ, "CC_SWITCH_DB": str(source), "GW_STATS": str(self.stats),
                 "BEHAVIOR_LEDGER_DB": str(self.ledger), "WB_USAGE_SNAPSHOT": str(self.snapshot)},
        )

    def write_stats(self, *rows):
        self.stats.write_text(json.dumps({"requests": rows}), encoding="utf-8")

    def test_gateway_only_is_idempotent_and_keeps_both_simultaneous_requests(self):
        row = {"account": "fixture-account", "at": self.now * 1000, "model": "auto",
               "resolvedModel": "fixture-model", "credit": 2.5}
        self.write_stats(row, row)
        first = self.run_etl()
        self.assertEqual(first.returncode, 0, first.stderr)
        summary = json.loads(first.stdout)
        self.assertEqual((summary["gateway_new_rows"], summary["last_7d"]["requests"]), (2, 2))
        self.assertEqual(json.loads(self.run_etl().stdout)["gateway_new_rows"], 0)
        payload = json.loads(self.snapshot.read_text(encoding="utf-8"))
        self.assertEqual(payload["kpi"]["gateway_requests"], 2)
        self.assertEqual(payload["kpi"]["codex_requests"], 0)
        self.assertEqual(payload["kpi"]["models"], [
            {"model": "fixture-model", "chain": "gateway", "req": 2, "cost": 5.0},
        ])

    def test_empty_install_can_refresh_without_optional_sources(self):
        proc = self.run_etl()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(self.snapshot.read_text(encoding="utf-8"))
        self.assertEqual((payload["kpi"]["requests"], payload["days"]), (0, []))

    def test_broken_configured_source_preserves_last_snapshot(self):
        self.snapshot.write_text("previous snapshot", encoding="utf-8")
        self.stats.write_text("{broken data", encoding="utf-8")
        self.assertNotEqual(self.run_etl().returncode, 0)
        self.assertEqual(self.snapshot.read_text(encoding="utf-8"), "previous snapshot")

    def test_v2_ledger_migrates_without_losing_historical_model_data(self):
        with sqlite3.connect(self.ledger) as db:
            self.addCleanup(db.close)
            db.execute("""CREATE TABLE requests (
                request_id TEXT, created_ts INTEGER, model TEXT, input_tokens INTEGER,
                output_tokens INTEGER, cache_read_tokens INTEGER, total_cost_usd REAL,
                latency_ms INTEGER, first_token_ms INTEGER, status_code INTEGER, error_message TEXT,
                session_id TEXT, chain TEXT, cost_credit REAL, PRIMARY KEY (request_id, created_ts))""")
            db.execute("""INSERT INTO requests
                (request_id, created_ts, model, chain, cost_credit) VALUES ('old', ?, ?, 'gateway', 3.5)""",
                       (self.now, "{'id': 'old-model'}"))
        run = self.run_etl()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(self.snapshot.read_text())["kpi"]["models"],
                         [{"model": "old-model", "chain": "gateway", "req": 1, "cost": 3.5}])
        self.assertEqual(db.execute("SELECT resolved_model FROM requests").fetchone(), (None,))

    def test_codex_wal_source_is_read_only_and_counts_actual_insertions(self):
        path = self.root / "CC Switch #?.db"
        with sqlite3.connect(path) as src:
            self.addCleanup(src.close)
            src.execute("PRAGMA journal_mode=WAL")
            src.execute("""CREATE TABLE proxy_request_logs (
                request_id, created_at, model, input_tokens, output_tokens, cache_read_tokens,
                total_cost_usd, latency_ms, first_token_ms, status_code, error_message, session_id)""")
            src.execute("INSERT INTO proxy_request_logs VALUES (?, ?, ?, NULL, 4, NULL, 0.001, 10, 1, 200, NULL, NULL)",
                        ("a", self.now, "codex-fixture"))
            src.commit()
            first = self.run_etl(path)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads(first.stdout)["new_rows"], 1)
            self.assertEqual(json.loads(self.run_etl(path).stdout)["new_rows"], 0)
            self.assertEqual(src.execute("SELECT COUNT(*) FROM proxy_request_logs").fetchone()[0], 1)
            previous = self.snapshot.read_bytes()
            src.execute("INSERT INTO proxy_request_logs SELECT 'b', created_at, model, input_tokens, "
                        "output_tokens, cache_read_tokens, total_cost_usd, latency_ms, first_token_ms, "
                        "status_code, error_message, session_id FROM proxy_request_logs")
            src.commit()
            self.write_stats(
                {"account": "fixture", "at": self.now * 1000, "model": "fixture-model"},
                {"account": "missing-timestamp"},
            )
            self.assertNotEqual(self.run_etl(path).returncode, 0)
            self.assertEqual(self.snapshot.read_bytes(), previous)
            with sqlite3.connect(self.ledger) as ledger:
                self.assertEqual(ledger.execute("SELECT chain, COUNT(*) FROM requests GROUP BY chain").fetchall(),
                                 [("codex", 2)])
        self.assertEqual(json.loads(self.snapshot.read_text())["kpi"]["codex_requests"], 1)


if __name__ == "__main__":
    unittest.main()
