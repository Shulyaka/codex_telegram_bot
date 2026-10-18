import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import (
    MAX_TELEGRAM_DOCUMENT_BYTES,
    configured_upload_dir,
    open_upload_file,
    validate_upload_file,
)


class UploadPathTests(unittest.TestCase):
    def test_configured_directory_is_optional_and_canonical(self) -> None:
        self.assertIsNone(configured_upload_dir(None))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alias = root / "alias"
            allowed = root / "uploads"
            allowed.mkdir()
            alias.symlink_to(allowed, target_is_directory=True)
            self.assertEqual(configured_upload_dir(alias), allowed.resolve())

    def test_configured_directory_must_exist_and_be_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary_file = root / "file"
            ordinary_file.write_bytes(b"data")
            for invalid in (root / "missing", ordinary_file):
                with self.subTest(path=invalid):
                    with self.assertRaisesRegex(ValueError, "CODEX_TELEGRAM_UPLOAD_DIR"):
                        configured_upload_dir(invalid)

    def test_relative_absolute_and_internal_symlink_files_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            nested = root / "nested"
            nested.mkdir()
            ordinary_file = nested / "report.txt"
            ordinary_file.write_bytes(b"report")
            alias = root / "report-alias.txt"
            alias.symlink_to(ordinary_file)
            for path in (ordinary_file, Path("nested/report.txt"), alias):
                with self.subTest(path=path):
                    self.assertEqual(validate_upload_file(path, root), ordinary_file)

    def test_traversal_sibling_and_escaping_symlink_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "uploads"
            root.mkdir()
            sibling = parent / "uploads-other"
            sibling.mkdir()
            outside = sibling / "secret.txt"
            outside.write_bytes(b"secret")
            alias = root / "escape.txt"
            alias.symlink_to(outside)
            for path in (outside, Path("../uploads-other/secret.txt"), alias):
                with self.subTest(path=path):
                    with self.assertRaisesRegex(ValueError, "outside CODEX_TELEGRAM_UPLOAD_DIR"):
                        validate_upload_file(path, root)

    def test_missing_empty_directory_and_fifo_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            empty = root / "empty"
            empty.touch()
            fifo = root / "fifo"
            os.mkfifo(fifo)
            for path in (root / "missing", empty, root, fifo):
                with self.subTest(path=path):
                    with self.assertRaises(ValueError):
                        validate_upload_file(path, root)

    def test_oversized_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            oversized = root / "large.bin"
            with oversized.open("wb") as output:
                output.truncate(MAX_TELEGRAM_DOCUMENT_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "document limit"):
                validate_upload_file(oversized, root)

    def test_safe_open_reads_and_closes_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ordinary_file = root / "report.txt"
            ordinary_file.write_bytes(b"report")
            with open_upload_file(Path("report.txt"), root) as stream:
                self.assertEqual(stream.read(), b"report")
                self.assertFalse(stream.closed)
            self.assertTrue(stream.closed)

    def test_file_symlink_swap_after_validation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "uploads"
            root.mkdir()
            ordinary_file = root / "report.txt"
            ordinary_file.write_bytes(b"report")
            outside = parent / "secret.txt"
            outside.write_bytes(b"secret")

            def swap_after_validation(path: Path, upload_dir: Path) -> Path:
                resolved = validate_upload_file(path, upload_dir)
                ordinary_file.unlink()
                ordinary_file.symlink_to(outside)
                return resolved

            with patch("bot.validate_upload_file", side_effect=swap_after_validation):
                with self.assertRaisesRegex(ValueError, "opened safely"):
                    with open_upload_file(ordinary_file, root):
                        self.fail("the swapped symlink must never be opened")

    def test_parent_symlink_swap_after_validation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "uploads"
            nested = root / "nested"
            nested.mkdir(parents=True)
            ordinary_file = nested / "report.txt"
            ordinary_file.write_bytes(b"report")
            outside = parent / "outside"
            outside.mkdir()
            (outside / ordinary_file.name).write_bytes(b"secret")

            def swap_after_validation(path: Path, upload_dir: Path) -> Path:
                resolved = validate_upload_file(path, upload_dir)
                nested.rename(root / "old-nested")
                nested.symlink_to(outside, target_is_directory=True)
                return resolved

            with patch("bot.validate_upload_file", side_effect=swap_after_validation):
                with self.assertRaisesRegex(ValueError, "opened safely"):
                    with open_upload_file(ordinary_file, root):
                        self.fail("the swapped parent must never be followed")

    def test_replaced_configured_directory_does_not_change_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            root = parent / "uploads"
            root.mkdir()
            configured_root = configured_upload_dir(root)
            self.assertIsNotNone(configured_root)
            outside = parent / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_bytes(b"secret")
            root.rename(parent / "old-uploads")
            root.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "outside CODEX_TELEGRAM_UPLOAD_DIR"):
                validate_upload_file(Path("secret.txt"), configured_root)

    def test_fifo_swap_after_validation_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            ordinary_file = root / "report.txt"
            ordinary_file.write_bytes(b"report")

            def swap_after_validation(path: Path, upload_dir: Path) -> Path:
                resolved = validate_upload_file(path, upload_dir)
                ordinary_file.unlink()
                os.mkfifo(ordinary_file)
                return resolved

            with patch("bot.validate_upload_file", side_effect=swap_after_validation):
                with self.assertRaisesRegex(ValueError, "regular file"):
                    with open_upload_file(ordinary_file, root):
                        self.fail("a FIFO must never be returned to an upload")


if __name__ == "__main__":
    unittest.main()
