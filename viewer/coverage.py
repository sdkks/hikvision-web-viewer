"""Aggregate a day's recordings into coverage buckets for the picker.

The UI shows a day as 24 hour tiles (shaded by coverage) and an hour as 60
minute cells, then lets the user start playback at a covered minute. Everything
here is pure computation over clips already returned by the camera search, so
the map and the range rows can never disagree.
"""

from datetime import datetime, timedelta

from . import util
from .camera import Clip

# Coverage levels: 0 none, 1 sparse, 2 about half, 3 dense, 4 full.
MINUTE_COVERED = 4


def _level(covered_minutes: int, total_minutes: int = 60) -> int:
    if covered_minutes <= 0:
        return 0
    if covered_minutes >= total_minutes:
        return 4
    fraction = covered_minutes / total_minutes
    if fraction < 0.25:
        return 1
    if fraction < 0.60:
        return 2
    return 3


def neighbours(files: list[dict], current_from: int, current_to: int, limit: int = 5) -> dict:
    """Recordings before/after the one being watched, nearest first.

    Each entry carries `gap_s`: how much time is skipped when jumping there, so
    a jump across a gap can never look continuous.
    """
    before = [f for f in files if f["end_epoch"] <= current_from]
    after = [f for f in files if f["start_epoch"] >= current_to]
    earlier = [
        {**f, "gap_s": max(0, current_from - f["end_epoch"])} for f in reversed(before[-limit:])
    ]
    later = [{**f, "gap_s": max(0, f["start_epoch"] - current_to)} for f in after[:limit]]
    return {
        "earlier": earlier,
        "later": later,
        "more_earlier": len(before) > limit,
        "more_later": len(after) > limit,
    }


def build_coverage(clips: list[Clip], day_start: datetime) -> dict:
    """Build the coverage map for one local day.

    Args:
        clips: camera.Clip objects (aware UTC datetimes).
        day_start: aware datetime of local midnight for the requested day.

    Returns a dict with hours/hour_clips/minutes levels and merged ranges
    (epoch seconds, UTC) ready for JSON.
    """
    day_end = day_start + timedelta(days=1)
    minutes = [[0] * 60 for _ in range(24)]
    hour_clips = [0] * 24
    spans: list[tuple[datetime, datetime, int]] = []

    for clip in clips:
        start = max(clip.start_dt.astimezone(), day_start)
        end = min(clip.end_dt.astimezone(), day_end)
        if end <= start:
            continue
        spans.append((start, end, clip.size))
        for hour in range(24):
            hour_start = day_start + timedelta(hours=hour)
            hour_end = hour_start + timedelta(hours=1)
            if start < hour_end and end > hour_start:
                hour_clips[hour] += 1
        cursor = start.replace(second=0, microsecond=0)
        while cursor < end:
            minutes[cursor.hour][cursor.minute] = MINUTE_COVERED
            cursor += timedelta(minutes=1)

    hours = [_level(sum(1 for level in minutes[h] if level)) for h in range(24)]

    ranges: list[dict] = []
    for start, end, size in sorted(spans):
        if ranges and start.timestamp() <= ranges[-1]["end_epoch"]:
            last = ranges[-1]
            last["end_epoch"] = max(last["end_epoch"], int(end.timestamp()))
            last["dur_s"] = last["end_epoch"] - last["start_epoch"]
            last["clips"] += 1
            last["size"] += size
        else:
            start_epoch, end_epoch = int(start.timestamp()), int(end.timestamp())
            ranges.append(
                {
                    "start_epoch": start_epoch,
                    "end_epoch": end_epoch,
                    "dur_s": end_epoch - start_epoch,
                    "clips": 1,
                    "size": size,
                }
            )

    for span in ranges:
        span["start_local"] = util.local_display(datetime.fromtimestamp(span["start_epoch"]))
        span["end_local"] = util.local_display(datetime.fromtimestamp(span["end_epoch"]))

    # Individual recording files (what the SD card holds): each gets its own
    # play/download row, so a window can be consumed file by file.
    files: list[dict] = []
    for clip in sorted(clips, key=lambda c: c.start_dt):
        start = max(clip.start_dt.astimezone(), day_start)
        end = min(clip.end_dt.astimezone(), day_end)
        if end <= start:
            continue
        files.append(
            {
                "start_epoch": int(start.timestamp()),
                "end_epoch": int(end.timestamp()),
                "dur_s": int((end - start).total_seconds()),
                "size": clip.size,
                "name": clip.name,
                "start_local": util.local_display(start),
                "end_local": util.local_display(end),
            }
        )

    return {
        "hours": hours,
        "hour_clips": hour_clips,
        "minutes": minutes,
        "ranges": ranges,
        "files": files,
    }


