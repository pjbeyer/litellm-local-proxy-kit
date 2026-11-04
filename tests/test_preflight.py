"""Offline regression tests for preflight command-mode behavior."""
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "preflight.py"
spec = importlib.util.spec_from_file_location("preflight_for_test", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("Unable to load preflight module for tests")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class PreflightModeTests(unittest.TestCase):
    def test_router_test_does_not_require_provider_credentials(self):
        self.assertFalse(preflight.requires_resolved_environment(router_test=True, catalogs=False))

    def test_catalog_reads_require_provider_credentials(self):
        self.assertTrue(preflight.requires_resolved_environment(router_test=True, catalogs=True))

    def test_default_validation_requires_provider_credentials(self):
        self.assertTrue(preflight.requires_resolved_environment(router_test=False, catalogs=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
