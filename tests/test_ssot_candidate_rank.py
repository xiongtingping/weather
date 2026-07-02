import unittest


class TestSsotCandidateRank(unittest.TestCase):
    def test_rank_requires_valid_ask(self) -> None:
        from ui.server import _ssot_candidate_rank

        cand = {"edge_score": None, "q_round": 0.99, "ask": None}
        self.assertLess(_ssot_candidate_rank(cand), -1e100)

        cand2 = {"edge_score": None, "q_round": 0.99, "ask": 0.5}
        self.assertGreater(_ssot_candidate_rank(cand2), -1e100)


if __name__ == "__main__":
    unittest.main()

