"""Configuracao centralizada e politica de retencao dos logs do VoxImago."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sys
import time
from datetime import datetime
from logging.handlers import BaseRotatingHandler
from pathlib import Path


DEFAULT_RETENTION_DAYS = 45
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
_MANAGED_ARCHIVE_PATTERNS = (
    "voximago-*.log.gz",
    "voximago-legacy-*.log",
    "voximago-legacy-*.log.gz",
)


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def _gzip_file(source: Path, destination: Path) -> None:
    """Comprime de forma atomica e preserva a data do arquivo de origem."""
    source_mtime = source.stat().st_mtime
    temporary = destination.with_name(f"{destination.name}.tmp")
    try:
        with source.open("rb") as source_file, gzip.open(temporary, "wb") as target_file:
            shutil.copyfileobj(source_file, target_file, length=1024 * 1024)
        os.replace(temporary, destination)
        os.utime(destination, (source_mtime, source_mtime))
        source.unlink()
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def cleanup_expired_logs(
    archive_dir: str | os.PathLike[str],
    retention_days: int = DEFAULT_RETENTION_DAYS,
    *,
    now: float | None = None,
) -> list[Path]:
    """Remove somente arquivos historicos gerenciados que excederam a retencao."""
    if retention_days < 1:
        raise ValueError("retention_days deve ser maior ou igual a 1")

    directory = Path(archive_dir)
    if not directory.exists():
        return []

    cutoff = (time.time() if now is None else now) - (retention_days * 24 * 60 * 60)
    removed: list[Path] = []
    candidates: set[Path] = set()
    for pattern in _MANAGED_ARCHIVE_PATTERNS:
        candidates.update(directory.glob(pattern))

    for path in candidates:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return sorted(removed)


def archive_legacy_log(
    base_dir: str | os.PathLike[str],
    archive_dir: str | os.PathLike[str],
) -> Path | None:
    """Move o antigo ``app.log`` para o historico sem reescrever seu conteudo."""
    legacy_path = Path(base_dir) / "app.log"
    if not legacy_path.is_file():
        return None

    destination_dir = Path(archive_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    modified_at = datetime.fromtimestamp(legacy_path.stat().st_mtime)
    stem = f"voximago-legacy-{modified_at:%Y%m%d-%H%M%S}"
    destination = _unique_path(destination_dir, stem, ".log")
    os.replace(legacy_path, destination)
    return destination


class AgeAndSizeRotatingFileHandler(BaseRotatingHandler):
    """Rotaciona por dia ou tamanho e expira arquivos pela idade."""

    def __init__(
        self,
        filename: str | os.PathLike[str],
        archive_dir: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        encoding: str = "utf-8",
        delay: bool = True,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes deve ser maior ou igual a 1")
        if retention_days < 1:
            raise ValueError("retention_days deve ser maior ou igual a 1")

        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.retention_days = retention_days
        super().__init__(str(filename), mode="a", encoding=encoding, delay=delay)

        active_path = Path(self.baseFilename)
        if active_path.is_file() and active_path.stat().st_size:
            self._active_date = datetime.fromtimestamp(active_path.stat().st_mtime).date()
        else:
            self._active_date = datetime.now().date()

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        active_path = Path(self.baseFilename)
        current_date = datetime.now().date()

        if active_path.is_file() and active_path.stat().st_size:
            if current_date != self._active_date:
                return True

            message = f"{self.format(record)}{self.terminator}"
            encoded_size = len(message.encode(self.encoding or "utf-8", errors="replace"))
            if active_path.stat().st_size + encoded_size >= self.max_bytes:
                return True
        else:
            self._active_date = current_date
        return False

    def doRollover(self) -> None:  # noqa: N802
        if self.stream:
            self.stream.close()
            self.stream = None

        active_path = Path(self.baseFilename)
        if active_path.is_file() and active_path.stat().st_size:
            source_mtime = active_path.stat().st_mtime
            timestamp = datetime.fromtimestamp(source_mtime).strftime("%Y%m%d-%H%M%S-%f")
            compressed = _unique_path(self.archive_dir, f"voximago-{timestamp}", ".log.gz")
            try:
                _gzip_file(active_path, compressed)
            except Exception as compression_error:
                fallback = _unique_path(
                    self.archive_dir, f"voximago-legacy-{timestamp}", ".log"
                )
                try:
                    os.replace(active_path, fallback)
                    os.utime(fallback, (source_mtime, source_mtime))
                except OSError:
                    sys.stderr.write(
                        "VoxImago: nao foi possivel rotacionar o log: "
                        f"{compression_error}\n"
                    )

        self._active_date = datetime.now().date()
        cleanup_expired_logs(self.archive_dir, self.retention_days)
        if not self.delay:
            self.stream = self._open()


def _resolve_level(value: str | int | None) -> int:
    if isinstance(value, int):
        return value
    name = (value or os.getenv("VOXIMAGO_LOG_LEVEL", "INFO")).strip().upper()
    level = getattr(logging, name, None)
    return level if isinstance(level, int) else logging.INFO


def configure_logging(
    *,
    base_dir: str | os.PathLike[str] | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    level: str | int | None = None,
) -> Path:
    """Instala os handlers do aplicativo e devolve o caminho do log ativo."""
    project_dir = (
        Path(base_dir).resolve()
        if base_dir is not None
        else Path(__file__).resolve().parents[2]
    )
    log_dir = project_dir / "logs"
    archive_dir = log_dir / "archive"
    log_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    migration_error: OSError | None = None
    migrated_path: Path | None = None
    try:
        migrated_path = archive_legacy_log(project_dir, archive_dir)
    except OSError as exc:
        migration_error = exc

    removed = cleanup_expired_logs(archive_dir, retention_days)
    active_path = log_dir / "voximago.log"
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = AgeAndSizeRotatingFileHandler(
        active_path,
        archive_dir,
        max_bytes=max_bytes,
        retention_days=retention_days,
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout or sys.stderr)
    console_handler.setFormatter(formatter)

    resolved_level = _resolve_level(level)
    root_logger.setLevel(resolved_level)
    file_handler.setLevel(resolved_level)
    console_handler.setLevel(resolved_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    logging.captureWarnings(True)

    if migrated_path:
        logging.info("Log legado arquivado: %s", migrated_path)
    if migration_error:
        logging.warning("Nao foi possivel arquivar app.log nesta inicializacao: %s", migration_error)
    if removed:
        logging.info("Retencao de logs removeu %d arquivo(s) com mais de %d dias.", len(removed), retention_days)

    return active_path
