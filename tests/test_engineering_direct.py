# SPDX-License-Identifier: AGPL-3.0-or-later

import unittest

from heretic.engineering_direct import (
    build_parser,
    data_ranges,
    select_trial,
    trial_has_update,
)


def _trial(attempt, *, accepted=True, keywords=0.5, first_kl=0.1, seq_kl=0.1):
    return {
        "attempt": attempt,
        "state": "COMPLETE",
        "events": [{"accepted": accepted}],
        "scores": {
            "keywords": keywords,
            "first_token_kl": first_kl,
            "sequence_kl": seq_kl,
            "log_odds": 0.2,
        },
    }


class EngineeringDirectTests(unittest.TestCase):
    def test_default_cli_is_small_direct_run(self):
        options = build_parser().parse_args([])

        self.assertEqual(options.fit_samples, 8)
        self.assertEqual(options.monitor_samples, 8)
        self.assertEqual(options.development_samples, 20)
        self.assertEqual(options.trials, 1)

    def test_data_ranges_are_disjoint_and_exact(self):
        ranges = data_ranges(8, 12, 20)

        self.assertEqual(
            ranges,
            {
                "fit": (0, 8),
                "monitor": (8, 20),
                "development": (20, 40),
            },
        )

    def test_data_ranges_reject_source_overflow(self):
        with self.assertRaisesRegex(ValueError, "exceeds"):
            data_ranges(200, 100, 101)

    def test_selection_requires_an_effective_update(self):
        unchanged = _trial(0, accepted=False)

        self.assertFalse(trial_has_update(unchanged))
        with self.assertRaisesRegex(RuntimeError, "accepted adapter update"):
            select_trial([unchanged])

    def test_selection_prefers_kl_guarded_candidate(self):
        lower_keywords_but_unsafe = _trial(0, keywords=0.1, first_kl=0.2, seq_kl=0.2)
        guarded = _trial(1, keywords=0.4, first_kl=0.1, seq_kl=0.1)

        selected = select_trial([lower_keywords_but_unsafe, guarded])

        self.assertEqual(selected["attempt"], 1)


if __name__ == "__main__":
    unittest.main()
