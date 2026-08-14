import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import h5py

from server import _read_snirf_with_subject_id_compatibility


class SnirfSubjectIdCompatibilityTests(unittest.TestCase):
    def _make_snirf(self, directory: Path, subject_id: str) -> Path:
        path = directory / "recording.snirf"
        with h5py.File(path, "w") as source:
            metadata = source.create_group("nirs/metaDataTags")
            dataset = metadata.create_dataset(
                "SubjectID",
                data=subject_id,
                dtype=h5py.string_dtype(encoding="utf-8"),
            )
            dataset.attrs["preserved"] = "yes"
        return path

    def test_non_ascii_subject_id_is_preserved_and_analyzed_from_a_temp_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = self._make_snirf(Path(directory), "胡方洪")
            observed: dict[str, object] = {}

            def read_snirf(analysis_path: Path) -> list[str]:
                copied_path = Path(analysis_path)
                observed["path"] = copied_path
                with h5py.File(copied_path, "r") as copied:
                    subject_id = copied["nirs/metaDataTags/SubjectID"]
                    string_info = h5py.check_string_dtype(subject_id.dtype)
                    observed["subject_id"] = subject_id[()]
                    observed["string_length"] = string_info.length if string_info else None
                    observed["string_encoding"] = string_info.encoding if string_info else None
                    observed["attribute"] = subject_id.attrs["preserved"]
                return ["recording"]

            with patch("server.cedalion.io.read_snirf", side_effect=read_snirf):
                recordings, subject, compatibility = _read_snirf_with_subject_id_compatibility(
                    source_path,
                    "a" * 64,
                )

            self.assertEqual(recordings, ["recording"])
            self.assertEqual(subject["display_name"], "胡方洪")
            self.assertTrue(compatibility["temporary_analysis_copy_used"])
            self.assertFalse(compatibility["source_file_modified"])
            self.assertEqual(observed["subject_id"], b"subject-aaaaaaaaaaaa")
            self.assertIsNone(observed["string_length"])
            self.assertEqual(observed["string_encoding"], "ascii")
            self.assertEqual(observed["attribute"], "yes")
            self.assertFalse(Path(observed["path"]).exists())
            with h5py.File(source_path, "r") as source:
                self.assertEqual(source["nirs/metaDataTags/SubjectID"][()], "胡方洪".encode("utf-8"))

    def test_ascii_subject_id_uses_the_original_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = self._make_snirf(Path(directory), "subject-001")
            with patch("server.cedalion.io.read_snirf", return_value=["recording"]) as reader:
                _, subject, compatibility = _read_snirf_with_subject_id_compatibility(
                    source_path,
                    "b" * 64,
                )

            self.assertEqual(subject["display_name"], "subject-001")
            self.assertFalse(compatibility["temporary_analysis_copy_used"])
            self.assertEqual(reader.call_args.args[0], source_path)


if __name__ == "__main__":
    unittest.main()
