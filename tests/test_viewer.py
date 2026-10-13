"""Unit tests for time/URI helpers and the mock camera pipeline."""

import io
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from viewer import app as viewer_app
from viewer import camera, config, config_file, coverage, motion, streams, util


class TimeTests(unittest.TestCase):
    def test_cam_time_round_trip(self):
        dt = util.parse_cam_time("20261012T194808Z")
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual(util.to_cam_time(dt), "20261012T194808Z")

    def test_parse_user_range_rejects_inverted(self):
        with self.assertRaises(ValueError):
            util.parse_user_range("2026-10-13T10:00", "2026-10-13T09:00")

    def test_parse_user_range_rejects_garbage(self):
        with self.assertRaises(ValueError):
            util.parse_user_range("yesterday", "tomorrow")

    def test_parse_user_range_accepts_minute_precision(self):
        frm, to = util.parse_user_range("2026-10-13T10:00", "2026-10-13T11:30")
        self.assertLess(frm, to)
        self.assertIsNotNone(frm.tzinfo)

    def test_human_duration(self):
        self.assertEqual(util.human_duration(45), "45 s")
        self.assertEqual(util.human_duration(138), "2 min 18 s")
        self.assertEqual(util.human_duration(3725), "1 h 02 min")

    def test_human_size(self):
        self.assertEqual(util.human_size(512), "512 B")
        self.assertEqual(util.human_size(264532808), "252.3 MB")


class PlaybackUriTests(unittest.TestCase):
    URI = (
        "rtsp://192.168.0.156/Streaming/tracks/101/?starttime=20261012T194808Z"
        "&amp;endtime=20261012T201036Z&amp;name=ch01_00000000000000000&amp;size=264532808"
    )

    def test_normalize_unescapes_and_rewrites_host(self):
        out = util.normalize_playback_uri(self.URI, "rtsp://10.0.0.5:554")
        self.assertTrue(out.startswith("rtsp://10.0.0.5:554/Streaming/tracks/"))
        self.assertNotIn("&amp;", out)
        self.assertIn("starttime=20261012T194808Z", out)

    def test_clip_fields(self):
        uri = util.normalize_playback_uri(self.URI, "rtsp://192.168.0.156:554")
        fields = util.clip_fields(uri)
        self.assertEqual(fields["starttime"], "20261012T194808Z")
        self.assertEqual(fields["endtime"], "20261012T201036Z")
        self.assertEqual(fields["duration_s"], 22 * 60 + 28)
        self.assertEqual(fields["size"], 264532808)
        self.assertEqual(fields["name"], "ch01_00000000000000000")


class SearchParseTests(unittest.TestCase):
    RESPONSE = (
        '<?xml version="1.0" encoding="UTF-8"?><CMSearchResult version="2.0">'
        "<numOfMatches>1</numOfMatches><searchMatchItem>"
        "<trackID>101</trackID><startTime>2026-10-12T19:48:08Z</startTime>"
        "<endTime>2026-10-12T20:10:36Z</endTime>"
        "<playbackURI>rtsp://192.168.0.156/Streaming/tracks/101/?starttime=20261012T194808Z"
        "&amp;endtime=20261012T201036Z&amp;name=ch01_x&amp;size=1000</playbackURI>"
        "</searchMatchItem></CMSearchResult>"
    )

    def test_parse_search_response(self):
        clips = camera.parse_search_response(self.RESPONSE, "rtsp://192.168.0.156:554")
        self.assertEqual(len(clips), 1)
        clip = clips[0]
        self.assertEqual(clip.start_utc, "20261012T194808Z")
        self.assertEqual(clip.end_utc, "20261012T201036Z")
        self.assertEqual(clip.duration_s, 22 * 60 + 28)
        self.assertEqual(clip.size, 1000)

    def test_parse_search_response_rejects_protocol_drift(self):
        with self.assertRaises(camera.BadResponseError):
            camera.parse_search_response("<html>login page</html>", "rtsp://x")


class MockCameraTests(unittest.TestCase):
    def test_mock_endpoints(self):
        cam = camera.MockCamera("http://mock", "u", "p", "rtsp://mock", 5)
        self.assertIn("mock", cam.device_info()["model"].lower())
        self.assertEqual(cam.status()["uptime_s"], 4242)
        clips = cam.search("2026-10-12T00:00:00Z", "2026-10-13T00:00:00Z")
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].duration_s, 22 * 60 + 28)


class IsoToCamTests(unittest.TestCase):
    def test_iso_with_z(self):
        self.assertEqual(camera._iso_to_cam("2026-10-12T19:48:08Z"), "20261012T194808Z")

    def test_iso_space_form(self):
        self.assertEqual(camera._iso_to_cam("2026-10-12 19:48:08"), "20261012T194808Z")

    def test_garbage_returns_empty(self):
        self.assertEqual(camera._iso_to_cam("not-a-date"), "")


class CoverageTests(unittest.TestCase):
    """Day coverage aggregation for the picker (pure logic)."""

    def _clip(self, start_cam, end_cam, size=1000):
        return camera.Clip(
            uri="rtsp://x",
            uri_escaped="rtsp://x",
            start_utc=start_cam,
            end_utc=end_cam,
            name="n",
            size=size,
            duration_s=int(
                (util.parse_cam_time(end_cam) - util.parse_cam_time(start_cam)).total_seconds()
            ),
        )

    def test_buckets_and_merge(self):
        day = datetime(2026, 10, 13, tzinfo=datetime.now().astimezone().tzinfo)
        # two adjacent clips inside hour 3 local: 03:10-03:20 and 03:20-03:40
        base = day.replace(hour=3, minute=10)
        clips = [
            self._clip(
                util.to_cam_time(base.astimezone(timezone.utc)),
                util.to_cam_time((base + timedelta(minutes=10)).astimezone(timezone.utc)),
                size=100,
            ),
            self._clip(
                util.to_cam_time((base + timedelta(minutes=10)).astimezone(timezone.utc)),
                util.to_cam_time((base + timedelta(minutes=30)).astimezone(timezone.utc)),
                size=200,
            ),
        ]
        built = coverage.build_coverage(clips, day)
        self.assertEqual(built["hours"][3], 2)  # 30 of 60 minutes covered
        self.assertEqual(sum(built["minutes"][3]), 30 * coverage.MINUTE_COVERED)
        self.assertEqual(built["hour_clips"][3], 2)
        self.assertEqual(len(built["ranges"]), 1)  # merged into one range
        self.assertEqual(built["ranges"][0]["dur_s"], 1800)
        self.assertEqual(built["ranges"][0]["clips"], 2)
        self.assertEqual(built["ranges"][0]["size"], 300)
        self.assertEqual(built["hours"][5], 0)

    def test_clips_outside_the_day_are_clamped_away(self):
        day = datetime(2026, 10, 13, tzinfo=datetime.now().astimezone().tzinfo)
        other = day + timedelta(days=2)
        clip = self._clip(
            util.to_cam_time(other.replace(hour=1).astimezone(timezone.utc)),
            util.to_cam_time(other.replace(hour=2).astimezone(timezone.utc)),
        )
        built = coverage.build_coverage([clip], day)
        self.assertEqual(built["hours"], [0] * 24)
        self.assertEqual(built["ranges"], [])

    def test_levels_quantize(self):
        self.assertEqual(coverage._level(0), 0)
        self.assertEqual(coverage._level(5), 1)
        self.assertEqual(coverage._level(30), 2)
        self.assertEqual(coverage._level(50), 3)
        self.assertEqual(coverage._level(60), 4)


class NeighbourTests(unittest.TestCase):
    """Neighbour navigation must report the gap a jump would skip."""

    def _file(self, start, end, name="f"):
        return {
            "start_epoch": start,
            "end_epoch": end,
            "dur_s": end - start,
            "size": 1,
            "name": name,
            "start_local": "",
            "end_local": "",
        }

    def test_earlier_later_and_gaps(self):
        files = [self._file(0, 100), self._file(160, 260), self._file(1000, 1100)]
        out = coverage.neighbours(files, current_from=160, current_to=260)
        self.assertEqual([f["start_epoch"] for f in out["earlier"]], [0])
        self.assertEqual(out["earlier"][0]["gap_s"], 60)  # 100 -> 160 is empty
        self.assertEqual([f["start_epoch"] for f in out["later"]], [1000])
        self.assertEqual(out["later"][0]["gap_s"], 740)  # 260 -> 1000 is empty
        self.assertFalse(out["more_later"])

    def test_limit_and_truncation_flags(self):
        files = [self._file(i * 1000, i * 1000 + 100) for i in range(1, 9)]
        out = coverage.neighbours(files, current_from=1000, current_to=1100, limit=2)
        self.assertEqual(len(out["earlier"]), 0)
        self.assertEqual(len(out["later"]), 2)
        self.assertTrue(out["more_later"])


class StorageParseTests(unittest.TestCase):
    """Real camera replies carry attributes and namespaces; parsing must survive."""

    REAL_XML = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<storage version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">\n'
        '<hddList version="1.0" xmlns="http://www.hikvision.com/ver10/XMLSchema" size="8" >\n'
        '<hdd version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">\n'
        "<id>1</id><hddName>hdde</hddName><hddPath></hddPath><hddType>SATA</hddType>\n"
        "<status>ok</status><capacity>244016</capacity><freeSpace>242432</freeSpace>\n"
        "<property>RW</property>\n</hdd>\n</hddList>\n"
        '<nasList version="2.0"><supportMountType opt="NFS,SMB/CIFS"/></nasList>\n'
        "<workMode>quota</workMode>\n</storage>"
    )

    def test_parses_namespaced_hdd_with_attributes(self):
        parsed = camera.parse_storage_response(self.REAL_XML)
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["capacity_mb"], 244016)
        self.assertEqual(parsed["free_mb"], 242432)
        self.assertTrue(parsed["writable"])

    def test_rejects_reply_without_hdd(self):
        with self.assertRaises(camera.BadResponseError):
            camera.parse_storage_response("<storage><nasList/></storage>")

    def test_rejects_unparseable_reply(self):
        with self.assertRaises(camera.BadResponseError):
            camera.parse_storage_response("<html>login required</html>")


