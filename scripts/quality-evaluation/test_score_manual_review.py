from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("score_manual_review.py")


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


class ManualReviewScoringTests(unittest.TestCase):
    def run_score(self, root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(root)],
            capture_output=True,
            check=False,
            text=True,
        )

    def test_scores_only_human_labels_and_reports_pending_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "manual-review-items.csv",
                [
                    "manual_relevance",
                    "manual_title_clarity",
                    "manual_event_coverage",
                    "manual_source_attribution",
                ],
                [
                    {
                        "manual_relevance": "relevant",
                        "manual_title_clarity": "clear",
                        "manual_event_coverage": "complete",
                        "manual_source_attribution": "explicit",
                    },
                    {
                        "manual_relevance": "",
                        "manual_title_clarity": "unclear",
                        "manual_event_coverage": "partial",
                        "manual_source_attribution": "missing",
                    },
                ],
            )
            write_csv(
                root / "manual-review-candidates.csv",
                ["manual_relevance", "profile_ids"],
                [{"manual_relevance": "relevant", "profile_ids": "one; two"}],
            )
            write_csv(
                root / "manual-review-fact-claims.csv",
                ["is_key_fact", "manual_support"],
                [
                    {"is_key_fact": "yes", "manual_support": "supported"},
                    {"is_key_fact": "yes", "manual_support": "unsupported"},
                    {"is_key_fact": "no", "manual_support": ""},
                ],
            )
            write_csv(
                root / "manual-review-no-match.csv",
                ["manual_no_match_correct"],
                [{"manual_no_match_correct": "yes"}],
            )

            result = self.run_score(root)

            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["accepted_outputs_reviewed"], 1)
            self.assertEqual(report["clear_title_rate"], 0.5)
            self.assertEqual(report["complete_event_coverage_rate"], 0.5)
            self.assertEqual(report["explicit_source_attribution_rate"], 0.5)
            self.assertEqual(report["pending"]["accepted_output_relevance"], 1)
            self.assertEqual(report["key_facts_reviewed"], 2)
            self.assertEqual(report["key_fact_support_rate"], 0.5)
            self.assertEqual(report["profiles_covered_by_reviewed_candidate_materials"], 2)
            self.assertTrue(report["human_review_only"])

    def test_rejects_a_label_outside_the_documented_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_csv(
                root / "manual-review-items.csv",
                ["manual_relevance", "manual_title_clarity"],
                [{"manual_relevance": "looks-good", "manual_title_clarity": ""}],
            )
            write_csv(root / "manual-review-candidates.csv", ["manual_relevance", "profile_ids"], [])
            write_csv(root / "manual-review-fact-claims.csv", ["is_key_fact", "manual_support"], [])
            write_csv(root / "manual-review-no-match.csv", ["manual_no_match_correct"], [])

            result = self.run_score(root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Invalid manual_relevance label", result.stderr)


if __name__ == "__main__":
    unittest.main()
