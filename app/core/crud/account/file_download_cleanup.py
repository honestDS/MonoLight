import errno
import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

_DATABASE_NAME = ".cleanup.sqlite3"
_TABLE_NAME = "file_download_cleanup"


class FileDownloadCleanupJournal:
    def __init__(self, token_dir: str | Path) -> None:
        self._token_dir = Path(token_dir)
        self._database_path = self._token_dir / _DATABASE_NAME

    @staticmethod
    def _is_link(path_stat: os.stat_result) -> bool:
        return stat.S_ISLNK(path_stat.st_mode) or bool(getattr(path_stat, "st_reparse_tag", 0))

    @staticmethod
    def _reject_link(path: Path) -> None:
        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), os.fspath(path))

    @classmethod
    def _validate_token_dir(cls, path: Path, path_stat: os.stat_result) -> None:
        if cls._is_link(path_stat):
            cls._reject_link(path)
        if not stat.S_ISDIR(path_stat.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), os.fspath(path))

    @classmethod
    def _validate_database(cls, path: Path, path_stat: os.stat_result) -> None:
        if cls._is_link(path_stat):
            cls._reject_link(path)
        if stat.S_ISREG(path_stat.st_mode):
            return
        if stat.S_ISDIR(path_stat.st_mode):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), os.fspath(path))
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), os.fspath(path))

    def _storage_ready(self, *, create: bool) -> bool:
        try:
            token_dir_stat = os.lstat(self._token_dir)
        except FileNotFoundError:
            if not create:
                return False
            self._token_dir.mkdir(parents=True, exist_ok=True)
            token_dir_stat = os.lstat(self._token_dir)
        self._validate_token_dir(self._token_dir, token_dir_stat)

        try:
            database_stat = os.lstat(self._database_path)
        except FileNotFoundError:
            return create
        self._validate_database(self._database_path, database_stat)
        return True

    @staticmethod
    def _absolute_source_path(path: Path) -> Path:
        return Path(os.path.abspath(os.fspath(path)))

    @staticmethod
    def _source_key(path: Path) -> str:
        return os.path.normcase(os.fspath(path))

    @staticmethod
    def _source_identity(source_stat: os.stat_result, *, is_tree: bool = False) -> str:
        birthtime_ns = getattr(source_stat, "st_birthtime_ns", None)
        if is_tree:
            birthtime_ns = birthtime_ns or 0
        elif birthtime_ns is None:
            birthtime_ns = source_stat.st_ctime_ns
        return f"{int(source_stat.st_dev)}:{int(source_stat.st_ino)}:{int(birthtime_ns)}"

    @staticmethod
    def _same_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
        for attribute in ("st_mode", "st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns", "st_size"):
            if getattr(first, attribute) != getattr(second, attribute):
                return False
        return True

    @classmethod
    def _current_identity(cls, path: Path, *, is_tree: bool = False) -> tuple[str, os.stat_result] | None:
        try:
            source_stat = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            return None
        return cls._source_identity(source_stat, is_tree=is_tree), source_stat

    @staticmethod
    def _prepare_row(
        connection: sqlite3.Connection,
        source_path: Path,
        source_identity: str,
        *,
        is_tree: bool,
    ) -> int:
        connection.execute("BEGIN IMMEDIATE")
        cutoff_ns = time.time_ns()
        cursor = connection.execute(
            f"""
            INSERT INTO {_TABLE_NAME}
                (source_path, source_key, cutoff_ns, source_identity, confirmed, is_tree)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (str(source_path), os.path.normcase(os.fspath(source_path)), cutoff_ns, source_identity, int(is_tree)),
        )
        connection.commit()
        return int(cursor.lastrowid)

    @staticmethod
    def _record_prepared_result(connection: sqlite3.Connection, row_id: int, *, confirmed: bool) -> None:
        if confirmed:
            connection.execute(f"UPDATE {_TABLE_NAME} SET confirmed = 1 WHERE id = ?", (row_id,))
        else:
            connection.execute(f"DELETE FROM {_TABLE_NAME} WHERE id = ?", (row_id,))
        connection.commit()

    def _recover_prepared(
        self,
        connection: sqlite3.Connection,
        row_id: int,
        source_path: Path,
        source_identity: str,
        is_tree: bool,
    ) -> bool | None:
        try:
            current = self._current_identity(source_path, is_tree=is_tree)
        except OSError:
            return None
        same_source = current is not None and current[0] == source_identity
        if same_source and not is_tree:
            connection.execute(f"DELETE FROM {_TABLE_NAME} WHERE id = ?", (row_id,))
        if same_source:
            return False
        connection.execute(f"UPDATE {_TABLE_NAME} SET confirmed = 1 WHERE id = ?", (row_id,))
        return True

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            with closing(sqlite3.connect(str(self._database_path), timeout=1.0, isolation_level=None)) as connection:
                yield connection
        except sqlite3.Error as exc:
            raise OSError(errno.EIO, f"cleanup journal database error: {exc}", os.fspath(self._database_path)) from exc

    @staticmethod
    def _has_schema(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_TABLE_NAME,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _initialise_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                source_key TEXT NOT NULL,
                cutoff_ns INTEGER NOT NULL,
                source_identity TEXT,
                confirmed INTEGER NOT NULL CHECK (confirmed IN (0, 1)),
                is_tree INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{_TABLE_NAME}_source_cutoff
            ON {_TABLE_NAME} (source_key, cutoff_ns)
            """
        )

    @staticmethod
    def _path_keys(path: Path) -> list[str]:
        keys: list[str] = []
        current = path
        while True:
            key = os.path.normcase(os.fspath(current))
            if key not in keys:
                keys.append(key)
            parent = current.parent
            if parent == current:
                return keys
            current = parent

    @staticmethod
    def _rows_for_keys(
        connection: sqlite3.Connection,
        keys: list[str],
        *,
        cutoff_ns: int | None = None,
    ) -> list[tuple]:
        placeholders = ", ".join("?" for _ in keys)
        query = f"""
            SELECT source_path, source_key, cutoff_ns, source_identity, confirmed, is_tree
            FROM {_TABLE_NAME}
            WHERE source_key IN ({placeholders})
        """
        parameters: tuple[object, ...] = tuple(keys)
        if cutoff_ns is not None:
            query += " AND cutoff_ns >= ?"
            parameters += (cutoff_ns,)
        query += " ORDER BY id"
        return connection.execute(query, parameters).fetchall()

    def unlink_file(self, path: Path, *, expected_stat: os.stat_result | None = None) -> None:
        self._storage_ready(create=True)
        source_path = self._absolute_source_path(path)
        source_stat = os.lstat(source_path)
        source_identity = self._source_identity(source_stat)
        if expected_stat is not None and not self._same_snapshot(source_stat, expected_stat):
            raise OSError(errno.EAGAIN, "source file changed during cleanup", os.fspath(source_path))

        with self._connection() as connection:
            self._initialise_schema(connection)
            row_id = self._prepare_row(connection, source_path, source_identity, is_tree=False)

            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT confirmed FROM {_TABLE_NAME} WHERE id = ?",
                (row_id,),
            ).fetchone()
            if row is None or row[0]:
                connection.commit()
                raise BlockingIOError(errno.EAGAIN, "cleanup preparation was claimed by another worker", os.fspath(source_path))

            try:
                current = self._current_identity(source_path)
            except OSError:
                self._record_prepared_result(connection, row_id, confirmed=False)
                raise

            if current is None:
                self._record_prepared_result(connection, row_id, confirmed=True)
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), os.fspath(source_path))
            if not self._same_snapshot(source_stat, current[1]):
                self._record_prepared_result(connection, row_id, confirmed=False)
                raise OSError(errno.EAGAIN, "source file changed during cleanup", os.fspath(source_path))

            try:
                source_path.unlink()
            except FileNotFoundError:
                self._record_prepared_result(connection, row_id, confirmed=True)
                raise
            except OSError:
                self._record_prepared_result(connection, row_id, confirmed=False)
                raise

            self._record_prepared_result(connection, row_id, confirmed=True)

    def remove_tree(self, path: Path) -> None:
        self._storage_ready(create=True)
        source_path = self._absolute_source_path(path)
        source_stat = os.lstat(source_path)
        self._validate_token_dir(source_path, source_stat)
        source_identity = self._source_identity(source_stat, is_tree=True)

        with self._connection() as connection:
            self._initialise_schema(connection)
            row_id = self._prepare_row(connection, source_path, source_identity, is_tree=True)
            try:
                current = self._current_identity(source_path, is_tree=True)
            except OSError:
                connection.execute("BEGIN IMMEDIATE")
                self._record_prepared_result(connection, row_id, confirmed=False)
                raise
            if current is None:
                connection.execute("BEGIN IMMEDIATE")
                self._record_prepared_result(connection, row_id, confirmed=True)
                return
            if not self._same_snapshot(source_stat, current[1]):
                connection.execute("BEGIN IMMEDIATE")
                self._record_prepared_result(connection, row_id, confirmed=False)
                raise OSError(errno.EAGAIN, "source file changed during cleanup", os.fspath(source_path))
            try:
                shutil.rmtree(source_path)
            except FileNotFoundError:
                connection.execute("BEGIN IMMEDIATE")
                self._record_prepared_result(connection, row_id, confirmed=True)
                return
            connection.execute("BEGIN IMMEDIATE")
            self._record_prepared_result(connection, row_id, confirmed=True)

    def note_missing_file(self, path: Path, cutoff_ns: int) -> None:
        self._storage_ready(create=True)
        source_path = self._absolute_source_path(path)
        with self._connection() as connection:
            self._initialise_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                INSERT INTO {_TABLE_NAME}
                    (source_path, source_key, cutoff_ns, source_identity, confirmed, is_tree)
                VALUES (?, ?, ?, NULL, 1, 0)
                """,
                (str(source_path), self._source_key(source_path), cutoff_ns),
            )
            connection.commit()

    def snapshot_pending(self) -> tuple[list[int], dict[Path, int], dict[Path, int]]:
        if not self._storage_ready(create=False):
            return [], {}, {}

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._has_schema(connection):
                connection.commit()
                return [], {}, {}

            rows = connection.execute(
                f"""
                SELECT id, source_path, cutoff_ns, source_identity, confirmed, is_tree
                FROM {_TABLE_NAME}
                ORDER BY id
                """
            ).fetchall()
            confirmed_ids: list[int] = []
            file_cutoffs: dict[Path, int] = {}
            tree_cutoffs: dict[Path, int] = {}

            for row_id, source_path_value, cutoff_ns, source_identity, confirmed, is_tree in rows:
                source_path = Path(source_path_value)
                should_confirm: bool | None = bool(confirmed)
                if not should_confirm:
                    should_confirm = self._recover_prepared(connection, row_id, source_path, source_identity, bool(is_tree))
                if should_confirm is None or not should_confirm:
                    continue

                confirmed_ids.append(int(row_id))
                cutoffs = tree_cutoffs if is_tree else file_cutoffs
                previous_cutoff = cutoffs.get(source_path)
                if previous_cutoff is None or cutoff_ns > previous_cutoff:
                    cutoffs[source_path] = cutoff_ns

            connection.commit()
            return confirmed_ids, file_cutoffs, tree_cutoffs

    def acknowledge(self, ids: list[int]) -> None:
        if not ids or not self._storage_ready(create=False):
            return

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._has_schema(connection):
                connection.commit()
                return
            connection.executemany(
                f"DELETE FROM {_TABLE_NAME} WHERE id = ?",
                ((row_id,) for row_id in ids),
            )
            connection.commit()

    def revokes(self, source_path: Path, issued_at_ns: int, source_stat: os.stat_result) -> bool:
        if not self._storage_ready(create=False):
            return False

        source_path = self._absolute_source_path(source_path)
        source_key = self._source_key(source_path)
        with self._connection() as connection:
            if not self._has_schema(connection):
                return False
            rows = self._rows_for_keys(connection, self._path_keys(source_path), cutoff_ns=issued_at_ns)

        if not rows:
            return False

        current_identity: str | None = None
        for _, row_source_key, _, source_identity, confirmed, is_tree in rows:
            is_tree = bool(is_tree)
            is_exact = row_source_key == source_key
            if not is_tree and not is_exact:
                continue
            if confirmed:
                return True
            if is_tree:
                return True
            if current_identity is None:
                current_identity = self._source_identity(source_stat)
            if source_identity != current_identity:
                return True
        return False

    @contextmanager
    def publication_guard(self, source_path: Path, source_stat: os.stat_result) -> Iterator[None]:
        self._storage_ready(create=True)
        source_path = self._absolute_source_path(source_path)
        source_key = self._source_key(source_path)
        source_identity: str | None = None
        with self._connection() as connection:
            self._initialise_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            rows = self._rows_for_keys(connection, self._path_keys(source_path))
            for row_source_path, row_source_key, _, row_identity, confirmed, is_tree in rows:
                is_tree = bool(is_tree)
                is_exact = row_source_key == source_key
                if not is_tree and not is_exact:
                    continue
                if confirmed:
                    continue
                if not is_tree and source_identity is None:
                    source_identity = self._source_identity(source_stat)
                if not is_tree and row_identity == source_identity:
                    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), os.fspath(source_path))
                if not is_tree:
                    continue
                try:
                    current = self._current_identity(Path(row_source_path), is_tree=True)
                except OSError:
                    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), os.fspath(source_path))
                if current is None or current[0] != row_identity:
                    continue
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), os.fspath(source_path))
            try:
                yield
            finally:
                connection.commit()
