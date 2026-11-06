"""Pixel provenance and local sent-RTP adapter contracts."""

import io
import struct
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock

import av
import pytest
from PIL import Image

import media_rtp
from frame_records import TARGET_FIELDS, FrameRecords
from media_rtp import RtpFrameAdapter
from media_track import BrowserVideoTrack


@pytest.fixture
def source_state():
    """Provide one actual-contract-shaped source, distinct from current SSE state.

    Returns:
        Flat captured identity with a processed-input lower bound.
    """
    return {
        "generation": "generation",
        "browser_session_id": "browser",
        "available": True,
        "tab_id": "tab",
        "document_id": "document",
        "url": "http://fixture/",
        "viewport_width": 1366,
        "viewport_height": 768,
        "scroll_x": 0,
        "scroll_y": 0,
        "position_revision": 0,
        "control_epoch": 1,
        "coordinate_contract_version": 2,
        "coordinate_token": "a" * 64,
        "frame_tree_token": "b" * 64,
        "coordinate_event_revision": 0,
        "coordinate_ready": True,
        "applied_input_id": 0,
        "width": 1920,
        "height": 1080,
    }


def test_issued_record_rejects_omission_and_relabel_without_losing_ordered_typing(
    source_state,
):
    """Missing provenance cannot turn current metadata into old-pixel authority.

    Args:
        source_state: Original captured source identity.
    """
    ledger = FrameRecords()
    record = ledger.add(source_state)
    target = {key: record.get(key) for key in TARGET_FIELDS}
    assert ledger.matches(target, source_state)
    assert not ledger.matches(
        {key: value for key, value in target.items() if key != "frame_id"}, source_state
    )
    assert not ledger.matches(target, {**source_state, "coordinate_token": "c" * 64})
    assert ledger.matches(target, {**source_state, "applied_input_id": 1})
    assert ledger.records[record["frame_id"]]["applied_input_id"] == 0
    source_state["scroll_y"] = 120
    assert ledger.records[record["frame_id"]]["scroll_y"] == 0
    assert not ledger.matches(target, source_state)


def test_video_dimensions_and_clock_are_part_of_the_issued_record(source_state):
    """A real video raster must not inherit scaled-snapshot dimensions or a new clock.

    Args:
        source_state: Captured CSS viewport and scaled JPEG dimensions.
    """
    ledger = FrameRecords()
    record = ledger.add(
        source_state,
        width=1366,
        height=768,
        media_session_id="d" * 32,
        media_epoch=1,
        rtp_timestamp=0xFFFFFFFF,
        capture_sequence=9,
    )
    assert (record["width"], record["height"]) == (1366, 768)
    target = {key: record.get(key) for key in TARGET_FIELDS}
    assert ledger.matches(target, source_state)
    assert not ledger.matches({**target, "rtp_timestamp": 0}, source_state)
    assert not ledger.matches(
        {key: value for key, value in target.items() if key != "media_session_id"},
        source_state,
    )
    ledger.forget_media("d" * 32)
    assert not ledger.matches(target, source_state)


def test_static_video_record_survives_other_viewer_activity(source_state):
    """Bound history while keeping the actual last static frame usable.

    Args:
        source_state: Stable source shared by the admitted viewers.
    """
    ledger = FrameRecords()
    video = ledger.add(
        source_state,
        media_session_id="d" * 32,
        media_epoch=1,
        rtp_timestamp=10,
        capture_sequence=1,
    )
    old_jpeg = ledger.add(source_state)
    for _ in range(600):
        ledger.add(source_state)
    assert len(ledger.records) == 512
    assert ledger.matches({key: video.get(key) for key in TARGET_FIELDS}, source_state)
    assert not ledger.matches(
        {key: old_jpeg.get(key) for key in TARGET_FIELDS}, source_state
    )
    ledger.clear()
    assert not ledger.records and not ledger.latest


