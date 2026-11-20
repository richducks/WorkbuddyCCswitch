#!/usr/bin/env python3
"""Provider registry tests; only private throwaway files, no upstream calls."""
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from provider_registry import ProviderRegistry


class ProviderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "providers.json"
        self.registry = ProviderRegistry(self.path)
        self.record = dict(id="relay", name="O'Reilly 中转", url="https://relay.example.org/v1",
                           transport="chat_completions", defaultModel="vendor/a", models=["vendor/b", "vendor/b"],
                           keyEnv="RELAY_API_KEY")

    def test_export_and_persistence(self):
        row = self.registry.save(self.record, create=True)
        self.assertEqual(row["provider"], "custom:wbx-relay")
        parts = shlex.split(row["command"])
        self.assertEqual(parts[:-1], ["hermes", "config", "set", "--force", "providers.wbx-relay"])
        entry = json.loads(parts[-1])
        self.assertEqual(entry["name"], self.record["name"])
        self.assertEqual(entry["models"], ["vendor/a", "vendor/b"])
        self.assertEqual(entry["key_env"], "RELAY_API_KEY")
        self.assertNotIn("api_key", entry)
        self.assertEqual(ProviderRegistry(self.path).list(), [row])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_create_edit_delete_are_local_and_require_existing_id(self):
        self.registry.save(self.record, create=True)
        with self.assertRaises(ValueError):
            self.registry.save(self.record, create=True)
        with self.assertRaises(ValueError):
            self.registry.save({**self.record, "id": "other"}, create=False)
        row = self.registry.save({**self.record, "name": "edited"}, create=False)
        self.assertEqual(row["name"], "edited")
        self.registry.remove("relay")
        self.assertEqual(self.registry.list(), [])

    def test_rejects_secret_fields_credential_urls_and_unsafe_values(self):
        for change in ({"apiKey": "fake"}, {"url": "https://user:secret@example.org/v1"},
                       {"url": "https://example.org/v1?api_key=fake"}, {"keyEnv": "$(command)"},
                       {"id": "../workbuddy"}, {"transport": "something"}, {"url": "file:///tmp/a"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.registry.save({**self.record, **change}, create=True)
        self.assertFalse(self.path.exists())

    def test_local_ollama_and_second_protocol(self):
        local = self.registry.save({**self.record, "id": "ollama", "url": "http://127.0.0.1:11434/v1", "keyEnv": ""}, create=True)
        self.assertNotIn("key_env", local["entry"])
        other = self.registry.save({**self.record, "id": "messages", "transport": "anthropic_messages"}, create=True)
        self.assertEqual(other["entry"]["transport"], "anthropic_messages")
        self.assertEqual({row["id"] for row in self.registry.list()}, {"ollama", "messages"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
