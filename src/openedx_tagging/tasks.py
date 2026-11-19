"""Celery tasks for openedx_tagging."""

import logging
from collections.abc import Iterable
from contextlib import ExitStack
from functools import partial
from itertools import batched

from celery import shared_task  # type: ignore[import]
from django.db import transaction
from openedx_events.content_authoring.data import ContentObjectChangedData  # type: ignore[import-untyped]
from openedx_events.content_authoring.signals import CONTENT_OBJECT_ASSOCIATIONS_CHANGED  # type: ignore[import-untyped]

from openedx_tagging.models.base import ObjectTag, Tag

logger = logging.getLogger(__name__)

# Each ID becomes one event that receivers handle inside the task, so the chunk size bounds how long a task runs.
NOTIFICATION_OBJECT_IDS_CHUNK_SIZE = 500


def _emit_content_object_associations_changed_for_object_ids(object_ids: Iterable[str]) -> int:
    """
    Emit CONTENT_OBJECT_ASSOCIATIONS_CHANGED once for each distinct object ID.
    """
    emitted_events = 0
    for object_id in set(object_ids):
        # .. event_implemented_name: CONTENT_OBJECT_ASSOCIATIONS_CHANGED
        # .. event_type: org.openedx.content_authoring.content.object.associations.changed.v1
        CONTENT_OBJECT_ASSOCIATIONS_CHANGED.send_event(
            content_object=ContentObjectChangedData(
                object_id=object_id,
                changes=["tags"],
            ),
        )
        emitted_events += 1

    return emitted_events


def _emit_content_object_associations_changed_for_tag(tag: Tag) -> int:
    """
    Emit CONTENT_OBJECT_ASSOCIATIONS_CHANGED events for each content object linked to this tag
    via the ObjectTag associations. This is used to trigger downstream updates
    like search index refreshes in Meilisearch.
    """
    object_ids = ObjectTag.objects.filter(tag=tag).values_list("object_id", flat=True).distinct()
    emitted_events = _emit_content_object_associations_changed_for_object_ids(object_ids.iterator())

    logger.info(
        "Tag with id %s was updated. Emitted CONTENT_OBJECT_ASSOCIATIONS_CHANGED events for %s associated objects.",
        tag.id,
        emitted_events,
    )
    return emitted_events


@shared_task
def emit_content_object_associations_changed_for_tag_task(tag_id: int) -> int:
    """
    When a tag is updated, emit a CONTENT_OBJECT_ASSOCIATIONS_CHANGED event for every ObjectTag linked to that tag.
    Each ObjectTag represents an association between the tag and an Open edX object.
    Because downstream systems (for example, search indexes such as Meilisearch) index object-tag relationships,
    they must be notified so they can refresh the object's association data.
    """
    try:
        tag = Tag.objects.get(pk=tag_id)
    except Tag.DoesNotExist:
        logger.warning(
            "Skipping CONTENT_OBJECT_ASSOCIATIONS_CHANGED emission because tag id %s does not exist.",
            tag_id,
        )
        return 0

    return _emit_content_object_associations_changed_for_tag(tag)


@shared_task
def emit_content_object_associations_changed_for_object_ids_task(object_ids: list[str]) -> int:
    """
    Emit CONTENT_OBJECT_ASSOCIATIONS_CHANGED events for content objects whose
    tag associations changed because tags or a taxonomy were deleted or archived.
    """
    emitted_events = _emit_content_object_associations_changed_for_object_ids(object_ids)
    logger.info(
        "Tag or taxonomy deletion or archive affected %s objects. Emitted CONTENT_OBJECT_ASSOCIATIONS_CHANGED events.",
        emitted_events,
    )
    return emitted_events


def queue_content_object_associations_changed(object_ids: Iterable[str]) -> None:
    """
    Queue CONTENT_OBJECT_ASSOCIATIONS_CHANGED events for the given object IDs once the current transaction commits.

    Each distinct object ID is queued once, in tasks of at most ``NOTIFICATION_OBJECT_IDS_CHUNK_SIZE`` IDs. Every
    task is attempted even if queueing an earlier one fails. Each failure is logged, and one is then raised.
    """
    chunks = [list(chunk) for chunk in batched(sorted(set(object_ids)), NOTIFICATION_OBJECT_IDS_CHUNK_SIZE)]
    if chunks:
        transaction.on_commit(partial(_queue_object_id_chunks, chunks))


def _queue_object_id_chunks(chunks: list[list[str]]) -> None:
    """
    Queue one task for each chunk of object IDs.
    """
    # An ExitStack runs every callback even when one raises, and raises the error afterwards. It runs them last
    # in, first out, so the chunks are added in reverse.
    with ExitStack() as queue_attempts:
        for chunk in reversed(chunks):
            queue_attempts.callback(_queue_object_id_chunk, chunk)


def _queue_object_id_chunk(chunk: list[str]) -> None:
    """
    Queue one task for a chunk of object IDs, and log the chunk if it can't be queued.
    """
    try:
        emit_content_object_associations_changed_for_object_ids_task.delay(object_ids=chunk)
    except Exception:
        logger.exception(
            "Could not queue CONTENT_OBJECT_ASSOCIATIONS_CHANGED events. "
            "Chunk size: %s. First object ID: %s. Last object ID: %s.",
            len(chunk),
            chunk[0],
            chunk[-1],
        )
        raise
