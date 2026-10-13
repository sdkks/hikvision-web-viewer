"""Unit tests for time/URI helpers and the mock camera pipeline."""

import unittest
from datetime import timezone
from unittest import mock

from viewer import app as viewer_app
from viewer import camera, config, util


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


if __name__ == "__main__":
    unittest.main()
