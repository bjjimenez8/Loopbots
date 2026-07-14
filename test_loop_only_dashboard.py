from __future__ import annotations

import unittest

from dashboard import render_opportunities_dashboard


class LoopOnlyDashboardTests(unittest.TestCase):
    def test_grid_opportunities_and_history_are_not_rendered(self) -> None:
        snapshot = {
            "opportunities": {
                "generated_at": "2026-07-14T12:00:00+00:00",
                "filters": {"paper": "loop", "horizon": "all"},
                "opportunities": [
                    {"strategy": "GRID", "status": "Ready Now", "pair": "OLD/USDT"},
                ],
            },
            "active_setups": {
                "active": [{"strategy": "GRID", "pair": "OLD/USDT"}],
            },
            "opportunity_paper": {
                "investment_usd": 1000,
                "starting_balance_usd": 10000,
                "open": [{"strategy": "GRID", "pair": "OLD/USDT", "status": "OPEN"}],
                "closed": [],
            },
        }

        html = render_opportunities_dashboard(snapshot, refresh_seconds=30)

        self.assertNotIn("GRID Bots", html)
        self.assertNotIn("OLD/USDT", html)
        self.assertIn("LOOP Bots Ready Now", html)
        self.assertIn("LOOP Bot Paper Trading", html)


if __name__ == "__main__":
    unittest.main()
