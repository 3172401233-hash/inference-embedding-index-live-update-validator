"""在线向量索引可见性验证的单元测试（python -m unittest）。"""

import unittest

from live_index import BuildState, IndexManager, Segment


def make_manager_with_old_segment():
    """旧 segment：gen0，已发布，含 a(v1) b(v2) c(v3)。"""
    mgr = IndexManager()
    old = Segment(segment_id="seg_old", generation=0, state=BuildState.READY, published=True)
    mgr.add_segment(old)
    mgr.insert("seg_old", "a", 1)
    mgr.insert("seg_old", "b", 2)
    mgr.insert("seg_old", "c", 3)
    return mgr


class TestOnlineUpdate(unittest.TestCase):
    """在线更新：构建中新段不可见；切换窗口双读；切换完成只读新段。"""

    def test_building_segment_not_visible(self):
        mgr = make_manager_with_old_segment()
        new = Segment(segment_id="seg_new", generation=1)
        mgr.add_segment(new)
        mgr.insert("seg_new", "a", 4)   # 构建中的新段已写入更新版本
        mgr.insert("seg_new", "d", 5)

        result = mgr.query()
        self.assertEqual(result.visible_segments, ["seg_old"])
        self.assertEqual(result.visible_vectors, {"a": 1, "b": 2, "c": 3})
        self.assertEqual(result.double_read_segments, [])
        self.assertEqual(result.double_read_ids, [])

    def test_swap_window_double_read_then_single_read(self):
        mgr = make_manager_with_old_segment()
        mgr.add_segment(Segment(segment_id="seg_new", generation=1))
        mgr.insert("seg_new", "a", 4)
        mgr.insert("seg_new", "b", 2)
        mgr.insert("seg_new", "c", 3)
        mgr.insert("seg_new", "d", 5)
        mgr.finish_build("seg_new")

        # 构建完成但未发布：仍不可见
        self.assertEqual(mgr.query().visible_segments, ["seg_old"])

        mgr.begin_swap("seg_new")
        during = mgr.query()
        self.assertEqual(during.visible_segments, ["seg_old", "seg_new"])
        # 重叠向量 a/b/c 需要双读合并；a 在新段有更高版本，旧副本被屏蔽
        self.assertEqual(during.double_read_segments, [("seg_old", "seg_new")])
        self.assertEqual(during.double_read_ids, ["a", "b", "c"])
        self.assertEqual(during.visible_vectors, {"a": 4, "b": 2, "c": 3, "d": 5})
        stale = [m for m in during.masked_vectors if m.reason == "stale_version"]
        self.assertEqual([m.vector_id for m in stale], ["a"])

        mgr.finish_swap("seg_new")
        after = mgr.query()
        self.assertEqual(after.visible_segments, ["seg_new"])
        self.assertEqual(after.visible_vectors, {"a": 4, "b": 2, "c": 3, "d": 5})
        self.assertEqual(after.double_read_ids, [])

    def test_begin_swap_requires_ready(self):
        mgr = make_manager_with_old_segment()
        mgr.add_segment(Segment(segment_id="seg_new", generation=1))
        with self.assertRaises(ValueError):
            mgr.begin_swap("seg_new")


class TestTombstone(unittest.TestCase):
    """tombstone：只存在一边也生效；切换后删除不丢失。"""

    def test_tombstone_only_on_old_side_masks_during_swap(self):
        mgr = make_manager_with_old_segment()
        mgr.add_segment(Segment(segment_id="seg_new", generation=1))
        # 新段基于旧快照构建，仍携带 b(v2)
        mgr.insert("seg_new", "b", 2)
        mgr.finish_build("seg_new")
        # 删除标记只写入旧段
        mgr.delete("seg_old", "b", 6)

        mgr.begin_swap("seg_new")
        during = mgr.query()
        self.assertNotIn("b", during.visible_vectors)
        tomb = [m for m in during.masked_vectors if m.reason == "tombstone"]
        self.assertEqual([m.vector_id for m in tomb], ["b"])
        # tombstone 只落在旧段，新段仍持有 b：b 必须双读
        self.assertIn("b", during.double_read_ids)

        # 切换完成后删除仍然生效，不因旧段下线而复活
        mgr.finish_swap("seg_new")
        after = mgr.query()
        self.assertEqual(after.visible_segments, ["seg_new"])
        self.assertNotIn("b", after.visible_vectors)
        self.assertEqual([m.vector_id for m in after.masked_vectors], ["b"])

    def test_tombstone_only_on_new_side_masks(self):
        mgr = make_manager_with_old_segment()
        mgr.add_segment(Segment(segment_id="seg_new", generation=1))
        mgr.insert("seg_new", "c", 3)
        mgr.finish_build("seg_new")
        # 删除标记只写入新段
        mgr.delete("seg_new", "c", 7)

        mgr.begin_swap("seg_new")
        result = mgr.query()
        self.assertNotIn("c", result.visible_vectors)
        self.assertIn("c", result.double_read_ids)

    def test_reinsert_after_tombstone_revives_vector(self):
        mgr = make_manager_with_old_segment()
        mgr.delete("seg_old", "a", 8)
        self.assertNotIn("a", mgr.query().visible_vectors)
        mgr.insert("seg_old", "a", 9)   # 更高版本重新插入，清除 tombstone
        self.assertEqual(mgr.query().visible_vectors["a"], 9)