class ErrorClassifyTests(unittest.TestCase):
    """A connect failure is 'unreachable'; a slow but connected reply is a service problem."""

    def test_connect_timeout_is_unreachable(self):
        import requests

        exc = requests.exceptions.ConnectTimeout("connect timeout")
        self.assertIsInstance(camera._classify_requests_error(exc), camera.UnreachableError)

    def test_connection_error_is_unreachable(self):
        import requests

        exc = requests.exceptions.ConnectionError("no route")
        self.assertIsInstance(camera._classify_requests_error(exc), camera.UnreachableError)

    def test_read_timeout_is_service_down(self):
        import requests

        exc = requests.exceptions.ReadTimeout("server too slow")
        self.assertIsInstance(camera._classify_requests_error(exc), camera.ServiceDownError)


class OrphanCleanupTests(unittest.TestCase):
    def test_missing_pkill_does_not_raise(self):
        """Minimal container images have no pkill; session start must survive."""
        from pathlib import Path

        from viewer import streams

        with mock.patch("viewer.streams.subprocess.run", side_effect=FileNotFoundError("pkill")):
            streams._kill_orphans(Path("/tmp/whatever"))  # must not raise


class BasicAuthTests(unittest.TestCase):
    """Viewer-side HTTP Basic Auth: active only when both env vars are set."""

    def _with_auth(self, user, password):
        return mock.patch.multiple(config, AUTH_USER=user, AUTH_PASSWORD=password)

    def test_no_auth_configured_allows_anything(self):
        with self._with_auth("", ""):
            self.assertTrue(viewer_app.check_auth(None))
            self.assertTrue(viewer_app.check_auth("Basic anything"))

    def test_configured_requires_valid_credentials(self):
        import base64

        good = "Basic " + base64.b64encode(b"alice:s3cret").decode()
        bad_pw = "Basic " + base64.b64encode(b"alice:wrong").decode()
        bad_user = "Basic " + base64.b64encode(b"bob:s3cret").decode()
        with self._with_auth("alice", "s3cret"):
            self.assertFalse(viewer_app.check_auth(None))
            self.assertFalse(viewer_app.check_auth(""))
            self.assertFalse(viewer_app.check_auth("Bearer token"))
            self.assertFalse(viewer_app.check_auth("Basic not-base64!!"))
            self.assertFalse(viewer_app.check_auth(bad_pw))
            self.assertFalse(viewer_app.check_auth(bad_user))
            self.assertTrue(viewer_app.check_auth(good))
            self.assertTrue(viewer_app.check_auth("basic " + good.split(" ", 1)[1]))


class MotionGridTests(unittest.TestCase):
    """Decode/encode the camera's 18x22 bit map - one source of truth (motion.py)."""

    def test_all_armed_row_is_fffffc(self):
        rows = motion.decode_gridmap("fffffc" * 18)
        self.assertTrue(all(all(row) for row in rows))
        self.assertEqual(motion.encode_gridmap(rows), "fffffc" * 18)

    def test_empty_grid_round_trip(self):
        self.assertEqual(motion.encode_gridmap(motion.empty_grid()), "000000" * 18)

    def test_first_cell_is_the_msb(self):
        one = motion.empty_grid()
        one[0][0] = True
        self.assertTrue(motion.encode_gridmap(one).startswith("800000"))
        last = motion.empty_grid()
        last[0][21] = True
        self.assertTrue(motion.encode_gridmap(last).startswith("000004"))

    def test_padding_bits_are_not_cells(self):
        # The two low bits of each row are padding: setting them adds no cells.
        rows = motion.decode_gridmap("000003" + "000000" * 17)
        self.assertFalse(any(rows[0]))

    def test_encode_preserves_padding_bits_from_base(self):
        base = "000003" * 18  # padding bits set, no cells armed
        rows = motion.decode_gridmap(base)
        self.assertEqual(motion.encode_gridmap(rows), "000000" * 18)
        self.assertEqual(motion.encode_gridmap(rows, base_grid_map=base), base)
        rows[0][0] = True
        self.assertEqual(motion.encode_gridmap(rows, base_grid_map=base), "800003" + "000003" * 17)

    def test_vendor_worked_example(self):
        # Worked example from Hikvision's ISAPI IPMD Service guide: 108 hex chars
        # that decode to a person-shaped blob (row 7 cols 9-11; rows 8-10 cols
        # 9-12, 1-based) plus a corner block (rows 15-18, cols 1-4). Locks the
        # vendor's stated order: first cell upper-left, left-to-right, top-to-bottom.
        vendor = (
            "00000000000000000000000000000000000000e00000f00000f00000f000000000000000000"
            "000000000f00000f00000f00000f00000"
        )
        self.assertEqual(len(vendor), motion.GRID_HEX_LEN)
        rows = motion.decode_gridmap(vendor)
        armed = {(r, c) for r in range(motion.ROWS) for c in range(motion.COLS) if rows[r][c]}
        expected = (
            {(6, 8), (6, 9), (6, 10)}
            | {(r, c) for r in (7, 8, 9) for c in (8, 9, 10, 11)}
            | {(r, c) for r in (14, 15, 16, 17) for c in (0, 1, 2, 3)}
        )
        self.assertEqual(armed, expected)
        self.assertEqual(motion.encode_gridmap(rows), vendor)

    def test_round_trip_asymmetric_pattern(self):
        rows = motion.empty_grid()
        rows[3][5] = rows[3][6] = rows[3][7] = True
        rows[17][0] = True
        self.assertEqual(motion.decode_gridmap(motion.encode_gridmap(rows)), rows)

    def test_wrong_length_and_non_hex_rejected(self):
        with self.assertRaises(ValueError):
            motion.decode_gridmap("fff")
        with self.assertRaises(ValueError):
            motion.decode_gridmap("g" * 108)

    def test_arm_mask_invert(self):
        rows = motion.empty_grid()
        motion.set_region(rows, 0, 0, 2, 3, True)
        self.assertEqual(motion.counts(rows)["armed"], 6)
        motion.set_region(rows, 0, 0, 2, 3, False)
        self.assertEqual(motion.counts(rows)["armed"], 0)
        motion.set_region(rows, 0, 0, 1, 1, True)
        motion.invert(rows)
        self.assertEqual(motion.counts(rows)["armed"], 396 - 1)

    def test_quick_zone_is_whole_cells(self):
        rows = motion.empty_grid()
        motion.apply_tool_to_zone(rows, 0, 0, True)
        self.assertEqual(motion.counts(rows)["armed"], 3 * 11)
        self.assertTrue(rows[0][0] and rows[2][10])
        self.assertFalse(rows[0][11])  # right half untouched
        self.assertFalse(rows[3][0])  # next band untouched

    def test_summary_counts(self):
        rows = motion.empty_grid()
        motion.set_region(rows, 0, 0, 18, 11, True)  # 18 x 11 = 198
        counted = motion.counts(rows)
        self.assertEqual(counted, {"armed": 198, "masked": 198, "total": 396})
        self.assertEqual(motion.summary(counted), "198 of 396 cells armed (50%)")

    def test_sensitivity_validation(self):
        for value in (0, 20, 40, 60, 80, 100):
            self.assertTrue(motion.valid_sensitivity(value))
        for invalid_value in (55, -20, 101, "60", True, None):
            self.assertFalse(motion.valid_sensitivity(invalid_value))

    def test_diff_reports_drift(self):
        import copy

        base = {
            "enabled": True,
            "enable_highlight": False,
            "sensitivity": 60,
            "cells": motion.empty_grid(),
        }
        self.assertEqual(motion.diff(base, copy.deepcopy(base)), [])
        actual = copy.deepcopy(base)
        actual["sensitivity"] = 40
        differences = motion.diff(base, actual)
        self.assertEqual(differences, ["The camera kept sensitivity at 40."])


class MotionCameraTests(unittest.TestCase):
    """Read-modify-write of the motionDetection document (mock round-trip)."""

    def setUp(self):
        self.cam = camera.MockCamera("http://mock", "u", "p", "rtsp://mock", 5)

    def test_parse_motion_detection(self):
        doc = self.cam.motion_detection()
        self.assertFalse(doc["enabled"])
        self.assertEqual(doc["sensitivityLevel"], 40)
        self.assertEqual(doc["regionType"], "grid")
        self.assertEqual(doc["rowGranularity"], "18")
        self.assertEqual(doc["columnGranularity"], "22")
        self.assertEqual(motion.validate_gridmap(doc["gridMap"]), doc["gridMap"])

    def test_read_modify_write_round_trips(self):
        merged = camera.apply_motion_fields(
            self.cam.motion_detection_xml(),
            enabled=True,
            enable_highlight=True,
            sensitivity=60,
            grid_map="aaaaaa" * 18,
        )
        self.cam.put_motion_detection(merged)
        doc = self.cam.motion_detection()
        self.assertTrue(doc["enabled"])
        self.assertTrue(doc["enableHighlight"])
        self.assertEqual(doc["sensitivityLevel"], 60)
        self.assertEqual(doc["gridMap"], "aaaaaa" * 18)
        # Fields we do not edit survive the merge untouched.
        self.assertEqual(doc["samplingInterval"], "2")
        self.assertEqual(doc["regionType"], "grid")

    def test_apply_registers_default_namespace(self):
        merged = camera.apply_motion_fields(self.cam.motion_detection_xml(), enabled=True)
        self.assertIn('xmlns="http://www.hikvision.com/ver20/XMLSchema"', merged)
        self.assertNotIn("ns0:", merged)

    def test_parse_rejects_short_grid(self):
        bad = (
            "<MotionDetection xmlns='http://www.hikvision.com/ver20/XMLSchema'>"
            "<MotionDetectionLayout><layout><gridMap>short</gridMap></layout>"
            "</MotionDetectionLayout></MotionDetection>"
        )
        with self.assertRaises(camera.BadResponseError):
            camera.parse_motion_detection(bad)

    def test_parse_rejects_non_xml(self):
        with self.assertRaises(camera.BadResponseError):
            camera.parse_motion_detection("<html>login required</html>")


