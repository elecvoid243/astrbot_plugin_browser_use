"""Bound issued pixel records independently of later browser metadata."""

from collections import OrderedDict
from types import MappingProxyType
from uuid import uuid4

from browser_capture import IDENTITY_FIELDS

TARGET_FIELDS = (
    "tab_id",
    "document_id",
    "viewport_width",
    "viewport_height",
    "scroll_x",
    "scroll_y",
    "position_revision",
    "coordinate_token",
    "frame_id",
    "media_session_id",
    "media_epoch",
    "rtp_timestamp",
    "capture_sequence",
)


class FrameRecords:
    """Retain bounded server-issued provenance for new discrete input.

    Held gestures keep their already admitted anchor in the observer, so eviction
    cannot drop a movement sample or prevent terminal input release.
    """

    def __init__(self):
        """Initialize a bounded ledger scoped to one observer/browser lifetime."""
        self.records = OrderedDict()
        self.latest = {}

    def clear(self):
        """Revoke every issued pixel record on browser replacement or close."""
        self.records.clear()
        self.latest.clear()

    def forget_media(self, session_id):
        """Revoke this viewer's records while leaving JPEG and other peers intact.

        Args:
            session_id: Actual closed media session.
        """
        self.latest.pop(session_id, None)
        self.records = OrderedDict(
            (key, value)
            for key, value in self.records.items()
            if value.get("media_session_id") != session_id
        )

    def add(self, state, **media):
        """Issue immutable provenance without retaining image or input content.

        Args:
            state: Metadata bound to the actual captured source pixels.
            **media: Actual sent-RTP identity and decoded dimensions for video.

        Returns:
            A new flat record safe to serialize alongside its original pixels.
        """
        record = {
            name: state.get(name)
            for name in (
                *IDENTITY_FIELDS,
                "available",
                "coordinate_ready",
                "applied_input_id",
                "sequence",
                "capture_timestamp",
                "width",
                "height",
                "mime_type",
            )
        }
        record.update(media, frame_id=uuid4().hex)
        self.records[record["frame_id"]] = MappingProxyType(record)
        self.latest[record.get("media_session_id") or "jpeg"] = record["frame_id"]
        while len(self.latest) > 5:
            self.latest.pop(next(iter(self.latest)))
        while len(self.records) > 512:
            key, value = self.records.popitem(last=False)
            if key in self.latest.values():
                self.records[key] = value
        return dict(record)

    def matches(self, target, current):
        """Verify the exact issued record and its current authoritative identity.

        Args:
            target: Displayed-frame target carried by the ordered input command.
            current: Authoritatively rechecked browser state before dispatch.

        Returns:
            Whether the issued pixels still describe the authoritative coordinates.
        """
        record = self.records.get(target.get("frame_id"))
        return bool(
            record
            and current.get("coordinate_ready")
            and all(record.get(key) == target.get(key) for key in TARGET_FIELDS)
            and all(record.get(key) == current.get(key) for key in IDENTITY_FIELDS)
            and type(record.get("applied_input_id")) is int
            and type(current.get("applied_input_id")) is int
            # A lower watermark does not imply changed coordinates. Requiring a
            # new painted frame after each character breaks ordered typing and
            # shortcuts. It confirms visible feedback, never relabels these pixels.
        )
