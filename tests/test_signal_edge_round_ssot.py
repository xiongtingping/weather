import unittest


class TestSignalEdgeRoundSSOT(unittest.TestCase):
    def test_ui_has_no_local_edge_recompute_entrypoint(self) -> None:
        from ui import server

        self.assertFalse(hasattr(server, "_compute_edge_fields_in_place"))


if __name__ == "__main__":
    unittest.main()