class MotionApiGuardTests(unittest.TestCase):
    """Origin + content-type guard on the app's first mutating endpoint."""

    def _handler(self, headers):
        return mock.Mock(headers=headers)

    def test_allows_same_origin_json(self):
        handler = self._handler(
            {
                "Origin": "http://127.0.0.1:8756",
                "Host": "127.0.0.1:8756",
                "Content-Type": "application/json",
            }
        )
        self.assertTrue(viewer_app.check_same_origin(handler))

    def test_allows_missing_origin_json(self):
        handler = self._handler(
            {"Host": "127.0.0.1:8756", "Content-Type": "application/json; charset=utf-8"}
        )
        self.assertTrue(viewer_app.check_same_origin(handler))

    def test_rejects_cross_origin(self):
        handler = self._handler(
            {
                "Origin": "http://evil.example",
                "Host": "127.0.0.1:8756",
                "Content-Type": "application/json",
            }
        )
        self.assertFalse(viewer_app.check_same_origin(handler))

    def test_rejects_non_json(self):
        handler = self._handler(
            {"Host": "127.0.0.1:8756", "Content-Type": "application/x-www-form-urlencoded"}
        )
        self.assertFalse(viewer_app.check_same_origin(handler))

    def test_intent_validation(self):
        app = viewer_app.ViewerApp()
        intent = app._motion_intent(
            {
                "enabled": True,
                "enable_highlight": False,
                "sensitivity": 40,
                "cells": motion.empty_grid(),
            }
        )
        self.assertEqual(intent["gridMap"], "000000" * 18)
        for bad in (
            {"cells": [[False] * 22] * 17, "sensitivity": 40},
            {"cells": motion.empty_grid(), "sensitivity": 55},
            {"cells": "nope", "sensitivity": 40},
            "not-an-object",
        ):
            with self.assertRaises(ValueError):
                app._motion_intent(bad)

    def test_intent_rejects_non_boolean_cells(self):
        # bool("0") is True: a truthy string must not arm a cell the caller
        # asked to leave masked while the error promises a boolean array.
        app = viewer_app.ViewerApp()
        rows = motion.empty_grid()
        rows[0][0] = "0"  # type: ignore[call-overload]  # deliberately non-boolean cell
        with self.assertRaises(ValueError):
            app._motion_intent({"cells": rows, "sensitivity": 40})

    def test_rejects_rebound_host_even_when_origin_matches(self):
        # A DNS-rebinding attacker controls both Host and Origin, so they match;
        # the Host allowlist is what rejects the request.
        handler = self._handler(
            {
                "Origin": "http://evil.example",
                "Host": "evil.example",
                "Content-Type": "application/json",
            }
        )
        self.assertFalse(viewer_app.check_same_origin(handler))
        reason = viewer_app.motion_guard_reason(handler)
        assert reason is not None
        self.assertIn("VIEWER_ALLOWED_HOSTS", reason)

    def test_accepts_loopback_host_names(self):
        for host in ("localhost:8756", "127.0.0.1", "[::1]:8756"):
            handler = self._handler({"Host": host, "Content-Type": "application/json"})
            self.assertTrue(viewer_app.check_same_origin(handler), host)

    def test_accepts_configured_allowed_host(self):
        with mock.patch.object(config, "ALLOWED_HOSTS", ("cam.local",)):
            handler = self._handler({"Host": "CAM.local:9000", "Content-Type": "application/json"})
            self.assertTrue(viewer_app.check_same_origin(handler))

    def test_rejects_media_type_substring(self):
        handler = self._handler({"Host": "127.0.0.1", "Content-Type": "application/jsonp"})
        self.assertFalse(viewer_app.check_same_origin(handler))


class CameraPutMappingTests(unittest.TestCase):
    """Camera._put maps HTTP status to the honest error classes."""

    def _cam_with(self, status, text=""):
        cam = camera.Camera("http://cam", "u", "p", "rtsp://cam", 5)
        response = mock.Mock(status_code=status, text=text)
        cam.session = mock.Mock()
        cam.session.put.return_value = response
        return cam

    def test_403_is_rejected_not_service_down(self):
        cam = self._cam_with(403, "<statusString>Invalid Operation</statusString>")
        with self.assertRaises(camera.RejectedError):
            cam._put("/ISAPI/x", "<body/>")

    def test_400_is_bad_response(self):
        with self.assertRaises(camera.BadResponseError):
            self._cam_with(400, "bad")._put("/ISAPI/x", "<body/>")

    def test_415_is_bad_response(self):
        with self.assertRaises(camera.BadResponseError):
            self._cam_with(415, "bad")._put("/ISAPI/x", "<body/>")

    def test_500_is_service_down(self):
        with self.assertRaises(camera.ServiceDownError):
            self._cam_with(500, "boom")._put("/ISAPI/x", "<body/>")


class CameraCredentialsMappingTests(unittest.TestCase):
    """A 401 is "credentials refused", never collapsed into "did not answer"."""

    def _cam(self):
        return camera.Camera("http://cam", "u", "p", "rtsp://cam", 5)

    def test_401_is_credentials_and_stays_a_camera_error(self):
        # Subclassing CameraError/ServiceDownError is what keeps every existing
        # failure path typed (and stops a new type escaping a handler as a 500).
        self.assertTrue(issubclass(camera.CredentialsRejectedError, camera.CameraError))
        self.assertTrue(issubclass(camera.CredentialsRejectedError, camera.ServiceDownError))

    def test_get_401_is_credentials(self):
        cam = self._cam()
        cam.session = mock.Mock()
        cam.session.get.return_value = mock.Mock(status_code=401, text="")
        with self.assertRaises(camera.CredentialsRejectedError):
            cam._get("/ISAPI/System/deviceInfo")

    def test_get_500_is_still_service_down(self):
        cam = self._cam()
        cam.session = mock.Mock()
        cam.session.get.return_value = mock.Mock(status_code=500, text="")
        with self.assertRaises(camera.ServiceDownError):
            cam._get("/ISAPI/System/deviceInfo")

    def test_put_401_is_credentials(self):
        cam = self._cam()
        cam.session = mock.Mock()
        cam.session.put.return_value = mock.Mock(status_code=401, text="")
        with self.assertRaises(camera.CredentialsRejectedError):
            cam._put("/ISAPI/x", "<body/>")

    def test_search_401_is_credentials(self):
        cam = self._cam()
        cam.session = mock.Mock()
        cam.session.post.return_value = mock.Mock(status_code=401, text="")
        with self.assertRaises(camera.CredentialsRejectedError):
            cam.search("2026-10-12T00:00:00Z", "2026-10-13T00:00:00Z")

    def test_snapshot_401_is_credentials(self):
        cam = self._cam()
        cam.session = mock.Mock()
        cam.session.get.return_value = mock.Mock(status_code=401, content=b"")
        with self.assertRaises(camera.CredentialsRejectedError):
            cam.snapshot_jpeg("101")

    def test_download_401_is_credentials_and_closes(self):
        cam = self._cam()
        cam.session = mock.Mock()
        response = mock.Mock(status_code=401, headers={})
        cam.session.get.return_value = response
        with self.assertRaises(camera.CredentialsRejectedError):
            cam.download_stream("rtsp://x")
        response.close.assert_called_once()

    def test_download_500_is_service_down(self):
        cam = self._cam()
        cam.session = mock.Mock()
        cam.session.get.return_value = mock.Mock(status_code=500, headers={})
        with self.assertRaises(camera.ServiceDownError):
            cam.download_stream("rtsp://x")

    def test_download_200_returns_the_streaming_response(self):
        cam = self._cam()
        cam.session = mock.Mock()
        response = mock.Mock(status_code=200, headers={})
        cam.session.get.return_value = response
        self.assertIs(cam.download_stream("rtsp://x"), response)


class _FakeCam:
    """Camera stand-in for motion_write: no network, records the PUT body."""

    def __init__(self, base_xml, *, put_error=None, readback=None):
        self.base_xml = base_xml
        self.put_error = put_error
        self.readback = readback
        self.put_body = None

    def motion_detection_xml(self):
        return self.base_xml

    def motion_detection(self):
        if isinstance(self.readback, Exception):
            raise self.readback
        return self.readback

    def put_motion_detection(self, xml_text):
        self.put_body = xml_text
        if self.put_error:
            raise self.put_error
        return ""  # the app never reads the PUT reply body


class _FakeBodyHandler:
    """Minimal handler for route_motion_post: headers, request body, response sink."""

    def __init__(self, headers, body=b""):
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.close_connection = False
        self.status = None
        self.response_headers = {}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


class CredentialsSurfaceTests(unittest.TestCase):
    """The credentials message reaches every page that surfaces camera failures."""

    def _app_with(self, **side_effects):
        cam = mock.Mock()
        for name, effect in side_effects.items():
            getattr(cam, name).side_effect = effect
        app = viewer_app.ViewerApp()
        app.cam = cam
        return app, cam

    def test_coverage_401_is_a_credentials_state(self):
        app, _ = self._app_with(search=camera.CredentialsRejectedError("401"))
        handler = _FakeBodyHandler({})
        app.route_coverage(handler, {"date": ["2026-10-13"]})
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["state"], "credentials")
        self.assertIn("credentials", payload["state_detail"])

    def test_range_search_401_card_names_credentials(self):
        app, _ = self._app_with(search=camera.CredentialsRejectedError("401"))
        card, code = app.run_search("2026-10-13T00:00", "2026-10-13T01:00")
        self.assertEqual(code, 502)
        self.assertIn("refused the viewer", card)

    def test_motion_read_401_is_a_credentials_state(self):
        app, _ = self._app_with(motion_detection=camera.CredentialsRejectedError("401"))
        handler = _FakeBodyHandler({})
        app.route_motion_get(handler)
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["state"], "credentials")

    def test_live_page_shows_the_credentials_warning(self):
        app, cam = self._app_with(stream_resolution=camera.CredentialsRejectedError("401"))
        cam.storage.return_value = {"free_mb": 1, "capacity_mb": 2}
        cam.device_info.return_value = {}
        handler = _FakeBodyHandler({})
        app.page_live(handler)
        html = handler.wfile.getvalue().decode()
        self.assertIn("refused the viewer", html)


