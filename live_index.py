"""在线向量索引可见性验证模型（纯内存模拟，不调用真实向量数据库）。

模拟场景：旧 segment 仍对外可见，新 segment 正在构建，删除标记（tombstone）
可能只存在于新旧索引的一边。查询基于快照（可见性水位 watermark）计算
每次查询的可见集合，并输出：
  - 可见 segment 列表
  - 被屏蔽（masked）的向量及原因
  - 需要双读（double-read）的向量范围

可见性规则：
  1. 新 segment 构建未完成（BUILDING / FAILED）不可见。
  2. 切换窗口内新旧 segment 同时可见，重叠向量必须双读合并。
  3. tombstone 在任意一个可见 segment 中存在即生效，删除不因切换暂时失效。
  4. 版本回退保护：同一向量取最高版本，旧版本副本被屏蔽。
  5. 可见性水位：版本号大于水位的向量与 tombstone 对本次快照不可见。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class BuildState(Enum):
    BUILDING = "BUILDING"    # 构建中，不可见
    READY = "READY"          # 构建完成，可被发布
    FAILED = "FAILED"        # 构建失败，永不可见
    RETIRED = "RETIRED"      # 已下线，不可见


@dataclass
class Segment:
    segment_id: str
    generation: int
    vectors: Dict[str, int] = field(default_factory=dict)      # vector_id -> version
    tombstones: Dict[str, int] = field(default_factory=dict)   # vector_id -> tombstone version
    state: BuildState = BuildState.BUILDING
    published: bool = False

    def is_visible(self) -> bool:
        return self.state == BuildState.READY and self.published


@dataclass
class MaskedVector:
    vector_id: str
    reason: str          # "tombstone" | "stale_version"
    detail: str


@dataclass
class QueryResult:
    watermark: int
    visible_segments: List[str]
    visible_vectors: Dict[str, int]          # vector_id -> 生效版本
    masked_vectors: List[MaskedVector]
    double_read_segments: List[Tuple[str, str]]
    double_read_ids: List[str]               # 需要双读合并的向量范围


class IndexManager:
    def __init__(self) -> None:
        self.segments = []  # type: List[Segment]
        self.committed_watermark = 0

    # ---- 写入路径 ----

    def add_segment(self, segment: Segment) -> None:
        self.segments.append(segment)

    def get_segment(self, segment_id: str) -> Segment:
        for seg in self.segments:
            if seg.segment_id == segment_id:
                return seg
        raise KeyError("unknown segment: %s" % segment_id)

    def insert(self, segment_id: str, vector_id: str, version: int) -> None:
        seg = self.get_segment(segment_id)
        if seg.state in (BuildState.FAILED, BuildState.RETIRED):
            raise ValueError("cannot insert into %s segment %s" % (seg.state.value, segment_id))
        seg.vectors[vector_id] = version
        seg.tombstones.pop(vector_id, None)
        self.committed_watermark = max(self.committed_watermark, version)

    def delete(self, segment_id: str, vector_id: str, tombstone_version: int) -> None:
        seg = self.get_segment(segment_id)
        if seg.state in (BuildState.FAILED, BuildState.RETIRED):
            raise ValueError("cannot delete in %s segment %s" % (seg.state.value, segment_id))
        seg.tombstones[vector_id] = tombstone_version
        self.committed_watermark = max(self.committed_watermark, tombstone_version)

    def finish_build(self, segment_id: str, success: bool = True) -> None:
        seg = self.get_segment(segment_id)
        seg.state = BuildState.READY if success else BuildState.FAILED

    # ---- 切换（swap）----

    def begin_swap(self, new_segment_id: str) -> None:
        """发布新 segment；旧 segment 保持可见，进入双读窗口。"""
        new_seg = self.get_segment(new_segment_id)
        if new_seg.state != BuildState.READY:
            raise ValueError("segment %s is not READY (state=%s)" % (new_segment_id, new_seg.state.value))
        new_seg.published = True

    def finish_swap(self, new_segment_id: str) -> None:
        """结束双读窗口：把旧 segment 的 tombstone 合并进新 segment 后再下线旧段，
        保证只存在旧索引一边的删除标记不会因切换而丢失。"""
        new_seg = self.get_segment(new_segment_id)
        if not new_seg.is_visible():
            raise ValueError("segment %s is not published" % new_segment_id)
        for seg in self.segments:
            if seg is new_seg or not seg.is_visible():
                continue
            if seg.generation >= new_seg.generation:
                continue
            for vid, tomb_ver in seg.tombstones.items():
                existing = new_seg.tombstones.get(vid, 0)
                new_seg.tombstones[vid] = max(tomb_ver, existing)
            seg.state = BuildState.RETIRED
            seg.published = False

    # ---- 查询路径 ----

    def query(self, watermark: Optional[int] = None) -> QueryResult:
        """基于快照水位计算可见集合。watermark 缺省取当前已提交水位。"""
        wm = self.committed_watermark if watermark is None else watermark
        visible = [s for s in self.segments if s.is_visible()]

        candidates = {}   # type: Dict[str, List[Tuple[Segment, int]]]
        tombstones = {}   # type: Dict[str, int]
        for seg in visible:
            for vid, ver in seg.vectors.items():
                if ver <= wm:
                    candidates.setdefault(vid, []).append((seg, ver))
            for vid, tomb_ver in seg.tombstones.items():
                if tomb_ver <= wm:
                    tombstones[vid] = max(tombstones.get(vid, 0), tomb_ver)

        visible_vectors = {}   # type: Dict[str, int]
        masked = []            # type: List[MaskedVector]
        for vid in sorted(candidates):
            copies = candidates[vid]
            best = max(ver for _, ver in copies)
            tomb_ver = tombstones.get(vid)
            if tomb_ver is not None and tomb_ver >= best:
                masked.append(MaskedVector(
                    vector_id=vid,
                    reason="tombstone",
                    detail="tombstone v%d >= best visible v%d" % (tomb_ver, best),
                ))
                continue
            visible_vectors[vid] = best
            stale = [(seg.segment_id, ver) for seg, ver in copies if ver < best]
            for seg_id, ver in stale:
                masked.append(MaskedVector(
                    vector_id=vid,
                    reason="stale_version",
                    detail="segment %s holds v%d, superseded by v%d (rollback blocked)"
                           % (seg_id, ver, best),
                ))

        # 双读范围：出现在多个可见 segment 中，或 tombstone 只落在部分 segment 上
        double_read_ids = set()
        for vid, copies in candidates.items():
            seg_ids = set(seg.segment_id for seg, _ in copies)
            if len(seg_ids) > 1:
                double_read_ids.add(vid)
        for vid in tombstones:
            holders = set(s.segment_id for s in visible if vid in s.tombstones)
            missing = [s.segment_id for s in visible
                       if vid in s.vectors and s.segment_id not in holders]
            if missing:
                double_read_ids.add(vid)

        double_read_segments = []  # type: List[Tuple[str, str]]
        for i in range(len(visible)):
            for j in range(i + 1, len(visible)):
                double_read_segments.append((visible[i].segment_id, visible[j].segment_id))

        return QueryResult(
            watermark=wm,
            visible_segments=[s.segment_id for s in visible],
            visible_vectors=visible_vectors,
            masked_vectors=masked,
            double_read_segments=double_read_segments,
            double_read_ids=sorted(double_read_ids),
        )
