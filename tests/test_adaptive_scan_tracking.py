import unittest

from v2_api.tracker import fast_poll_minutes_for_remaining, scan_progress_reaches_fast_poll


class AdaptiveScanTrackingTests(unittest.TestCase):
    def test_fast_poll_interval_uses_estimated_remaining_time(self):
        self.assertEqual(fast_poll_minutes_for_remaining(600, 100), 5)
        self.assertEqual(fast_poll_minutes_for_remaining(400, 100), 3)
        self.assertEqual(fast_poll_minutes_for_remaining(300, 100), 2)

    def test_fast_poll_interval_never_goes_below_two_minutes(self):
        self.assertEqual(fast_poll_minutes_for_remaining(1, 100), 2)
        self.assertEqual(fast_poll_minutes_for_remaining(100, 0), 5)

    def test_interval_can_change_each_cycle_as_speed_changes(self):
        remaining = 400
        self.assertEqual(fast_poll_minutes_for_remaining(remaining, 100), 3)
        self.assertEqual(fast_poll_minutes_for_remaining(remaining, 200), 2)
        self.assertEqual(fast_poll_minutes_for_remaining(remaining, 50), 5)

    def test_object_target_reaches_threshold(self):
        self.assertFalse(
            scan_progress_reaches_fast_poll(objects_scanned=899, object_target=1000)
        )
        self.assertTrue(
            scan_progress_reaches_fast_poll(objects_scanned=900, object_target=1000)
        )

    def test_part_progress_does_not_trigger_fast_polling(self):
        self.assertFalse(
            scan_progress_reaches_fast_poll(
                objects_scanned=800,
                object_target=1000,
                completed_parts=90,
                total_parts=100,
            )
        )

    def test_object_target_is_required_for_fast_polling(self):
        self.assertFalse(
            scan_progress_reaches_fast_poll(
                objects_scanned=800,
                completed_parts=89,
                total_parts=100,
            )
        )
        self.assertTrue(
            scan_progress_reaches_fast_poll(
                objects_scanned=900,
                object_target=1000,
            )
        )

    def test_invalid_or_empty_progress_does_not_enable_fast_polling(self):
        self.assertFalse(scan_progress_reaches_fast_poll(objects_scanned=0, object_target=0))
        self.assertFalse(
            scan_progress_reaches_fast_poll(
                objects_scanned="not-a-number", object_target=1000
            )
        )


if __name__ == "__main__":
    unittest.main()