class MotionWriteTests(unittest.TestCase):
    """motion_write across the honest states (ok, drift, timeout, rejected, bad base)."""

    BASE = camera.MockCamera.MOCK_MOTION_XML

    def _app(self):
        return viewer_app.ViewerApp()

    def _intent(self, app, *, enabled=True, sensitivity=60, grid=None):
        return app._motion_intent(
            {
                "enabled": enabled,
                "enable_highlight": False,
                "sensitivity": sensitivity,
                "cells": grid if grid is not None else motion.empty_grid(),
            }
        )

    def _doc(self, base_xml, intended):
        merged = camera.apply_motion_fields(
            base_xml,
            enabled=intended["enabled"],
            enable_highlight=intended["enable_highlight"],
            sensitivity=intended["sensitivity"],
            grid_map=intended["gridMap"],
        )
        return camera.parse_motion_detection(merged)

    def test_ok_reports_readback(self):
        app = self._app()
        intended = self._intent(app)
        app.cam = _FakeCam(self.BASE, readback=self._doc(self.BASE, intended))
        result = app.motion_write(intended)
        self.assertEqual(result["state"], "ok")
        self.assertTrue(result["match"])

    def test_drift_reports_what_the_camera_stored(self):
        app = self._app()
        intended = self._intent(app, sensitivity=80)
        stored = self._intent(app, sensitivity=40)  # camera keeps its old level
        app.cam = _FakeCam(self.BASE, readback=self._doc(self.BASE, stored))
        result = app.motion_write(intended)
        self.assertEqual(result["state"], "ok")
        self.assertFalse(result["match"])
        self.assertTrue(result["differences"])
        self.assertEqual(result["actual"]["sensitivity"], 40)

    def test_timeout_but_readback_landed_is_ok(self):
        app = self._app()
        intended = self._intent(app)
        app.cam = _FakeCam(
            self.BASE,
            put_error=camera.ServiceDownError("camera did not answer in time"),
            readback=self._doc(self.BASE, intended),
        )
        result = app.motion_write(intended)
        self.assertEqual(result["state"], "ok")
        self.assertTrue(result["match"])

    def test_timeout_with_different_readback_is_unobserved(self):
        app = self._app()
        intended = self._intent(app, sensitivity=60)
        current = camera.parse_motion_detection(self.BASE)  # unchanged camera state
        app.cam = _FakeCam(
            self.BASE,
            put_error=camera.ServiceDownError("camera did not answer in time"),
            readback=current,
        )
        result = app.motion_write(intended)
        self.assertEqual(result["state"], "service_down")
        self.assertTrue(result["write_attempted"])
        self.assertIsNotNone(result["actual"])

    def test_timeout_with_unreadable_camera_is_unobserved(self):
        app = self._app()
        intended = self._intent(app)
        app.cam = _FakeCam(
            self.BASE,
            put_error=camera.ServiceDownError("camera did not answer in time"),
            readback=camera.ServiceDownError("no readback"),
        )
        result = app.motion_write(intended)
        self.assertEqual(result["state"], "service_down")
        self.assertTrue(result["write_attempted"])
        self.assertIsNone(result["actual"])

    def test_rejected_writes_nothing(self):
        app = self._app()
        intended = self._intent(app)
        app.cam = _FakeCam(self.BASE, put_error=camera.RejectedError("403 Invalid Operation"))
        result = app.motion_write(intended)
        self.assertEqual(result["state"], "rejected")
        self.assertFalse(result["write_attempted"])

    def test_credentials_refused_write_saves_nothing(self):
        app = self._app()
        intended = self._intent(app)
        app.cam = _FakeCam(self.BASE, put_error=camera.CredentialsRejectedError("401"))
        result = app.motion_write(intended)
        self.assertEqual(result["state"], "credentials")
        self.assertFalse(result["write_attempted"])

    def test_refuses_to_write_when_base_is_not_a_motion_document(self):
        app = self._app()
        intended = self._intent(app)
        fake = _FakeCam("<html><body>login required</body></html>", readback=None)
        app.cam = fake
        result = app.motion_write(intended)
        self.assertEqual(result["state"], "bad_response")
        self.assertFalse(result["write_attempted"])
        self.assertIsNone(fake.put_body)  # the nonsensical write must never happen

    def test_write_preserves_the_camera_padding_bits(self):
        base = self.BASE.replace("ffe000" * 18, "ffffff" * 18)
        app = self._app()
        intended = self._intent(app)  # all masked -> cells zero, padding stays 3
        stored = camera.parse_motion_detection(
            camera.apply_motion_fields(
                base,
                enabled=intended["enabled"],
                enable_highlight=intended["enable_highlight"],
                sensitivity=intended["sensitivity"],
                grid_map="000003" * 18,
            )
        )
        fake = _FakeCam(base, readback=stored)
        app.cam = fake
        result = app.motion_write(intended)
        self.assertEqual(result["state"], "ok")
        self.assertIsNotNone(fake.put_body)
        assert fake.put_body is not None
        self.assertIn("000003" * 18, fake.put_body)


class MotionPostBodyTests(unittest.TestCase):
    """Non-numeric / oversized / deeply nested POST bodies get a typed 400."""

    def _app(self):
        app = viewer_app.ViewerApp()
        # This mock-body test intentionally injects a partial camera fake; the
        # exercised path touches only the motion methods _FakeCam implements.
        app.cam = _FakeCam(camera.MockCamera.MOCK_MOTION_XML, readback=None)  # type: ignore[assignment]
        return app

    def _handler(self, headers, body=b""):
        return _FakeBodyHandler(headers, body)

    def _json_headers(self, length):
        return {
            "Host": "127.0.0.1",
            "Content-Type": "application/json",
            "Content-Length": str(length),
        }

    def test_non_numeric_content_length_is_typed_400(self):
        handler = self._handler(
            {"Host": "127.0.0.1", "Content-Type": "application/json", "Content-Length": "abc"}
        )
        self._app().route_motion_post(handler)
        self.assertEqual(handler.status, 400)
        self.assertIn(b"bad_request", handler.wfile.getvalue())

    def test_oversized_body_closes_and_answers_400(self):
        handler = self._handler(self._json_headers(2_000_000))
        self._app().route_motion_post(handler)
        self.assertEqual(handler.status, 400)
        self.assertTrue(handler.close_connection)

    def test_deeply_nested_json_is_typed_400(self):
        raw = ("[" * 3000 + "]" * 3000).encode()
        handler = self._handler(self._json_headers(len(raw)), raw)
        self._app().route_motion_post(handler)
        self.assertEqual(handler.status, 400)
        self.assertIn(b"bad_request", handler.wfile.getvalue())


class WindowEligibilityTests(unittest.TestCase):
    """Pure geometry behind the restart bar: clipping, gaps, snapping, offsets."""

    def _file(self, start, end, name="f"):
        return {
            "start_epoch": start,
            "end_epoch": end,
            "dur_s": end - start,
            "size": 1,
            "name": name,
            "start_local": "",
            "end_local": "",
        }

    def test_clip_to_window_merges_adjacent_files(self):
        files = [self._file(0, 100), self._file(100, 200), self._file(400, 500)]
        self.assertEqual(coverage.clip_to_window(files, 50, 450), [(50, 200), (400, 450)])

    def test_window_eligibility_reports_segments_and_gaps(self):
        files = [self._file(0, 100), self._file(200, 300)]
        out = coverage.window_eligibility(files, 50, 250)
        self.assertEqual(
            [(s["start_epoch"], s["end_epoch"]) for s in out["segments"]],
            [(50, 100), (200, 250)],
        )
        self.assertEqual([(g["start_epoch"], g["end_epoch"]) for g in out["gaps"]], [(100, 200)])
        self.assertTrue(out["has_eligible"])
        self.assertEqual(out["covered_s"], 100)

    def test_epoch_pct_maps_and_clamps_to_the_window(self):
        self.assertEqual(coverage.epoch_pct(0, 0, 100), 0.0)
        self.assertEqual(coverage.epoch_pct(50, 0, 100), 50.0)
        self.assertEqual(coverage.epoch_pct(100, 0, 100), 100.0)
        self.assertEqual(coverage.epoch_pct(-10, 0, 100), 0.0)
        self.assertEqual(coverage.epoch_pct(999, 0, 100), 100.0)

    def test_nearest_epoch_snaps_out_of_a_gap(self):
        segments = [(50, 100), (200, 250)]
        self.assertEqual(coverage.nearest_epoch(segments, 160, 50, 250), 200)
        self.assertEqual(coverage.nearest_epoch(segments, 140, 50, 250), 100)
        self.assertEqual(coverage.nearest_epoch(segments, 80, 50, 250), 80)
        self.assertEqual(coverage.nearest_epoch(segments, 249, 50, 250), 249)
        self.assertIsNone(coverage.nearest_epoch([], 150, 50, 250))

    def test_no_eligibility_is_a_real_state_not_an_error(self):
        out = coverage.window_eligibility([self._file(1000, 1100)], 50, 250)
        self.assertEqual(out["segments"], [])
        self.assertFalse(out["has_eligible"])
        self.assertEqual(out["gaps"], [{"start_epoch": 50, "end_epoch": 250}])
        self.assertEqual(out["target_epoch"], 50)  # falls back to the window start

    def test_target_epoch_points_at_nearest_recording(self):
        out = coverage.window_eligibility([self._file(200, 300)], 50, 350)
        self.assertEqual(out["target_epoch"], 200)

    def test_target_epoch_can_differ_from_the_window_start(self):
        # A restart moved the session start to 220 while the window still begins
        # at 50; the preview must snap toward the session, not the window start.
        files = [self._file(50, 150), self._file(200, 300)]
        out = coverage.window_eligibility(files, 50, 300, target_epoch=220)
        self.assertEqual(out["from_epoch"], 50)
        self.assertEqual(out["to_epoch"], 300)
        self.assertEqual(out["target_epoch"], 220)
        # Without an explicit target the window start is snapped (first arrival).
        self.assertEqual(coverage.window_eligibility(files, 50, 300)["target_epoch"], 50)

    def test_target_epoch_snaps_out_of_a_gap_toward_the_session(self):
        files = [self._file(50, 150), self._file(200, 300)]
        out = coverage.window_eligibility(files, 50, 300, target_epoch=180)
        self.assertEqual(out["target_epoch"], 200)

    def test_clip_to_window_inverted_window_is_empty(self):
        # from > to is invalid input; it must not fabricate coverage.
        files = [self._file(0, 100), self._file(100, 200)]
        self.assertEqual(coverage.clip_to_window(files, 450, 50), [])

    def test_window_eligibility_inverted_window_has_no_eligibility(self):
        out = coverage.window_eligibility([self._file(0, 100)], 450, 50)
        self.assertEqual(out["segments"], [])
        self.assertFalse(out["has_eligible"])
        self.assertEqual(out["gaps"], [])
        self.assertEqual(out["covered_s"], 0)

    def test_window_eligibility_zero_width_window(self):
        out = coverage.window_eligibility([self._file(0, 100)], 50, 50)
        self.assertEqual(out["segments"], [])
        self.assertFalse(out["has_eligible"])
        self.assertEqual(out["covered_s"], 0)

    def test_nearest_epoch_tie_prefers_the_earlier_segment(self):
        # 15 is equidistant from 10 and 20; the earlier segment wins the tie.
        segments = [(0, 10), (20, 30)]
        self.assertEqual(coverage.nearest_epoch(segments, 15, 0, 30), 10)

    def test_merge_spans_collapses_nested_and_touching(self):
        spans = [(0, 100), (20, 30), (40, 50), (100, 120), (130, 140)]
        self.assertEqual(coverage._merge_spans(spans), [(0, 120), (130, 140)])
        self.assertEqual(coverage._merge_spans([]), [])


