from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from herdr_orchestrator.store import Store


class SqliteLifecycleTests(unittest.TestCase):
    def test_store_closes_connection_when_setup_fails(self) -> None:
        connection = Mock()
        connection.execute.side_effect = sqlite3.OperationalError("setup failed")

        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "state.db")
            with (
                patch("herdr_orchestrator.store.sqlite3.connect", return_value=connection),
                self.assertRaisesRegex(sqlite3.OperationalError, "setup failed"),
                store._connect(),
            ):
                pass

        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
