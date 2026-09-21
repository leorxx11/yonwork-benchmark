from datetime import datetime
import unittest
from unittest.mock import MagicMock, patch

from runner.reconcile import reconcile_suite, UPSERT_SQL


class ReconcileTests(unittest.TestCase):
    def test_reconcile_does_not_overwrite_counts_already_used_for_verdict(self):
        connection = MagicMock()
        runs = [{
            "benchmark_id": "b1", "started_at": datetime(2026, 9, 21),
            "ended_at": datetime(2026, 9, 21, 0, 0, 3),
            "model_mode": "newapi", "backend_matched_by": "time-window+model",
        }]
        with patch("runner.reconcile.NewApiConfig.load"), \
             patch("runner.reconcile.connect") as connect, \
             patch("runner.reconcile._runs_for", return_value=runs), \
             patch("runner.reconcile.fetch_logs", return_value=[{
                 "id": 7, "type": 2, "created_at": 1789948802,
             }]):
            connect.return_value.__enter__.return_value = connection
            result = reconcile_suite("suite")
        self.assertEqual(1, result["matched"])
        self.assertFalse(any(call.args[0] == UPSERT_SQL for call in
                             connection.cursor.return_value.__enter__.return_value.execute.call_args_list))