class TestBuildFailure(unittest.TestCase):
    """构建失败：新段永不可见，旧段持续服务，删除标记不受影响。"""

    def test_failed_build_never_visible(self):
        mgr = make_manager_with_old_segment()
        mgr.add_segment(Segment(segment_id="seg_new", generation=1))
        mgr.insert("seg_new", "a", 4)
        mgr.finish_build("seg_new", success=False)

        result = mgr.query()
        self.assertEqual(result.visible_segments, ["seg_old"])
        self.assertEqual(result.visible_vectors, {"a": 1, "b": 2, "c": 3})

        with self.assertRaises(ValueError):
            mgr.begin_swap("seg_new")
        with self.assertRaises(ValueError):
            mgr.insert("seg_new", "e", 5)

        # 失败期间旧段上的删除照常生效
        mgr.delete("seg_old", "b", 6)
        after = mgr.query()
        self.assertNotIn("b", after.visible_vectors)
        self.assertEqual(after.visible_segments, ["seg_old"])


class TestVersionRollback(unittest.TestCase):
    """版本回退：新段携带陈旧版本时，高版本获胜，旧副本被屏蔽。"""

    def test_stale_version_in_new_segment_does_not_rollback(self):
        mgr = make_manager_with_old_segment()
        # 旧段上 a 已更新到 v10
        mgr.insert("seg_old", "a", 10)
        mgr.add_segment(Segment(segment_id="seg_new", generation=1))
        # 新段基于过期快照构建，a 仍是 v1
        mgr.insert("seg_new", "a", 1)
        mgr.finish_build("seg_new")
        mgr.begin_swap("seg_new")

        result = mgr.query()
        self.assertEqual(result.visible_vectors["a"], 10)
        stale = [m for m in result.masked_vectors if m.reason == "stale_version"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].vector_id, "a")
        self.assertIn("rollback blocked", stale[0].detail)

    def test_tombstone_does_not_mask_newer_version(self):
        mgr = make_manager_with_old_segment()
        mgr.delete("seg_old", "a", 5)    # tombstone v5
        mgr.insert("seg_old", "a", 10)   # 重新插入 v10（清除旧 tombstone）
        mgr.add_segment(Segment(segment_id="seg_new", generation=1))
        mgr.insert("seg_new", "a", 10)
        mgr.finish_build("seg_new")
        # 新段残留过期 tombstone v5，不应屏蔽 v10
        mgr.get_segment("seg_new").tombstones["a"] = 5
        mgr.begin_swap("seg_new")

        result = mgr.query()
        self.assertEqual(result.visible_vectors["a"], 10)


class TestWatermarkSnapshot(unittest.TestCase):
    """可见性水位：快照只能看到水位线以下的版本与 tombstone。"""

    def test_snapshot_hides_newer_versions_and_tombstones(self):
        mgr = make_manager_with_old_segment()
        mgr.insert("seg_old", "d", 4)
        mgr.delete("seg_old", "b", 5)

        # 当前水位：d 可见，b 被删除
        current = mgr.query()
        self.assertEqual(current.watermark, 5)
        self.assertEqual(current.visible_vectors, {"a": 1, "c": 3, "d": 4})

        # 旧快照（水位 4）：b 尚未被删除，d 可见
        snap4 = mgr.query(watermark=4)
        self.assertEqual(snap4.visible_vectors, {"a": 1, "b": 2, "c": 3, "d": 4})

        # 更旧快照（水位 3）：d 尚不存在
        snap3 = mgr.query(watermark=3)
        self.assertEqual(snap3.visible_vectors, {"a": 1, "b": 2, "c": 3})


if __name__ == "__main__":
    unittest.main()
