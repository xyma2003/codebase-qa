import copy
import unittest

from scripts.check_storage_platform import verify_records


class StoragePlatformTests(unittest.TestCase):
    def setUp(self):
        self.before = {"python": "3.12.13", "sqlite": "3.40.1", "uid": 10001, "system": "Linux", "distribution": {"ID": "debian", "VERSION_ID": "12"}}
        self.after = dict(self.before, sqlite="3.46.1", distribution={"ID": "debian", "VERSION_ID": "13"})
        self.restore = {"status": "stopped_service_restore_passed", "cross_owner_denied": True, "source_process_exited_before_backup": True,
                        "original_path_replaced_for_readback": True, "source_snapshot_checked": True,
                        "native_turns_restored": 2, "vectors_restored": 1, "model_requests": 0}

    def test_valid_runtime_transition_requires_vector_messages_and_owners(self):
        report = verify_records(self.before, self.after, self.restore)
        self.assertTrue(report["old_data_read_on_new_image"])
        self.assertEqual(report["real_model_requests"], 0)

    def test_wrong_interpreter_distribution_or_root_cannot_pass(self):
        for change in ({"python": "3.13.0"}, {"uid": 0}, {"distribution": None}, {"distribution": {"ID": "debian", "VERSION_ID": "12"}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_records(self.before, dict(self.after, **change), self.restore)

    def test_python_must_use_new_sqlite_library(self):
        with self.assertRaises(ValueError):
            verify_records(self.before, dict(self.after, sqlite="3.40.1"), self.restore)

    def test_status_label_cannot_replace_successful_independent_checks(self):
        for key, value in (("cross_owner_denied", False), ("source_snapshot_checked", False), ("native_turns_restored", 1), ("vectors_restored", 0), ("model_requests", 1)):
            report = copy.deepcopy(self.restore)
            report[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                verify_records(self.before, self.after, report)


if __name__ == "__main__":
    unittest.main()
