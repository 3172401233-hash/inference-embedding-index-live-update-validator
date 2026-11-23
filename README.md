# inference-embedding-index-live-update-validator

验证在线向量索引更新的可见性和召回边界。

## 场景

在线向量索引同时进行插入、删除和查询：旧 segment 仍对外可见，新 segment
尚未完成构建，删除标记（tombstone）又可能只存在于新旧索引的一边。
本项目用纯内存模型复现该场景，基于可见性水位（watermark）的查询快照
计算每次查询的可见集合，并输出：

- **可见 segment**：当前快照对哪些 segment 生效；
- **被屏蔽向量**：被 tombstone 删除、或因版本回退保护被屏蔽的向量及原因；
- **双读范围**：切换窗口内必须同时读取新旧 segment 并合并的向量。

## 可见性规则

1. 新 segment 构建未完成（`BUILDING` / `FAILED`）不可见；
2. `begin_swap` 后新旧 segment 同时可见，重叠向量必须双读合并；
3. tombstone 在任意一个可见 segment 中存在即生效，删除不因切换暂时失效；
   `finish_swap` 会先把旧段 tombstone 合并进新段再下线旧段；
4. 版本回退保护：同一向量取最高版本，陈旧副本被屏蔽（`stale_version`）；
5. 可见性水位：版本号大于快照水位的向量与 tombstone 对本次查询不可见。

## 运行

```bash
python -m unittest
```

代码仅使用 Python 标准库，语法兼容 Python 3.8.2（已用 3.8 文法校验），
在更高版本解释器上同样可直接运行。

## 说明

**本项目不调用任何真实向量数据库。** `live_index.py` 是纯内存的可见性
语义模型，用字典模拟 segment 的向量与 tombstone 集合，用于验证在线
更新、删除和切换过程中的可见性与召回边界规则，不涉及真实索引构建、
ANN 检索或存储引擎。

## 文件

- `live_index.py` — segment / tombstone / 水位 / 双读的可见性模型
- `test_live_index.py` — 覆盖在线更新、tombstone、构建失败、版本回退、
  水位快照的单元测试
