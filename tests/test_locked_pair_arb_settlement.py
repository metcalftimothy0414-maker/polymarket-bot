from __future__ import annotations

import sqlite3
import unittest

from bot.db import SCHEMA
from bot.strategies.locked_pair_arb.settlement import extract_kalshi_outcome, settle_pair_position


class ExtractKalshiOutcomeTests(unittest.TestCase):
    def test_finalized_yes(self):
        self.assertEqual(extract_kalshi_outcome({"status": "finalized", "result": "yes"}), 1.0)

    def test_finalized_no(self):
        self.assertEqual(extract_kalshi_outcome({"status": "finalized", "result": "no"}), 0.0)

    def test_not_yet_finalized(self):
        self.assertIsNone(extract_kalshi_outcome({"status": "open", "result": ""}))

    def test_void_result_stays_unresolved(self):
        self.assertIsNone(extract_kalshi_outcome({"status": "finalized", "result": "void"}))


def _insert_position(conn: sqlite3.Connection, direction: str, size: int, entry_cost: float, predicted_edge: float) -> sqlite3.Row:
    conn.execute(
        "INSERT INTO pair_positions (id, strategy_id, pair_id, direction, size, leg_a_venue, leg_a_fill_price, "
        "leg_a_fee, leg_b_venue, leg_b_fill_price, leg_b_fee, entry_cost_usd, predicted_edge_per_contract, "
        "predicted_annual_return, status, opened_at) "
        "VALUES (1, 'locked_pair_arb', 1, ?, ?, 'kalshi', 0.3, 0.01, 'polymarket', 0.2, 0.01, ?, ?, 0.5, 'open', 'now')",
        (direction, size, entry_cost, predicted_edge),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM pair_positions WHERE id = 1").fetchone()


class SettlePairPositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def test_consistent_resolution_pays_exactly_size_dollars_no_divergence(self):
        # entry_cost 0.50/contract * 10 = 5.00; both venues resolve YES ->
        # gross payout is always exactly $1/contract regardless of which way,
        # as long as the venues agree (build prompt §5.3 table).
        pos = _insert_position(self.conn, "buy_kalshi_yes_poly_no", 10, entry_cost=5.0, predicted_edge=0.05)
        settle_pair_position(self.conn, pos, k_outcome=1.0, pm_outcome=1.0)

        row = self.conn.execute("SELECT gross_payout_usd, realized_pnl_usd, diverged FROM settlements").fetchone()
        self.assertEqual(row[0], 10.0)
        self.assertAlmostEqual(row[1], 5.0)
        self.assertEqual(row[2], 0)

        status = self.conn.execute("SELECT status FROM pair_positions WHERE id = 1").fetchone()[0]
        self.assertEqual(status, "closed")

    def test_consistent_resolution_no_no(self):
        pos = _insert_position(self.conn, "buy_kalshi_yes_poly_no", 10, entry_cost=5.0, predicted_edge=0.05)
        settle_pair_position(self.conn, pos, k_outcome=0.0, pm_outcome=0.0)
        row = self.conn.execute("SELECT gross_payout_usd, diverged FROM settlements").fetchone()
        self.assertEqual(row[0], 10.0)
        self.assertEqual(row[1], 0)

    def test_divergence_yes_no_is_a_total_loss_of_the_locked_leg(self):
        # Kalshi resolves YES, Polymarket resolves NO: buy_kalshi_yes_poly_no
        # loses the NO leg entirely (both legs would have needed the SAME
        # outcome sense to both pay).
        pos = _insert_position(self.conn, "buy_kalshi_yes_poly_no", 10, entry_cost=5.0, predicted_edge=0.05)
        settle_pair_position(self.conn, pos, k_outcome=1.0, pm_outcome=0.0)
        row = self.conn.execute("SELECT gross_payout_usd, realized_pnl_usd, diverged FROM settlements").fetchone()
        self.assertEqual(row[0], 20.0)  # k_outcome(1) + (1-pm_outcome=1) = 2/contract
        self.assertEqual(row[2], 1)

    def test_divergence_no_yes_is_a_windfall(self):
        pos = _insert_position(self.conn, "buy_kalshi_yes_poly_no", 10, entry_cost=5.0, predicted_edge=0.05)
        settle_pair_position(self.conn, pos, k_outcome=0.0, pm_outcome=1.0)
        row = self.conn.execute("SELECT gross_payout_usd, diverged FROM settlements").fetchone()
        self.assertEqual(row[0], 0.0)  # k_outcome(0) + (1-pm_outcome=0) = 0/contract
        self.assertEqual(row[1], 1)

    def test_other_direction_consistent_resolution(self):
        pos = _insert_position(self.conn, "buy_poly_yes_kalshi_no", 10, entry_cost=5.0, predicted_edge=0.05)
        settle_pair_position(self.conn, pos, k_outcome=1.0, pm_outcome=1.0)
        row = self.conn.execute("SELECT gross_payout_usd, diverged FROM settlements").fetchone()
        self.assertEqual(row[0], 10.0)
        self.assertEqual(row[1], 0)

    def test_edge_error_recorded(self):
        pos = _insert_position(self.conn, "buy_kalshi_yes_poly_no", 10, entry_cost=5.0, predicted_edge=0.05)
        settle_pair_position(self.conn, pos, k_outcome=1.0, pm_outcome=1.0)
        edge_error = self.conn.execute("SELECT edge_error_usd FROM settlements").fetchone()[0]
        # realized edge/contract = (10-5)/10 = 0.5; predicted was 0.05 -> error 0.45
        self.assertAlmostEqual(edge_error, 0.45)


if __name__ == "__main__":
    unittest.main()
