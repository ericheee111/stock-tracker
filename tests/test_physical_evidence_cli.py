"""Subprocess CLI tests, isolated roots only; --apply never targets production."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from stock_tracker.runtime_evidence.physical_store import (
    EvidenceInput,
    PhysicalEvidenceStore,
    StreamKind,
    canonical_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'stock_tracker.runtime_evidence.physical_store_cli'


class TestPhysicalStoreCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='b1-cli-')
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / 'evidence'
        self.value = EvidenceInput(StreamKind.EVENT, 'a' * 64, 'fixture/session',
                                   'fixture-v1', datetime(2025, 1, 1, tzinfo=UTC),
                                   '{"last_price":"10"}')
        self.input = self.parent / 'input.json'
        self.input.write_bytes(canonical_bytes(self.value.document()))

    def cli(self, command, *arguments):
        p = subprocess.run([sys.executable, '-B', '-m', MODULE, command, '--root',
                            str(self.root), *map(str, arguments)], cwd=ROOT,
                           capture_output=True, text=True, encoding='utf-8', timeout=30,
                           check=False)
        return p, json.loads(p.stdout) if p.stdout.strip() else None

    def test_init_defaults_to_plan_without_creating_root(self):
        process, output = self.cli('init')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(output['mode'], 'DRY_RUN')
        self.assertFalse(self.root.exists())
        process, output = self.cli('init', '--apply')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(self.root.exists())
        self.assertEqual(output['scope'], 'PHYSICAL_BYTES_ONLY')
        for name in ('auto_trade', 'market_coverage_verified', 'trusted_admission', 'power_loss_certified'):
            self.assertIs(output[name], False)

    def test_append_requires_explicit_apply_and_page_omits_payload(self):
        store = PhysicalEvidenceStore.initialize(self.root)
        args = ('--store-id', store.store_id, '--input', self.input)
        process, output = self.cli('append', *args)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(output['status'], 'INPUT_VALIDATED_NOT_APPENDED')
        self.assertEqual(store.snapshot().high_water, 0)
        process, output = self.cli('append', *args, '--apply')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(output['append_order'], 1)
        process, output = self.cli('page', '--store-id', store.store_id, '--limit', '1')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(output['high_water'], 1)
        self.assertNotIn('payload', output['records'][0])

    def test_wrong_store_identity_fails_without_overwriting(self):
        store = PhysicalEvidenceStore.initialize(self.root)
        before = store.catalog.read_bytes()
        process, output = self.cli('audit', '--store-id', 'b' * 64)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(output['status'], 'BLOCKED')
        self.assertEqual(store.catalog.read_bytes(), before)

    def test_recover_is_not_automatic(self):
        store = PhysicalEvidenceStore.initialize(self.root)
        def stop(stage):
            if stage == 'intent_committed':
                raise RuntimeError('deliberate test stop')
        failing = PhysicalEvidenceStore(self.root, store.store_id, fault=stop)
        with self.assertRaises(RuntimeError):
            failing.append(self.value)
        process, output = self.cli('recover', '--store-id', store.store_id)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(output['pending_order'], 1)
        self.assertEqual(store.audit().snapshot.high_water, 0)
        process, output = self.cli('recover', '--store-id', store.store_id, '--apply')
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(output['ready'])
        self.assertEqual(output['high_water'], 1)

    def test_no_implicit_root_and_no_destructive_force_flag(self):
        process = subprocess.run([sys.executable, '-B', '-m', MODULE, 'init'], cwd=ROOT,
                                 capture_output=True, timeout=30, check=False)
        self.assertEqual(process.returncode, 2)
        process, _ = self.cli('init', '--force')
        self.assertEqual(process.returncode, 2)
        self.assertFalse(self.root.exists())

    def test_malformed_input_is_blocked(self):
        store = PhysicalEvidenceStore.initialize(self.root)
        self.input.write_text('{"key":1,"key":2}', encoding='utf-8')
        process, output = self.cli('append', '--store-id', store.store_id, '--input', self.input, '--apply')
        self.assertEqual(process.returncode, 2)
        self.assertEqual(output['status'], 'BLOCKED')
        self.assertEqual(store.snapshot().high_water, 0)


if __name__ == '__main__':
    unittest.main()