def clip_to_window(files: list[dict], from_epoch: int, to_epoch: int) -> list[tuple[int, int]]:
    """Clip recorded file intervals to [from_epoch, to_epoch], merged and sorted.

    The play page's restart bar needs second-precision covered spans, so it uses
    the file list (never the coarse whole-minute buckets) intersected with the
    window the player was asked to play.
    """
    spans = []
    for f in files:
        start = max(int(f["start_epoch"]), int(from_epoch))
        end = min(int(f["end_epoch"]), int(to_epoch))
        if end > start:
            spans.append((start, end))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or touching (start, end) epoch spans into disjoint ones."""
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def epoch_pct(epoch: int, from_epoch: int, to_epoch: int) -> float:
    """Map an epoch second to a 0..100 offset within [from_epoch, to_epoch]."""
    span = to_epoch - from_epoch
    if span <= 0:
        return 0.0
    return round(min(100.0, max(0.0, (epoch - from_epoch) / span * 100.0)), 4)


def nearest_epoch(
    segments: list[tuple[int, int]], target_epoch: int, from_epoch: int, to_epoch: int
) -> int | None:
    """Nearest instant inside a covered segment to target; None when nothing is covered.

    Used to snap a restart target out of a gap (mirrors the picker's
    snap-to-covered behaviour). Returns an epoch clamped to the window.
    """
    best: int | None = None
    best_dist: int | None = None
    for start, end in segments:
        point = start if target_epoch < start else end if target_epoch > end else target_epoch
        dist = abs(point - target_epoch)
        if best_dist is None or dist < best_dist:
            best, best_dist = point, dist
    if best is None:
        return None
    return int(min(max(best, from_epoch), to_epoch))


def window_eligibility(
    files: list[dict], from_epoch: int, to_epoch: int, target_epoch: int | None = None
) -> dict:
    """Eligible sub-intervals of a clip window plus the gaps between them.

    Pure geometry behind the play page's restart bar: clip the day's recorded
    files to [from_epoch, to_epoch], merge them, and describe what is covered,
    what is a gap, and the eligible instant nearest `target_epoch`. The window
    start and the snap target are independent: after a restart the window can
    begin earlier than the session's start, but the preview must still snap
    toward where the session actually is. `target_epoch` defaults to the window
    start. The whole window being a gap is a real state (no covered instant)
    that the UI must not confuse with "coverage couldn't be checked".
    """
    from_epoch, to_epoch = int(from_epoch), int(to_epoch)
    segments = clip_to_window(files, from_epoch, to_epoch)

    gaps: list[dict] = []
    cursor = from_epoch
    for start, end in segments:
        if start > cursor:
            gaps.append({"start_epoch": cursor, "end_epoch": start})
        cursor = max(cursor, end)
    if cursor < to_epoch:
        gaps.append({"start_epoch": cursor, "end_epoch": to_epoch})

    out_segments = [
        {
            "start_epoch": start,
            "end_epoch": end,
            "start_pct": epoch_pct(start, from_epoch, to_epoch),
            "end_pct": epoch_pct(end, from_epoch, to_epoch),
        }
        for start, end in segments
    ]
    snap_from = from_epoch if target_epoch is None else int(target_epoch)
    target = nearest_epoch(segments, snap_from, from_epoch, to_epoch)
    return {
        "from_epoch": from_epoch,
        "to_epoch": to_epoch,
        "segments": out_segments,
        "gaps": gaps,
        "covered_s": sum(end - start for start, end in segments),
        "has_eligible": bool(segments),
        "target_epoch": target if target is not None else from_epoch,
    }