class NeighboursApiTests(unittest.TestCase):
    """route_neighbours exposes the restart bar's window without dropping fields."""

    def _clip(self, start_cam, end_cam):
        return camera.Clip(
            uri="rtsp://x",
            uri_escaped="rtsp://x",
            start_utc=start_cam,
            end_utc=end_cam,
            name="n",
            size=100,
            duration_s=0,
        )

    def test_window_payload_and_existing_fields(self):
        day = datetime(2026, 10, 13, tzinfo=datetime.now().astimezone().tzinfo)
        clip = self._clip(
            util.to_cam_time(day.replace(hour=8, minute=1).astimezone(timezone.utc)),
            util.to_cam_time(day.replace(hour=8, minute=31).astimezone(timezone.utc)),
        )
        cam = mock.Mock()
        cam.search.return_value = [clip]
        app = viewer_app.ViewerApp()
        app.cam = cam
        handler = _FakeBodyHandler({})
        from_cam = util.to_cam_time(day.replace(hour=8, minute=10).astimezone(timezone.utc))
        to_cam = util.to_cam_time(day.replace(hour=8, minute=20).astimezone(timezone.utc))

        app.route_neighbours(handler, {"from": [from_cam], "to": [to_cam]})
        payload = json.loads(handler.wfile.getvalue())

        self.assertEqual(payload["state"], "ok")
        for key in ("state_detail", "earlier", "later", "more_earlier", "more_later"):
            self.assertIn(key, payload)

        window = payload["window"]
        from_epoch = int(util.parse_cam_time(from_cam).timestamp())
        to_epoch = int(util.parse_cam_time(to_cam).timestamp())
        self.assertEqual(window["from_epoch"], from_epoch)
        self.assertEqual(window["to_epoch"], to_epoch)
        self.assertEqual(len(window["segments"]), 1)
        self.assertEqual(window["segments"][0]["start_epoch"], from_epoch)
        self.assertEqual(window["segments"][0]["end_epoch"], to_epoch)
        self.assertEqual(window["segments"][0]["start_pct"], 0.0)
        self.assertEqual(window["segments"][0]["end_pct"], 100.0)
        self.assertTrue(window["has_eligible"])

    def test_win_widens_the_coverage_window_but_not_neighbours(self):
        day = datetime(2026, 10, 13, tzinfo=datetime.now().astimezone().tzinfo)
        early = self._clip(
            util.to_cam_time(day.replace(hour=8, minute=0).astimezone(timezone.utc)),
            util.to_cam_time(day.replace(hour=8, minute=10).astimezone(timezone.utc)),
        )
        current = self._clip(
            util.to_cam_time(day.replace(hour=8, minute=14).astimezone(timezone.utc)),
            util.to_cam_time(day.replace(hour=8, minute=30).astimezone(timezone.utc)),
        )
        cam = mock.Mock()
        cam.search.return_value = [early, current]
        app = viewer_app.ViewerApp()
        app.cam = cam
        handler = _FakeBodyHandler({})
        from_cam = util.to_cam_time(day.replace(hour=8, minute=20).astimezone(timezone.utc))
        to_cam = util.to_cam_time(day.replace(hour=8, minute=30).astimezone(timezone.utc))
        win_cam = util.to_cam_time(day.replace(hour=8, minute=0).astimezone(timezone.utc))

        app.route_neighbours(handler, {"from": [from_cam], "to": [to_cam], "win": [win_cam]})
        payload = json.loads(handler.wfile.getvalue())

        self.assertEqual(payload["state"], "ok")
        # Neighbours stay relative to the session start (08:20): the 08:00 clip
        # ends earlier and must still appear as a jump.
        self.assertEqual(len(payload["earlier"]), 1)
        # The coverage window now spans the original window start, 08:00.
        window = payload["window"]
        win_epoch = int(util.parse_cam_time(win_cam).timestamp())
        from_epoch = int(util.parse_cam_time(from_cam).timestamp())
        self.assertEqual(window["from_epoch"], win_epoch)
        self.assertLess(window["from_epoch"], from_epoch)
        self.assertEqual(window["segments"][0]["start_epoch"], win_epoch)
        # The preview target follows the session start, not the window start.
        self.assertEqual(window["target_epoch"], from_epoch)

    def test_unknown_search_state_omits_window(self):
        cam = mock.Mock()
        cam.search.side_effect = camera.ServiceDownError("down")
        app = viewer_app.ViewerApp()
        app.cam = cam
        handler = _FakeBodyHandler({})
        day = datetime(2026, 10, 13, tzinfo=datetime.now().astimezone().tzinfo)
        from_cam = util.to_cam_time(day.replace(hour=8, minute=10).astimezone(timezone.utc))
        to_cam = util.to_cam_time(day.replace(hour=8, minute=20).astimezone(timezone.utc))

        app.route_neighbours(handler, {"from": [from_cam], "to": [to_cam]})
        payload = json.loads(handler.wfile.getvalue())
        self.assertEqual(payload["state"], "service_down")
        self.assertNotIn("window", payload)

    def test_truncated_search_omits_window_but_keeps_neighbours(self):
        day = datetime(2026, 10, 13, tzinfo=datetime.now().astimezone().tzinfo)
        clip = self._clip(
            util.to_cam_time(day.replace(hour=1).astimezone(timezone.utc)),
            util.to_cam_time(day.replace(hour=1, minute=10).astimezone(timezone.utc)),
        )
        cam = mock.Mock()
        cam.search.return_value = [clip] * 200  # hits route_coverage's 200 cap
        app = viewer_app.ViewerApp()
        app.cam = cam
        handler = _FakeBodyHandler({})
        from_cam = util.to_cam_time(day.replace(hour=8, minute=10).astimezone(timezone.utc))
        to_cam = util.to_cam_time(day.replace(hour=8, minute=20).astimezone(timezone.utc))

        app.route_neighbours(handler, {"from": [from_cam], "to": [to_cam]})
        payload = json.loads(handler.wfile.getvalue())
        # Truncated day = unknown coverage: never a fabricated "all gap" window.
        self.assertTrue(payload["truncated"])
        self.assertNotIn("window", payload)
        for key in ("earlier", "later", "more_earlier", "more_later"):
            self.assertIn(key, payload)


