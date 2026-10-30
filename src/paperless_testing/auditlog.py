"""Test-local isolation for in-process calls to the one-shot import command."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy

from django.apps import apps
from django.db.models.signals import m2m_changed


@contextmanager
def preserve_auditlog_registry() -> Iterator[None]:
    """Restore exact model options and connected receivers, including disabled ones."""
    if not apps.is_installed("auditlog"):
        yield
        return

    from auditlog.registry import auditlog

    registered = deepcopy(auditlog._registry)
    m2m_receivers = {
        model: dict(receivers) for model, receivers in auditlog._m2m_signals.items()
    }
    signals = set(auditlog._signals) | {m2m_changed}

    def owned_receiver(entry):
        dispatch_uid = entry[0][0]
        return (
            isinstance(dispatch_uid, tuple)
            and bool(dispatch_uid)
            and dispatch_uid[0] == id(auditlog)
        )

    connected = {}
    for signal in signals:
        with signal.lock:
            connected[signal] = [
                (position, entry)
                for position, entry in enumerate(signal.receivers)
                if owned_receiver(entry)
            ]
    try:
        yield
    finally:
        # Keep original m2m closures alive and restore their actual connections;
        # register() would create replacements and reapply global option defaults.
        auditlog._registry.clear()
        auditlog._registry.update(registered)
        auditlog._m2m_signals.clear()
        auditlog._m2m_signals.update(m2m_receivers)
        for signal in signals:
            with signal.lock:
                signal.receivers[:] = [
                    entry for entry in signal.receivers if not owned_receiver(entry)
                ]
                for position, entry in connected[signal]:
                    signal.receivers.insert(min(position, len(signal.receivers)), entry)
                signal.sender_receivers_cache.clear()
