"""Aggregate a day's recordings into coverage buckets for the picker.

The UI shows a day as 24 hour tiles (shaded by coverage) and an hour as 60
minute cells, then lets the user start playback at a covered minute. Everything
here is pure computation over clips already returned by the camera search, so
the map and the range rows can never disagree.
"""

from datetime import datetime, timedelta

from . import util

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


def build_coverage(clips, day_start: datetime) -> dict:
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