class PlayPageTests(unittest.TestCase):
    """page_play keeps the session start and the original window start separate."""

    def _render(self, query):
        app = viewer_app.ViewerApp()
        app.cam = camera.MockCamera("http://mock", "u", "p", "rtsp://mock", 5)
        handler = _FakeBodyHandler({})
        app.page_play(handler, query)
        return handler.wfile.getvalue().decode()

    def test_window_defaults_to_the_session_start(self):
        html = self._render({"from": ["20261012T190000Z"], "to": ["20261012T192000Z"]})
        from_epoch = int(util.parse_cam_time("20261012T190000Z").timestamp())
        self.assertIn(f'data-from-epoch="{from_epoch}"', html)
        self.assertIn(f'data-window-from-epoch="{from_epoch}"', html)

    def test_window_param_preserves_the_original_window(self):
        html = self._render(
            {
                "from": ["20261012T191200Z"],
                "to": ["20261012T192000Z"],
                "win": ["20261012T190000Z"],
            }
        )
        from_epoch = int(util.parse_cam_time("20261012T191200Z").timestamp())
        win_epoch = int(util.parse_cam_time("20261012T190000Z").timestamp())
        self.assertIn(f'data-from-epoch="{from_epoch}"', html)
        self.assertIn(f'data-window-from-epoch="{win_epoch}"', html)

    def test_window_after_the_session_is_clamped_back(self):
        html = self._render(
            {
                "from": ["20261012T191200Z"],
                "to": ["20261012T192000Z"],
                "win": ["20261012T191800Z"],
            }
        )
        from_epoch = int(util.parse_cam_time("20261012T191200Z").timestamp())
        self.assertIn(f'data-window-from-epoch="{from_epoch}"', html)

    def test_caveat_names_the_previous_start_time(self):
        html = self._render({"from": ["20261012T190000Z"], "to": ["20261012T192000Z"]})
        self.assertIn("previous start time", html)
        self.assertNotIn("previous position", html)

    def test_results_link_returns_to_the_clip_day_and_hour(self):
        html = self._render({"from": ["20261012T190000Z"], "to": ["20261012T192000Z"]})
        local = util.parse_cam_time("20261012T190000Z").astimezone()
        # The href is HTML-escaped (& -> &amp;) in the attribute.
        expected = f'href="/recordings?date={local.strftime("%Y-%m-%d")}&amp;hour={local.hour}"'
        self.assertIn(expected, html)


class DownloadSniffTests(unittest.TestCase):
    """The download's type and extension come from the bytes, not a guess."""

    def test_mpeg_program_stream(self):
        data = b"\x00\x00\x01\xba" + b"\x00" * 20
        self.assertEqual(viewer_app.sniff_download_container(data), ("video/mpeg", "mpg"))

    def test_iso_base_media_ftyp_box(self):
        data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16
        self.assertEqual(viewer_app.sniff_download_container(data), ("video/mp4", "mp4"))

    def test_mpeg_transport_stream(self):
        ts = bytearray(200)
        ts[0] = 0x47
        ts[188] = 0x47
        self.assertEqual(viewer_app.sniff_download_container(bytes(ts)), ("video/mp2t", "ts"))

    def test_hikvision_imkh_header_wrapped_mpeg_program_stream(self):
        # Measured on the device: a 40-byte "IMKH" header, then the MPEG-PS pack
        # start, and ffprobe reads the whole file as mpeg. Without this the stream
        # falls back to .bin, hiding a playable video.
        data = b"IMKH" + b"\x00" * 36 + b"\x00\x00\x01\xba" + b"\x00" * 20
        self.assertEqual(viewer_app.sniff_download_container(data), ("video/mpeg", "mpg"))

    def test_imkh_magic_alone_is_not_evidence_of_mpeg(self):
        data = b"IMKH" + b"\x00" * 400
        self.assertEqual(
            viewer_app.sniff_download_container(data), ("application/octet-stream", "bin")
        )

    def test_lone_sync_byte_is_not_enough_to_claim_transport_stream(self):
        # A TS packet is 188 bytes; a single 0x47 is coincidence, not evidence.
        data = b"\x47" + b"\x00" * 20
        self.assertEqual(
            viewer_app.sniff_download_container(data), ("application/octet-stream", "bin")
        )

    def test_unknown_bytes_stay_opaque(self):
        data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        self.assertEqual(
            viewer_app.sniff_download_container(data), ("application/octet-stream", "bin")
        )

    def test_empty_body_stays_opaque(self):
        self.assertEqual(
            viewer_app.sniff_download_container(b""), ("application/octet-stream", "bin")
        )


class DownloadRouteTests(unittest.TestCase):
    """route_download relays the camera stream and stays typed on failure."""

    class _FakeDownload:
        """requests-style streaming response stand-in (no camera, no network)."""

        def __init__(self, chunks, content_type="video/mp4"):
            self.status_code = 200
            self.headers = {"Content-Type": content_type}
            self._chunks = chunks
            self.closed = False
            self.chunk_size = None

        def iter_content(self, chunk_size):
            self.chunk_size = chunk_size
            yield from self._chunks

        def close(self):
            self.closed = True

    WINDOW = {"from": ["20261012T194808Z"], "to": ["20261012T201036Z"]}

    def _app(self):
        app = viewer_app.ViewerApp()
        app.cam = camera.MockCamera("http://mock", "u", "p", "rtsp://mock", 5)
        return app

    def test_bad_clip_parameters_are_a_typed_400(self):
        handler = _FakeBodyHandler({})
        self._app().route_download(handler, {"from": ["nope"], "to": ["nope"]})
        self.assertEqual(handler.status, 400)
        self.assertIn(b"Bad clip parameters", handler.wfile.getvalue())

    def test_relays_the_camera_stream_with_download_headers(self):
        app = self._app()
        fake = self._FakeDownload([b"\x00\x00\x00\x18ftypmp42", b"", b"cdef"])
        captured = {}

        def fake_download(uri_escaped):
            captured["uri"] = uri_escaped
            return fake

        app.cam.download_stream = fake_download
        handler = _FakeBodyHandler({})
        app.route_download(
            handler,
            {
                "from": ["20261012T194808Z"],
                "to": ["20261012T201036Z"],
                "name": ["ch01_00000000000000000"],
                "size": ["264532808"],
            },
        )
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.wfile.getvalue(), b"\x00\x00\x00\x18ftypmp42cdef")
        self.assertTrue(fake.closed)
        self.assertEqual(fake.chunk_size, viewer_app.DOWNLOAD_CHUNK)
        # The rebuilt playback URI keeps the camera's &amp; escaping and carries
        # the file name/size the picker passed.
        self.assertIn("starttime=20261012T194808Z&amp;endtime=20261012T201036Z", captured["uri"])
        self.assertIn("&amp;name=ch01_00000000000000000", captured["uri"])
        self.assertIn("&amp;size=264532808", captured["uri"])
        self.assertEqual(
            handler.response_headers["Content-Disposition"],
            'attachment; filename="clip_20261012T194808Z_20261012T201036Z.mp4"',
        )
        self.assertEqual(handler.response_headers["Content-Type"], "video/mp4")

    def test_download_body_uses_the_configured_rtsp_scheme(self):
        # With TLS on, the ISAPI playback body must carry rtsps, not rtsp.
        app = self._app()
        captured = {}

        def fake_download(uri):
            captured["uri"] = uri
            return self._FakeDownload([b"\x00\x00\x00\x18ftypmp42"])

        app.cam.download_stream = fake_download
        with mock.patch.object(config, "RTSP_SCHEME", "rtsps"):
            app.route_download(_FakeBodyHandler({}), self.WINDOW)
        self.assertTrue(captured["uri"].startswith("rtsps://"))
        self.assertIn("/Streaming/tracks/101/", captured["uri"])

    def test_single_chunk_body_is_relayed_intact(self):
        # The first chunk is read early to sniff the container, so it must still
        # be written exactly once: not dropped and not duplicated.
        app = self._app()
        body = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256))
        fake = self._FakeDownload([body])
        app.cam.download_stream = lambda uri: fake
        handler = _FakeBodyHandler({})
        app.route_download(handler, self.WINDOW)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.wfile.getvalue(), body)
        self.assertEqual(
            handler.response_headers["Content-Disposition"],
            'attachment; filename="clip_20261012T194808Z_20261012T201036Z.mp4"',
        )

    def test_mpeg_program_stream_download_is_named_mpg(self):
        # The extension and type follow the sniffed container, not a hard-coded
        # .mp4 -- the real device serves MPEG-PS here.
        app = self._app()
        body = b"\x00\x00\x01\xba" + bytes(range(256))
        app.cam.download_stream = lambda uri: self._FakeDownload([body])
        handler = _FakeBodyHandler({})
        app.route_download(handler, self.WINDOW)
        self.assertEqual(handler.wfile.getvalue(), body)
        self.assertEqual(handler.response_headers["Content-Type"], "video/mpeg")
        self.assertEqual(
            handler.response_headers["Content-Disposition"],
            'attachment; filename="clip_20261012T194808Z_20261012T201036Z.mpg"',
        )

    def test_unsafe_name_is_dropped_not_embedded(self):
        app = self._app()
        captured = {}

        def fake_download(uri):
            captured["uri"] = uri
            return self._FakeDownload([])

        app.cam.download_stream = fake_download
        # A name the camera could never report must never reach the XML body.
        handler = _FakeBodyHandler({})
        app.route_download(handler, {**self.WINDOW, "name": ["x</playbackURI><y>"]})
        self.assertNotIn("</playbackURI>", captured["uri"])
        self.assertNotIn("name=", captured["uri"])

    def test_refused_credentials_render_the_credentials_card(self):
        app = self._app()
        app.cam.download_stream = mock.Mock(side_effect=camera.CredentialsRejectedError("401"))
        handler = _FakeBodyHandler({})
        app.route_download(handler, self.WINDOW)
        self.assertEqual(handler.status, 502)
        body = handler.wfile.getvalue().decode()
        self.assertIn("refused the viewer", body)
        self.assertNotIn("Traceback", body)

    def test_service_down_offers_a_retry(self):
        app = self._app()
        app.cam.download_stream = mock.Mock(side_effect=camera.ServiceDownError("boom"))
        handler = _FakeBodyHandler({})
        app.route_download(handler, self.WINDOW)
        self.assertEqual(handler.status, 503)
        self.assertIn("/download?from=20261012T194808Z", handler.wfile.getvalue().decode())

    def test_client_going_away_mid_stream_is_quiet(self):
        app = self._app()

        class _BrokenStream(DownloadRouteTests._FakeDownload):
            def iter_content(self, chunk_size):
                yield b"first"
                raise BrokenPipeError("client gone")

        fake = _BrokenStream([])
        app.cam.download_stream = lambda uri: fake
        handler = _FakeBodyHandler({})
        app.route_download(handler, self.WINDOW)
        self.assertTrue(fake.closed)

    def test_mock_encoder_failure_raises_a_typed_error_with_stderr(self):
        # A failed encoder is a failed reply, not an empty file: the generator
        # names ffmpeg's stderr instead of silently yielding nothing.
        failed = subprocess.CompletedProcess([], 1, b"", b"boom: no such filter")
        with mock.patch.object(streams.subprocess, "run", return_value=failed):
            with self.assertRaises(camera.BadResponseError) as ctx:
                list(streams.MockDownloadResponse().iter_content(viewer_app.DOWNLOAD_CHUNK))
        self.assertIn("boom: no such filter", str(ctx.exception))

    def test_mock_snapshot_encoder_failure_raises_a_typed_error(self):
        # Same rule as the download, and the reason it matters: route_snapshot
        # only catches camera.CameraError, so a bare RuntimeError escaped as an
        # unhandled 500 instead of the typed card the other failures get.
        failed = subprocess.CompletedProcess([], 1, b"", b"snapshot boom")
        with mock.patch.object(streams.subprocess, "run", return_value=failed):
            with self.assertRaises(camera.BadResponseError) as ctx:
                streams.mock_snapshot_jpeg()
        self.assertIn("snapshot boom", str(ctx.exception))

    def test_failed_mock_encoder_is_a_typed_error_not_an_empty_200(self):
        # Drive the real mock response through the route: the download must not
        # answer 200 with a 0-byte attachment, and the stderr must reach the card.
        app = self._app()
        failed = subprocess.CompletedProcess([], 1, b"", b"forced mock encoder failure")
        with (
            mock.patch.object(config, "MOCK", True),
            mock.patch.object(streams.subprocess, "run", return_value=failed),
        ):
            handler = _FakeBodyHandler({})
            app.route_download(handler, self.WINDOW)
        body = handler.wfile.getvalue().decode()
        self.assertEqual(handler.status, 502)
        self.assertIn("no data", body)
        self.assertIn("forced mock encoder failure", body)
        self.assertIn("/download?from=20261012T194808Z", body)  # Retry affordance

    def test_successful_mock_generation_still_relays_bytes(self):
        # The happy path must survive the failure guard: bytes in, same bytes out.
        app = self._app()
        body = b"\x00\x00\x00\x18ftypmp42" + bytes(range(64))
        ok = subprocess.CompletedProcess([], 0, body, b"")
        with (
            mock.patch.object(config, "MOCK", True),
            mock.patch.object(streams.subprocess, "run", return_value=ok),
        ):
            handler = _FakeBodyHandler({})
            app.route_download(handler, self.WINDOW)
        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.wfile.getvalue(), body)
        self.assertEqual(handler.response_headers["Content-Type"], "video/mp4")
        self.assertEqual(
            handler.response_headers["Content-Disposition"],
            'attachment; filename="clip_20261012T194808Z_20261012T201036Z.mp4"',
        )

    def test_empty_camera_reply_is_a_typed_error_not_an_empty_200(self):
        # A real camera that answers 200 with no bytes is a bad reply, not a
        # successful empty download. It must be typed and offer a Retry.
        app = self._app()
        app.cam.download_stream = lambda uri: self._FakeDownload([])
        handler = _FakeBodyHandler({})
        app.route_download(handler, self.WINDOW)
        self.assertEqual(handler.status, 502)
        body = handler.wfile.getvalue().decode()
        self.assertIn("no data", body)
        self.assertIn("/download?from=20261012T194808Z", body)


