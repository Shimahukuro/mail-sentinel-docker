import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "admin"))
import maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.components = {name: root / name for name in maintenance.COMPONENTS}
        for name, directory in self.components.items():
            directory.mkdir()
            (directory / "data.txt").write_text(name, encoding="utf-8")
        self.patch = patch.object(maintenance, "COMPONENTS", self.components)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_backup_verify_and_restore_round_trip(self):
        archive = Path(self.temporary.name) / "backup.tar.gz"
        maintenance.create(argparse.Namespace(output=str(archive)))
        maintenance.verify(argparse.Namespace(archive=str(archive)))
        for directory in self.components.values():
            (directory / "data.txt").write_text("changed", encoding="utf-8")
        maintenance.restore(argparse.Namespace(archive=str(archive), confirm="RESTORE"))
        for name, directory in self.components.items():
            self.assertEqual(name, (directory / "data.txt").read_text(encoding="utf-8"))

    def test_restore_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "--confirm RESTORE"):
            maintenance.restore(argparse.Namespace(archive="missing", confirm="no"))


if __name__ == "__main__":
    unittest.main()
