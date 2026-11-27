"""Retentativa com backoff apenas para leituras idempotentes na API do Drive.

Nunca repete escritas automaticamente. Só trata erros transitórios ou limite
de taxa (403 userRateLimitExceeded/rateLimitExceeded, 429 e 5xx), mantendo
rejeições de permissão e ausência imediatas e respeitando cancelamentos.
"""
from __future__ import annotations

import json
import logging
import random
import time

from googleapiclient.errors import HttpError

RATE_LIMIT_REASONS = frozenset({
    'userRateLimitExceeded',
    'rateLimitExceeded',
    'sharingRateLimitExceeded',
})
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


def http_status_and_reasons(exc: HttpError) -> tuple[int | None, set[str]]:
    status = None
    try:
        status = int(exc.resp.status)
    except (AttributeError, TypeError, ValueError):
        status = None
    reasons: set[str] = set()
    details = getattr(exc, 'error_details', None)
    if isinstance(details, list):
        for entry in details:
            if isinstance(entry, dict) and entry.get('reason'):
                reasons.add(str(entry['reason']))
    content = getattr(exc, 'content', None)
    if content:
        try:
            raw = content.decode('utf-8', errors='replace') if isinstance(content, (bytes, bytearray)) else str(content)
            parsed = json.loads(raw)
            err = parsed.get('error', {}) if isinstance(parsed, dict) else {}
            for entry in err.get('errors', []) or []:
                if isinstance(entry, dict) and entry.get('reason'):
                    reasons.add(str(entry['reason']))
        except (ValueError, TypeError):
            pass
    return status, reasons


def is_retryable_drive_read_error(exc: Exception) -> bool:
    if not isinstance(exc, HttpError):
        return False
    status, reasons = http_status_and_reasons(exc)
    if status in RETRYABLE_STATUS_CODES:
        return True
    return status == 403 and bool(reasons & RATE_LIMIT_REASONS)


def sleep_cancellable(seconds: float, cancelled=lambda: False, sleep_fn=None, cancel_error=RuntimeError):
    if seconds <= 0:
        return
    sleeper = time.sleep if sleep_fn is None else sleep_fn
    remaining = float(seconds)
    while remaining > 0:
        if cancelled():
            raise cancel_error('Operação cancelada.')
        step = min(0.25, remaining)
        sleeper(step)
        remaining -= step


def format_wait_message(
    delay: float,
    attempt: int,
    max_attempts: int,
    *,
    status: int | None = None,
    reasons=(),
) -> str:
    seconds = max(1, int(round(delay)))
    is_rate_limit = (
        status == 429
        or (status == 403 and bool(set(reasons) & RATE_LIMIT_REASONS))
        or (status is None and not reasons)
    )
    if is_rate_limit:
        prefix = 'Google limitou as consultas'
    elif status is not None:
        prefix = f'Google Drive temporariamente indisponível (HTTP {status})'
    else:
        prefix = 'Google Drive temporariamente instável'
    return f'{prefix}; nova tentativa em {seconds}s ({attempt}/{max_attempts})...'


def execute_drive_read(
    make_request,
    *,
    cancelled=lambda: False,
    max_attempts: int = 6,
    base_delay: float = 2.0,
    max_delay: float = 32.0,
    sleep_fn=None,
    jitter_fn=None,
    cancel_error=RuntimeError,
    on_wait=None,
):
    """Executa uma requisição de leitura do Drive com backoff em rate limit / 5xx."""
    sleeper = time.sleep if sleep_fn is None else sleep_fn
    jitter = random.uniform if jitter_fn is None else jitter_fn
    for attempt in range(max_attempts):
        if attempt > 0 and cancelled():
            raise cancel_error('Operação cancelada.')
        try:
            return make_request().execute()
        except Exception as exc:
            if attempt + 1 >= max_attempts or not is_retryable_drive_read_error(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** attempt)) + max(0.0, float(jitter(0.0, 1.0)))
            status, reasons = http_status_and_reasons(exc)
            logging.warning(
                'Leitura do Drive limitada/instável (HTTP %s, motivos=%s); tentando novamente em %.1fs (%d/%d).',
                status, sorted(reasons), delay, attempt + 1, max_attempts,
            )
            if on_wait is not None:
                on_wait(
                    format_wait_message(
                        delay,
                        attempt + 1,
                        max_attempts,
                        status=status,
                        reasons=reasons,
                    )
                )
            sleep_cancellable(delay, cancelled=cancelled, sleep_fn=sleeper, cancel_error=cancel_error)