class ConfigFileTests(unittest.TestCase):
    """VIEWER_CONFIG: strict loading, HIK_* fallback, and TLS scheme selection."""

    def _write(self, payload) -> str:
        path = Path(tempfile.mkdtemp()) / "camera.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        return str(path)

    # ── loading and validation ───────────────────────────────────────
    def test_loads_the_camera_fields_the_file_sets(self):
        path = self._write(
            {
                "camera": {
                    "host": "10.0.0.9",
                    "http_port": 8080,
                    "rtsp_port": 8554,
                    "user": "viewer",
                    "password": "secret",
                    "tls": True,
                    "rtsp_tls": False,
                    "tls_verify": False,
                }
            }
        )
        self.assertEqual(
            config_file.load(path),
            {
                "host": "10.0.0.9",
                "http_port": 8080,
                "rtsp_port": 8554,
                "user": "viewer",
                "password": "secret",
                "tls": True,
                "rtsp_tls": False,
                "tls_verify": False,
            },
        )

    def test_missing_file_names_the_path(self):
        path = "/nonexistent/definitely/camera.json"
        with self.assertRaises(config_file.ConfigFileError) as ctx:
            config_file.load(path)
        self.assertIn(path, str(ctx.exception))
        self.assertIn("cannot read", str(ctx.exception))

    def test_malformed_json_names_the_path(self):
        path = self._write("{not json")
        with self.assertRaises(config_file.ConfigFileError) as ctx:
            config_file.load(path)
        self.assertIn(path, str(ctx.exception))
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_a_non_object_top_level_is_refused(self):
        path = self._write([{"host": "cam"}])
        with self.assertRaises(config_file.ConfigFileError) as ctx:
            config_file.load(path)
        self.assertIn("must contain a JSON object", str(ctx.exception))

    def test_unknown_camera_key_is_named(self):
        path = self._write({"camera": {"usr": "admin"}})
        with self.assertRaises(config_file.ConfigFileError) as ctx:
            config_file.load(path)
        self.assertIn('"usr"', str(ctx.exception))

    def test_unknown_top_level_key_is_named(self):
        path = self._write({"camera": {}, "cam": {}})
        with self.assertRaises(config_file.ConfigFileError) as ctx:
            config_file.load(path)
        self.assertIn('"cam"', str(ctx.exception))

    def test_a_two_entry_cameras_array_is_refused(self):
        path = self._write({"cameras": [{"host": "a"}, {"host": "b"}]})
        with self.assertRaises(config_file.ConfigFileError) as ctx:
            config_file.load(path)
        self.assertIn("one camera per instance", str(ctx.exception))

    def test_a_one_entry_cameras_array_is_accepted(self):
        path = self._write({"cameras": [{"host": "solo"}]})
        self.assertEqual(config_file.load(path), {"host": "solo"})

    def test_empty_host_is_refused(self):
        path = self._write({"camera": {"host": "   "}})
        with self.assertRaises(config_file.ConfigFileError):
            config_file.load(path)

    def test_a_string_port_is_refused(self):
        path = self._write({"camera": {"http_port": "80"}})
        with self.assertRaises(config_file.ConfigFileError) as ctx:
            config_file.load(path)
        self.assertIn("http_port", str(ctx.exception))

    def test_a_port_out_of_range_is_refused(self):
        path = self._write({"camera": {"rtsp_port": 70000}})
        with self.assertRaises(config_file.ConfigFileError):
            config_file.load(path)

    def test_a_boolean_port_is_not_an_integer_port(self):
        # bool is an int subclass in Python; JSON true must not become port 1.
        path = self._write({"camera": {"http_port": True}})
        with self.assertRaises(config_file.ConfigFileError):
            config_file.load(path)

    def test_a_non_boolean_tls_is_refused(self):
        path = self._write({"camera": {"tls": "true"}})
        with self.assertRaises(config_file.ConfigFileError) as ctx:
            config_file.load(path)
        self.assertIn("tls", str(ctx.exception))

    def test_a_non_boolean_rtsp_tls_is_refused(self):
        path = self._write({"camera": {"rtsp_tls": "true"}})
        with self.assertRaises(config_file.ConfigFileError) as ctx:
            config_file.load(path)
        self.assertIn("rtsp_tls", str(ctx.exception))

    # ── precedence (file over HIK_*) ─────────────────────────────────
    def test_omitted_fields_fall_back_to_the_environment(self):
        settings, notes = config_file.resolve_camera(
            {"HIK_HOST": "envcam", "HIK_RTSP_PORT": "8554"},
            {"user": "fileuser"},
        )
        self.assertEqual(settings.host, "envcam")
        self.assertEqual(settings.rtsp_port, 8554)
        self.assertEqual(settings.user, "fileuser")
        self.assertEqual(notes, [])

    def test_file_wins_over_a_disagreeing_environment_and_says_so(self):
        settings, notes = config_file.resolve_camera(
            {"HIK_HOST": "envcam"},
            {"host": "filecam"},
        )
        self.assertEqual(settings.host, "filecam")
        self.assertEqual(len(notes), 1)
        self.assertIn("camera.host", notes[0])
        self.assertIn("HIK_HOST", notes[0])
        self.assertIn("filecam", notes[0])

    def test_agreeing_values_produce_no_note(self):
        _, notes = config_file.resolve_camera(
            {"HIK_HOST": "same"},
            {"host": "same"},
        )
        self.assertEqual(notes, [])

    def test_a_password_mismatch_never_prints_the_password(self):
        settings, notes = config_file.resolve_camera(
            {"HIK_PASSWORD": "env-secret"},
            {"password": "file-secret"},
        )
        self.assertEqual(settings.password, "file-secret")
        self.assertEqual(len(notes), 1)
        self.assertIn("HIK_PASSWORD", notes[0])
        self.assertNotIn("file-secret", notes[0])
        self.assertNotIn("env-secret", notes[0])

    def test_defaults_are_untouched_when_no_file_is_used(self):
        settings, notes = config_file.resolve_camera({}, {})
        self.assertEqual(settings.host, config_file.DEFAULT_HOST)
        self.assertEqual(settings.user, config_file.DEFAULT_USER)
        self.assertEqual(settings.http_port, config_file.DEFAULT_HTTP_PORT)
        self.assertEqual(settings.rtsp_port, config_file.DEFAULT_RTSP_PORT)
        self.assertFalse(settings.tls)
        self.assertFalse(settings.tls_verify)
        self.assertEqual(notes, [])

    # ── TLS scheme selection ─────────────────────────────────────────
    def test_tls_builds_https_and_rtsps_urls(self):
        settings, notes = config_file.resolve_camera(
            {"HIK_HOST": "cam.lan"},
            {"tls": True, "tls_verify": False, "http_port": 443, "rtsp_port": 322},
        )
        self.assertEqual(config_file.isapi_base(settings), "https://cam.lan:443")
        self.assertEqual(config_file.rtsp_base(settings), "rtsps://cam.lan:322")
        self.assertIn("verification is off", notes[0])

    def test_non_tls_builds_todays_urls(self):
        settings, _ = config_file.resolve_camera({}, {})
        self.assertEqual(config_file.isapi_base(settings), "http://192.168.0.156:80")
        self.assertEqual(config_file.rtsp_base(settings), "rtsp://192.168.0.156:554")

    def test_tls_verify_toggles_verification(self):
        on, _ = config_file.resolve_camera({}, {"tls": True, "tls_verify": True})
        off, _ = config_file.resolve_camera({}, {"tls": True, "tls_verify": False})
        default, _ = config_file.resolve_camera({}, {"tls": True})
        self.assertTrue(on.tls_verify)
        self.assertFalse(off.tls_verify)
        self.assertFalse(default.tls_verify)  # documented default

    # ── independent rtsp_tls ────────────────────────────────────────
    def test_rtsp_tls_defaults_to_tls(self):
        # Omitting rtsp_tls must behave exactly like the old single flag: both
        # services follow tls, so every pre-existing file is unchanged.
        omitted, _ = config_file.resolve_camera({}, {"tls": True})
        explicit, _ = config_file.resolve_camera({}, {"tls": True, "rtsp_tls": True})
        self.assertTrue(omitted.rtsp_tls)
        self.assertEqual(config_file.rtsp_base(omitted), config_file.rtsp_base(explicit))
        # ...and with tls off it stays off.
        off, _ = config_file.resolve_camera({}, {"tls": False})
        self.assertFalse(off.rtsp_tls)
        self.assertEqual(config_file.rtsp_base(off), "rtsp://192.168.0.156:554")

    def test_https_isapi_pairs_with_plain_rtsp(self):
        # This camera serves HTTPS on 443 but no RTSPS: the useful combination.
        settings, _ = config_file.resolve_camera(
            {},
            {"tls": True, "rtsp_tls": False, "http_port": 443, "rtsp_port": 554},
        )
        self.assertEqual(config_file.isapi_base(settings), "https://192.168.0.156:443")
        self.assertEqual(config_file.rtsp_base(settings), "rtsp://192.168.0.156:554")

    def test_plain_isapi_pairs_with_rtsps(self):
        settings, _ = config_file.resolve_camera(
            {},
            {"tls": False, "rtsp_tls": True, "http_port": 80, "rtsp_port": 322},
        )
        self.assertEqual(config_file.isapi_base(settings), "http://192.168.0.156:80")
        self.assertEqual(config_file.rtsp_base(settings), "rtsps://192.168.0.156:322")

    def test_rtsp_tls_alone_warns_when_verification_is_off(self):
        # TLS on either service with verification off is worth a startup note.
        _, notes = config_file.resolve_camera({}, {"tls": False, "rtsp_tls": True})
        self.assertEqual(len(notes), 1)
        self.assertIn("verification is off", notes[0])


