from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from proof_registry import AdaptiveProofRegistry, PROOF_MODEL


class AdaptiveProofRegistryTests(unittest.TestCase):
    def test_missing_registry_fails_closed(self) -> None:
        registry = AdaptiveProofRegistry(Path("does-not-exist.json"))
        self.assertEqual(registry.research_rows("LOOP"), [])
        self.assertEqual(registry.loop_proof_for({"symbol": "BTC/USDT"}), {})

    def test_only_exact_proven_settings_match(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "proof.json"
            path.write_text(
                json.dumps(
                    {
                        "version": PROOF_MODEL,
                        "profiles": [
                            {
                                "bot": "LOOP",
                                "symbol": "BTC/USDT",
                                "status": "Proven",
                                "proof_model": PROOF_MODEL,
                                "settings": {
                                    "timeframe": "1h",
                                    "order_distance_pct": 1.2,
                                    "order_count": 10,
                                    "take_profit_pct": 5.0,
                                    "stop_loss_pct": 7.0,
                                },
                                "train": {},
                                "test": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry = AdaptiveProofRegistry(path)
            live = {
                "symbol": "BTC/USDT",
                "timeframe": "1h",
                "order_distance_pct": 1.2,
                "order_count": 10,
                "take_profit_pct": 5.0,
                "monitored_stop_loss_pct": 7.0,
            }
            self.assertTrue(registry.loop_proof_for(live))
            self.assertEqual(registry.loop_proof_for({**live, "take_profit_pct": 20.0}), {})

    def test_needs_stronger_proof_never_matches(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "proof.json"
            path.write_text(
                json.dumps(
                    {
                        "version": PROOF_MODEL,
                        "profiles": [
                            {
                                "bot": "GRID",
                                "symbol": "DOGE/USDT",
                                "status": "Needs stronger proof",
                                "settings": {},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            registry = AdaptiveProofRegistry(path)
            self.assertEqual(registry.grid_proof_for({"symbol": "DOGE/USDT"}), {})


if __name__ == "__main__":
    unittest.main()
