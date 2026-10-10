"""Startup profiles choose documentation views; all 93 tools stay available.
Re-export directory, schema compatibility and view-state regression tests for
the existing run_all_tests.ps1 entry. Formal stdio coverage separately verifies
constant tools/list and unrestricted cross-directory calls.
"""
import unittest
from tests.test_dynamic_domains import MappingTests, RuntimeTests

if __name__ == "__main__":
    unittest.main()
