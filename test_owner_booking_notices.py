"""Compatibility runner for the owner notice regression suite.

The former AST-only tests are now integration tests against the real SQLite
schema, registration trigger and mocked LINE/SMTP transports.
"""
import unittest
from pathlib import Path


def load_tests(loader, tests, pattern):
    return loader.discover(str(Path(__file__).parent / 'tests'), pattern='test_owner_notifications.py')


if __name__ == '__main__':
    unittest.main()
