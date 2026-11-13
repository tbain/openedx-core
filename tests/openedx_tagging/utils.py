"""
Useful utilities for testing tagging and taxonomy code.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from django.db import connection
from django.db.models import Model, QuerySet


@contextmanager
def concurrent_write(model: type[Model], write: Callable[[], object]) -> Iterator[None]:
    """
    Make `write` look like another transaction committing just before a queryset delete of `model`.

    `write` runs just before each such delete, and again after each savepoint rollback. A rollback here would
    otherwise undo it, which it could not do to another transaction's commit. This relies on the test running
    inside a transaction, as `TestCase` arranges, so that each delete attempt is a savepoint. `write` has to be
    safe to run more than once.
    """
    real_delete = QuerySet.delete
    real_rollback = connection.savepoint_rollback

    def write_then_delete(queryset: QuerySet[Any]) -> tuple[int, dict[str, int]]:
        if queryset.model is model:
            with patch.object(QuerySet, "delete", real_delete):
                write()
        return real_delete(queryset)

    def rollback_then_write(sid: str) -> None:
        real_rollback(sid)
        write()

    with patch.object(QuerySet, "delete", write_then_delete):
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