@pytest.mark.parametrize("size", [(1366, 768), (1365, 767)])
async def test_each_encoder_gets_independent_native_pixels(size):
    """Copy real decoder strides without sharing mutable intra-frame flags.

    Args:
        size: Even or odd native raster geometry.
    """
    data = io.BytesIO()
    Image.new("RGB", size, (20, 120, 220)).save(data, format="JPEG", quality=90)
    with av.open(io.BytesIO(data.getvalue()), format="mjpeg") as container:
        original = next(container.decode(video=0))
    original.pts = 1234
    original.time_base = Fraction(1, 90000)
    original.pict_type = av.video.frame.PictureType.NONE
    first = BrowserVideoTrack(lambda epoch: epoch == 1)
    second = BrowserVideoTrack(lambda epoch: epoch == 1)
    first.push(original, {"media_epoch": 1})
    second.push(original, {"media_epoch": 1})
    a = await first.recv()
    b = await second.recv()
    a.pict_type = av.video.frame.PictureType.I
    assert b.pict_type == original.pict_type == av.video.frame.PictureType.NONE
    assert a is not b and b is not original
    assert (
        a.to_image().tobytes()
        == original.to_image().tobytes()
        == b.to_image().tobytes()
    )
    assert first.current["width"] == size[0] and first.current["height"] == size[1]
    assert a.pts == 1234 and a.time_base == Fraction(1, 90000)
    first.stop()
    second.stop()


async def test_actual_sent_dynamic_payload_and_timestamp_wrap():
    """Map real packet headers after send, including ordinary unsigned wrap."""
    order = []
    mapped = []

    async def sent(data):
        """Record the real owned transport send before publishing metadata.

        Args:
            data: Native packet bytes.
        """
        order.append("sent")

    sender = SimpleNamespace(
        _next_encoded_frame=AsyncMock(return_value=SimpleNamespace(timestamp=10)),
        _RTCRtpSender__encoder=None,
        _ssrc=123,
        transport=SimpleNamespace(_send_rtp=sent),
    )
    track = SimpleNamespace(
        current={"width": 1366, "height": 768, "source": "original"}
    )
    adapter = RtpFrameAdapter(
        sender,
        track,
        lambda value: (mapped.append(value), order.append("mapped")),
        pytest.fail,
    )
    codec = SimpleNamespace(mimeType="video/VP8", payloadType=117)
    await sender._next_encoded_frame(codec)
    await sender.transport._send_rtp(
        struct.pack("!BBHII", 0x80, 117, 1, 0xFFFFFFF0, 123) + b"first"
    )
    assert not mapped
    await sender.transport._send_rtp(
        struct.pack("!BBHII", 0x80, 117 | 128, 2, 0xFFFFFFF0, 123) + b"last"
    )
    assert mapped[0]["rtp_timestamp"] == 0xFFFFFFF0 and order[-2:] == ["sent", "mapped"]
    sender._next_encoded_frame = adapter.next_frame
    adapter.original_next.return_value = SimpleNamespace(timestamp=50)
    await sender._next_encoded_frame(codec)
    await sender.transport._send_rtp(
        struct.pack("!BBHII", 0x80, 117 | 128, 3, 24, 123) + b"wrap"
    )
    assert [value["rtp_timestamp"] for value in mapped] == [0xFFFFFFF0, 24]
    assert [value["media_sequence"] for value in mapped] == [1, 2]
    assert mapped[1]["source"] == "original"
    adapter.restore()


def test_unknown_adapter_version_fails_before_changing_sender(monkeypatch):
    """An unverified private interface must fall back rather than invent mappings.

    Args:
        monkeypatch: Isolate the reported optional dependency version.
    """
    monkeypatch.setattr(media_rtp, "version", lambda name: "unknown")
    original = AsyncMock()
    sender = SimpleNamespace(_next_encoded_frame=original)
    with pytest.raises(RuntimeError, match="verified aiortc"):
        RtpFrameAdapter(sender, None, None, None)
    assert sender._next_encoded_frame is original
