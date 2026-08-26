#!/usr/bin/env python3
"""Structural tests for v1 portable-core schemas and bundled examples.

The repository intentionally has no JSON Schema dependency. These tests enforce
only the portable v1 subset promised in docs/portable-core.md; the full Draft
2020-12 documents remain useful metadata for external validators.
"""

import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCHEMAS = os.path.join(ROOT, "schemas")


def load(relative):
    with open(os.path.join(ROOT, relative), encoding="utf-8") as f:
        return json.load(f)


def assert_extension_keys(test, value):
    for key in value:
        test.assertIsInstance(key, str)
        # Unknown regular keys are deliberately allowed by the schemas. This
        # assertion documents the convention without rejecting forward fields.
        if key.startswith("x-"):
            test.assertGreater(len(key), 2)


class SchemaDocumentTest(unittest.TestCase):
    def test_schema_ids_and_versions_are_stable(self):
        for name in ("capability-manifest", "review-report"):
            schema = load(f"schemas/{name}.v1.schema.json")
            self.assertEqual(
                schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
            )
            self.assertEqual(
                schema["$id"], f"https://sharpen.dev/schemas/{name}.v1.schema.json"
            )
            self.assertEqual(schema["properties"]["protocol_version"]["const"], "1")


class CapabilityManifestTest(unittest.TestCase):
    def test_example_matches_portable_v1_subset(self):
        manifest = load("schemas/examples/capability-manifest.v1.json")
        self.assertEqual(manifest["protocol_version"], "1")
        provider = manifest["provider"]
        self.assertIsInstance(provider, dict)
        self.assertIsInstance(provider["name"], str)
        self.assertTrue(provider["name"])
        capabilities = manifest["capabilities"]
        self.assertIsInstance(capabilities, list)
        self.assertTrue(capabilities)
        self.assertEqual(len(capabilities), len(set(capabilities)))
        allowed = {
            "plan",
            "review",
            "imagine",
            "fix",
            "test",
            "lint",
            "typecheck",
            "ship",
        }
        self.assertTrue(set(capabilities) <= allowed)
        assert_extension_keys(self, manifest)


class ReviewReportTest(unittest.TestCase):
    def test_example_matches_portable_v1_subset(self):
        report = load("schemas/examples/review-report.v1.json")
        self.assertEqual(report["protocol_version"], "1")
        self.assertIn(report["status"], {"pass", "fail", "inconclusive"})
        provenance = report["provenance"]
        self.assertIn(provenance["kind"], {"git-range", "legacy"})
        if provenance["kind"] == "git-range":
            for key in ("base", "head"):
                self.assertIsInstance(provenance[key], str)
                self.assertTrue(provenance[key])
        findings = report["findings"]
        self.assertIsInstance(findings, list)
        for finding in findings:
            self.assertIn(finding["severity"], {"critical", "serious", "suggestion"})
            for key in ("summary", "location", "consequence"):
                self.assertIsInstance(finding[key], str)
                self.assertTrue(finding[key])
        assert_extension_keys(self, report)


if __name__ == "__main__":
    unittest.main()
