from copy import deepcopy
import unittest

from product_radar.frozen_records import freeze_original_json, read_original_json
from product_radar.models import SafetyError, digest


class FrozenOriginalRecordTests(unittest.TestCase):
    def test_fractional_original_keeps_exact_original_hash_and_type(self):
        value = {"weight": 1.0, "fraction": 0.125, "small": 1e-08, "location": {"lat": "12.4"}}
        frozen = freeze_original_json(value)
        self.assertEqual(frozen["sha256"], digest(value))
        self.assertEqual(read_original_json(frozen), value)
        self.assertIs(type(read_original_json(frozen)["weight"]), float)
        self.assertIn('1.0', frozen["canonical_json"])
        self.assertTrue(all(isinstance(item, str) for item in frozen.values()))

    def test_unicode_nested_quotes_and_null_roundtrip(self):
        value = {"title": '₹ "Example"', "rows": [{"available": None, "observed": True}]}
        self.assertEqual(read_original_json(freeze_original_json(value)), value)

    def test_hash_or_raw_mutation_refused(self):
        for field, value in (("canonical_json", '{}'), ("sha256", '0' * 64)):
            with self.subTest(field=field):
                frozen = freeze_original_json({"weight": 1.0})
                frozen[field] = value
                with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
                    read_original_json(frozen)

    def test_noncanonical_and_duplicate_fields_refused_even_if_rehashed(self):
        from hashlib import sha256
        for raw in ('{ "a": 1}', '{"a":1,"a":2}'):
            with self.subTest(raw=raw):
                frozen = freeze_original_json({})
                frozen.update(canonical_json=raw, sha256=sha256(raw.encode()).hexdigest())
                with self.assertRaises(SafetyError):
                    read_original_json(frozen)

    def test_unknown_contract_extra_fields_and_wrong_expected_hash_refused(self):
        frozen = freeze_original_json({})
        for altered in (dict(frozen, contract="unknown"), dict(frozen, extra=True)):
            with self.assertRaises(SafetyError):
                read_original_json(altered)
        with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
            read_original_json(frozen, expected_hash="0" * 64)

    def test_nonobject_nonfinite_or_unbounded_original_refused(self):
        for value in ([], None, {"bad": float("nan")}, {"large": "x" * (1024 * 1024)}):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(SafetyError):
                    freeze_original_json(value)
