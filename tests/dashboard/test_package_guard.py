"""The production read-only scan still flags write SQL inside the package.

``test_*.py`` is not an exemption. Spot-book fixtures live outside
``dashboard/`` so they can keep temporary SQLite statements.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import dashboard
from dashboard.package_guard import package_write_offenders


class PackageGuardTests(unittest.TestCase):
    def test_shipped_dashboard_package_stays_clean(self) -> None:
        root = Path(dashboard.__file__).resolve().parent
        self.assertEqual(package_write_offenders(root), [])
        self.assertEqual(list(root.rglob("test_*.py")), [])
        data = (root / "data.py").read_text(encoding="utf-8")
        self.assertIn("mode=ro", data)
        self.assertIn("query_only=ON", data)

    def test_production_filename_with_write_sql_is_still_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "panel.py").write_text(
                "def bad():\n    sql = 'INSERT INTO positions VALUES (1)'\n",
                encoding="utf-8",
            )
            self.assertEqual(
                package_write_offenders(root),
                ["write sql in panel.py"],
            )

    def test_test_filename_inside_the_tree_is_not_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test_fixture.py").write_text(
                "import sqlite3\n"
                "def build():\n"
                "    conn = sqlite3.connect(':memory:')\n"
                "    conn.execute('CREATE TABLE positions (id INTEGER)')\n",
                encoding="utf-8",
            )
            hits = package_write_offenders(root)
            self.assertIn("write sql in test_fixture.py", hits)
            self.assertIn("sqlite connect in test_fixture.py", hits)

    def test_sessions_and_data_exceptions_stay_narrow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sessions.py").write_text(
                "import sqlite3\n"
                "def open_db(path):\n"
                "    return sqlite3.connect(path)\n"
                "def init(conn):\n"
                "    conn.execute('CREATE TABLE chat_sessions (id TEXT)')\n",
                encoding="utf-8",
            )
            (root / "data.py").write_text(
                "import sqlite3\n"
                "def connect(path):\n"
                "    return sqlite3.connect(path)\n",
                encoding="utf-8",
            )
            (root / "other.py").write_text(
                "import sqlite3\n"
                "sqlite3.connect('x')\n",
                encoding="utf-8",
            )
            self.assertEqual(
                package_write_offenders(root),
                ["sqlite connect in other.py"],
            )


if __name__ == "__main__":
    unittest.main()