class StreamTlsTests(unittest.TestCase):
    """The ffmpeg command grows RTSP TLS options only when rtsp_tls is on."""

    def test_tls_off_adds_nothing_and_keeps_todays_rtsp_url(self):
        with mock.patch.multiple(
            config,
            CAM_TLS=False,
            CAM_RTSP_TLS=False,
            CAM_TLS_VERIFY=False,
            RTSP_SCHEME="rtsp",
            CAM_HOST="cam",
            CAM_USER="u",
            CAM_PASS="p",
            RTSP_PORT=554,
        ):
            self.assertEqual(streams._tls_input_options(), [])
            self.assertEqual(
                streams._camera_rtsp("101"),
                "rtsp://u:p@cam:554/Streaming/channels/101",
            )

    def test_tls_on_uses_rtsps_and_the_verify_skip_flag(self):
        with mock.patch.multiple(
            config,
            CAM_TLS=True,
            CAM_RTSP_TLS=True,
            CAM_TLS_VERIFY=False,
            RTSP_SCHEME="rtsps",
            CAM_HOST="cam",
            CAM_USER="u",
            CAM_PASS="p",
            RTSP_PORT=322,
        ):
            self.assertEqual(streams._tls_input_options(), ["-tls_verify", "0"])
            self.assertEqual(
                streams._camera_rtsp("101"),
                "rtsps://u:p@cam:322/Streaming/channels/101",
            )

    def test_tls_on_with_verification_asks_ffmpeg_to_verify(self):
        with mock.patch.multiple(config, CAM_TLS=True, CAM_RTSP_TLS=True, CAM_TLS_VERIFY=True):
            self.assertEqual(streams._tls_input_options(), ["-tls_verify", "1"])

    def test_https_isapi_keeps_plain_rtsp_and_no_verify_flag(self):
        # The useful combination on this camera: HTTPS ISAPI, plain RTSP. The
        # -tls_verify input options belong to the RTSP connection, so they must
        # not appear just because ISAPI is on HTTPS.
        with mock.patch.multiple(
            config,
            MOCK=False,
            CAM_TLS=True,
            CAM_RTSP_TLS=False,
            CAM_TLS_VERIFY=False,
            RTSP_SCHEME="rtsp",
            CAM_HOST="cam",
            CAM_USER="u",
            CAM_PASS="p",
            RTSP_PORT=554,
        ):
            self.assertEqual(streams._tls_input_options(), [])
            live = streams._live_cmd("101", Path("/tmp/x"), True)
            play = streams._play_cmd("20261012T194808Z", "20261012T201036Z", Path("/tmp/x"), True)
        self.assertNotIn("-tls_verify", live)
        self.assertNotIn("-tls_verify", play)
        self.assertEqual(live[live.index("-i") + 1], "rtsp://u:p@cam:554/Streaming/channels/101")
        self.assertTrue(
            play[play.index("-i") + 1].startswith("rtsp://u:p@cam:554/Streaming/tracks/101")
        )

    def test_rtsp_tls_alone_uses_rtsps_even_when_isapi_is_plain(self):
        with mock.patch.multiple(
            config,
            MOCK=False,
            CAM_TLS=False,
            CAM_RTSP_TLS=True,
            CAM_TLS_VERIFY=False,
            RTSP_SCHEME="rtsps",
            CAM_HOST="cam",
            CAM_USER="u",
            CAM_PASS="p",
            RTSP_PORT=322,
        ):
            self.assertEqual(streams._tls_input_options(), ["-tls_verify", "0"])
            cmd = streams._live_cmd("101", Path("/tmp/x"), True)
        self.assertIn("rtsps://u:p@cam:322/Streaming/channels/101", cmd[cmd.index("-i") + 1])
        self.assertEqual(cmd[cmd.index("-tls_verify") + 1], "0")

    def test_tls_off_keeps_todays_live_command(self):
        with mock.patch.multiple(
            config,
            MOCK=False,
            CAM_TLS=False,
            CAM_RTSP_TLS=False,
            RTSP_SCHEME="rtsp",
            CAM_HOST="cam",
            CAM_USER="u",
            CAM_PASS="p",
            RTSP_PORT=554,
        ):
            cmd = streams._live_cmd("101", Path("/tmp/x"), True)
        self.assertNotIn("-tls_verify", cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], "rtsp://u:p@cam:554/Streaming/channels/101")

    def test_tls_off_keeps_todays_playback_command(self):
        with mock.patch.multiple(
            config,
            MOCK=False,
            CAM_TLS=False,
            CAM_RTSP_TLS=False,
            RTSP_SCHEME="rtsp",
            CAM_HOST="cam",
            CAM_USER="u",
            CAM_PASS="p",
            RTSP_PORT=554,
        ):
            cmd = streams._play_cmd("20261012T194808Z", "20261012T201036Z", Path("/tmp/x"), True)
        self.assertNotIn("-tls_verify", cmd)
        self.assertEqual(
            cmd[cmd.index("-i") + 1],
            "rtsp://u:p@cam:554/Streaming/tracks/101"
            "?starttime=20261012T194808Z&endtime=20261012T201036Z",
        )

    def test_tls_on_uses_rtsps_in_the_playback_command(self):
        with mock.patch.multiple(
            config,
            MOCK=False,
            CAM_TLS=True,
            CAM_RTSP_TLS=True,
            CAM_TLS_VERIFY=False,
            RTSP_SCHEME="rtsps",
            CAM_HOST="cam",
            CAM_USER="u",
            CAM_PASS="p",
            RTSP_PORT=322,
        ):
            cmd = streams._play_cmd("20261012T194808Z", "20261012T201036Z", Path("/tmp/x"), True)
        self.assertIn("rtsps://u:p@cam:322/Streaming/tracks/101", cmd[cmd.index("-i") + 1])
        self.assertLess(cmd.index("-tls_verify"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-tls_verify") + 1], "0")


class CameraVerifyTests(unittest.TestCase):
    def test_verify_defaults_to_requests_default(self):
        cam = camera.Camera("http://cam", "u", "p", "rtsp://cam", 5)
        self.assertTrue(cam.session.verify)

    def test_verify_false_is_carried_to_the_session(self):
        cam = camera.Camera("https://cam", "u", "p", "rtsps://cam", 5, verify=False)
        self.assertFalse(cam.session.verify)


class PlaybackUriSchemeTests(unittest.TestCase):
    def test_normalize_rewrites_to_the_configured_scheme(self):
        uri = "rtsp://old:554/Streaming/tracks/101?starttime=x"
        self.assertEqual(
            util.normalize_playback_uri(uri, "rtsps://cam:322"),
            "rtsps://cam:322/Streaming/tracks/101?starttime=x",
        )
        self.assertEqual(
            util.normalize_playback_uri(uri, "rtsp://cam:554"),
            "rtsp://cam:554/Streaming/tracks/101?starttime=x",
        )


if __name__ == "__main__":
    unittest.main()
