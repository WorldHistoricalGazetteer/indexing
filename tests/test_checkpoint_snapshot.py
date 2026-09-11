"""`create_checkpoint_snapshot` must not call an empty backup a backup.

On 11 Sep 2026 a rebuild logged "Snapshot created: toponyms_v6" for a snapshot
holding 908 bytes across two empty indices — state SUCCESS, 8/8 shards. The
15.5-hour index it had just built was not in it, and existed only on a staging
node's ephemeral scratch. State alone cannot tell a real backup from an empty
one, so these pin the two checks that can.
"""
import unittest

from processing.utilities import create_checkpoint_snapshot


class _FakeSnapshotAPI:
    def __init__(self, captured, size):
        self._captured, self._size, self.created_with = captured, size, None

    def create(self, repository=None, snapshot=None, body=None, wait_for_completion=None):
        self.created_with = body
        return {"accepted": True}

    def get(self, repository=None, snapshot=None):
        return {"snapshots": [{"state": "SUCCESS", "indices": self._captured}]}

    def status(self, repository=None, snapshot=None):
        return {"snapshots": [{"stats": {"total": {"size_in_bytes": self._size}}}]}


class _FakeES:
    def __init__(self, captured, size):
        self.snapshot = _FakeSnapshotAPI(captured, size)


class CheckpointSnapshot(unittest.TestCase):

    IDX = "toponyms_ipafix-20260910t175025z"

    def test_a_real_snapshot_is_accepted(self):
        es = _FakeES(captured=[self.IDX], size=79_155_667_374)
        self.assertIsNotNone(
            create_checkpoint_snapshot(es, indices=[self.IDX], repo_name="r"))

    def test_the_requested_index_must_actually_be_captured(self):
        # ignore_unavailable=True means naming a missing index yields a
        # perfectly successful snapshot of something else.
        es = _FakeES(captured=["places", "toponyms"], size=5_000_000_000)
        self.assertIsNone(
            create_checkpoint_snapshot(es, indices=[self.IDX], repo_name="r"))

    def test_an_empty_snapshot_is_rejected_however_green_it_looks(self):
        # The exact shape of the 11 Sep failure: SUCCESS, right name, 908 bytes.
        es = _FakeES(captured=[self.IDX], size=908)
        self.assertIsNone(
            create_checkpoint_snapshot(es, indices=[self.IDX], repo_name="r"))

    def test_an_unreadable_size_is_not_treated_as_good(self):
        class _Broken(_FakeES):
            def __init__(self):
                super().__init__([CheckpointSnapshot.IDX], 1)
                self.snapshot.status = self._raise
            @staticmethod
            def _raise(**_):
                raise RuntimeError("no status endpoint")
        self.assertIsNone(
            create_checkpoint_snapshot(_Broken(), indices=[self.IDX], repo_name="r"))

    def test_the_caller_s_indices_reach_the_request(self):
        # If the parameter were ignored, every test above would still pass
        # while the helper snapshotted the aliases as before.
        es = _FakeES(captured=[self.IDX], size=79_155_667_374)
        create_checkpoint_snapshot(es, indices=[self.IDX], repo_name="r")
        self.assertEqual(es.snapshot.created_with["indices"], self.IDX)


if __name__ == "__main__":
    unittest.main()
