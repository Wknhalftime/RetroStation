"""Tests for FileStatus enum and LibraryFolder model."""
from uuid import uuid4

from backend.domain.enums import FileStatus
from backend.domain.library import LibraryFile, LibraryFolder


class TestFileStatus:
    def test_present_value(self) -> None:
        assert FileStatus.PRESENT == "present"

    def test_missing_value(self) -> None:
        assert FileStatus.MISSING == "missing"

    def test_deleted_value(self) -> None:
        assert FileStatus.DELETED == "deleted"

    def test_is_str_enum(self) -> None:
        assert isinstance(FileStatus.PRESENT, str)


class TestLibraryFileStatus:
    def test_default_file_status_is_present(self) -> None:
        lf = LibraryFile(
            id=uuid4(),
            file_path="/test.flac",
            file_hash="abc123",
            format="flac",
        )
        assert lf.file_status == FileStatus.PRESENT

    def test_file_status_can_be_set(self) -> None:
        lf = LibraryFile(
            id=uuid4(),
            file_path="/test.flac",
            file_hash="abc123",
            format="flac",
            file_status=FileStatus.MISSING,
        )
        assert lf.file_status == FileStatus.MISSING


class TestLibraryFolderModel:
    def test_create_folder(self) -> None:
        folder = LibraryFolder(
            id=uuid4(),
            name="jazz",
            full_path="/music/jazz",
        )
        assert folder.name == "jazz"
        assert folder.full_path == "/music/jazz"
        assert folder.parent_id is None
        assert folder.folder_hash is None
