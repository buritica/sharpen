#!/usr/bin/env python3
"""Stdlib tests for portable capability manifests and profile resolution."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import capabilities  # noqa: E402


def manifest(**overrides):
    data = {
        "protocol_version": "1",
        "provider": {"name": "unit-test"},
        "capabilities": ["test", "lint", "typecheck"],
    }
    data.update(overrides)
    return data


class ManifestValidationTest(unittest.TestCase):
    def test_valid_manifest_normalizes_capability_order(self):
        result = capabilities.validate_manifest(
            manifest(capabilities=["typecheck", "lint", "test"])
        )
        self.assertEqual(result["capabilities"], ["lint", "test", "typecheck"])

    def test_missing_or_incorrect_protocol_version_fails(self):
        for value in (None, 1, "2"):
            with self.subTest(value=value):
                data = manifest()
                if value is None:
                    data.pop("protocol_version")
                else:
                    data["protocol_version"] = value
                with self.assertRaisesRegex(ValueError, "protocol_version"):
                    capabilities.validate_manifest(data)

    def test_empty_provider_name_fails(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "provider.name"):
                    capabilities.validate_manifest(
                        manifest(provider={"name": value})
                    )

    def test_duplicate_capability_fails(self):
        with self.assertRaisesRegex(ValueError, "duplicates"):
            capabilities.validate_manifest(
                manifest(capabilities=["test", "lint", "test"])
            )

    def test_unknown_capability_fails(self):
        with self.assertRaisesRegex(ValueError, "unknown capability"):
            capabilities.validate_manifest(
                manifest(capabilities=["test", "lint", "typecheck", "reviw"])
            )

    def test_unknown_extension_survives(self):
        result = capabilities.validate_manifest(
            manifest(**{"x-host": {"review": "/review"}})
        )
        self.assertEqual(result["x-host"], {"review": "/review"})


class ProfileResolutionTest(unittest.TestCase):
    def test_baseline_review_adversarial_select_correctly(self):
        cases = [
            (["test", "lint", "typecheck"], "baseline"),
            (["test", "lint", "typecheck", "review"], "review"),
            (["test", "lint", "typecheck", "review", "imagine", "fix"], "adversarial"),
        ]
        for available, expected in cases:
            with self.subTest(profile=expected):
                result = capabilities.resolve_profile(available)
                self.assertEqual(result["decision"], "selected")
                self.assertEqual(result["resolved_profile"], expected)
                self.assertEqual(result["missing"], [])

    def test_no_complete_baseline_returns_unavailable(self):
        result = capabilities.resolve_profile(["lint"])
        self.assertEqual(result["decision"], "unavailable")
        self.assertIsNone(result["resolved_profile"])
        self.assertEqual(result["missing"], ["test", "typecheck"])

    def test_explicit_unavailable_request_returns_missing_in_order(self):
        result = capabilities.resolve_profile(["test", "fix"], "adversarial")
        self.assertEqual(result["decision"], "unavailable")
        self.assertIsNone(result["resolved_profile"])
        self.assertEqual(result["missing"], ["lint", "typecheck", "review", "imagine"])

    def test_explicit_satisfied_request_selects_exactly_that_profile(self):
        result = capabilities.resolve_profile(
            ["test", "lint", "typecheck", "review", "imagine", "fix"], "review"
        )
        self.assertEqual(result["decision"], "selected")
        self.assertEqual(result["resolved_profile"], "review")
        self.assertEqual(result["requested_profile"], "review")
        self.assertEqual(result["missing"], [])

    def test_unknown_requested_profile_is_unavailable_not_downgraded(self):
        result = capabilities.resolve_profile(["test", "lint", "typecheck"], "bogus")
        self.assertEqual(result["decision"], "unavailable")
        self.assertIsNone(result["resolved_profile"])
        self.assertIn('unknown profile "bogus"', result["reason"])


if __name__ == "__main__":
    unittest.main()
