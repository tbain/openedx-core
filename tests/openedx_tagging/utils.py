"""
Useful utilities for testing tagging and taxonomy code.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

from django.db import connection
from django.db.models import Model, QuerySet

# Patch target for the task call that queues CONTENT_OBJECT_ASSOCIATIONS_CHANGED events for a list of object ids.
OBJECT_IDS_TASK_PATCH_TARGET = (
    "openedx_tagging.tasks.emit_content_object_associations_changed_for_object_ids_task.delay"
)


def queued_object_id_chunks(mock_delay: MagicMock) -> list[list[str]]:
    """
    Return the object ids passed to each call of the patched `OBJECT_IDS_TASK_PATCH_TARGET`, in call order.
    """
    return [call.kwargs["object_ids"] for call in mock_delay.call_args_list]


@contextmanager
def concurrent_write(model: type[Model], write: Callable[[], object], before: str = "delete") -> Iterator[None]:
    """
    Make `write` look like another transaction committing just before a queryset delete or update of `model`.

    `before` names the queryset method, "delete" or "update". `write` runs just before each such call, and again
    after each savepoint rollback. A rollback here would otherwise undo it, which it could not do to another
    transaction's commit. This relies on the test running inside a transaction, as `TestCase` arranges, so that
    each attempt is a savepoint. `write` has to be safe to run more than once.
    """
    real_method = getattr(QuerySet, before)
    real_rollback = connection.savepoint_rollback

    def write_then_call(queryset: QuerySet[Any], **kwargs: Any) -> Any:
        if queryset.model is model:
            with patch.object(QuerySet, before, real_method):
                write()
        return real_method(queryset, **kwargs)

    def rollback_then_write(sid: str) -> None:
        real_rollback(sid)
        write()

    with patch.object(QuerySet, before, write_then_call):
        with patch.object(connection, "savepoint_rollback", rollback_then_write):
            yield


def pretty_format_tags(result, parent=True, external_id=False) -> list[str]:
    """
    Format the result of get_filtered_tags() to be more human readable.

    Also works with other wrappers around get_filtered_tags, like api.get_tags()
    Also works with serialized TagData from the REST API.
    """
    pretty_results = []
    for t in result:
        line = f"{t['depth'] * '  '}{t['value']} "
        if external_id:
            line += f"({t['external_id']}) "
        if parent:
            line += f"({t['parent_value']}) "
        line += "("
        if "usage_count" in t:
            line += f"used: {t['usage_count']}, "
        line += f"children: {t['child_count']}"
        line += ")"
        pretty_results.append(line)
    return pretty_results
