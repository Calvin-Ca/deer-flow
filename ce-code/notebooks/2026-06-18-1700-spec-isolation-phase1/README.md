# 迁移 2026-06-18-1700 · 国标版本严格隔离 Phase 1（机制 + PG 迁移 + 验证）

> 状态：✅ 端到端验证通过。类型：架构/迁移（非检索实验，无召回指标）。承接：[[experiments.md E8]]/[[E9]]。
> 需求：用户调用造价知识库须显式指定所用国标版本（2013/2024），按版本严格隔离路由，避免同 9 位码跨版本串库。

## 1. 背景

2013 与 2024 两套清单计量国标**同 9 位码不同义**（E6–E9 实证：011702006、010401002 等）。混用会串库。
本次建版本隔离机制并迁移 PG。

## 2. 改动（代码）

- `config.SPEC_REGISTRY` + `resolve_spec(spec)`：2013/2024 → bill_collection / bill_doc_ids / bill_spec_versions
  / supports_compose；**spec 必填无默认**，缺省/未知抛 ValueError。
- `schema.sql`：`bill_spec` 主键 `code` → 复合 `(code, spec_version)`。
- `load_pg`：ON CONFLICT `(code, spec_version)`；`EVAL_ONLY_DOCS`→`EXCLUDED_DOCS`（仅排退役错源 GB-50500-2013）。
- `cost.query.compose_price`：加 `spec_versions` 过滤 bill_spec。
- `service.cost_api`：`/bill/match`（请求体 spec）、`/price/compose`（query spec）必填路由；未知→400、组价未就绪→501。
- `cost.bill_index`：加 `--spec`（registry 驱动 collection+doc_ids，防重建漏写 --doc-id 混版本）。
- `ce-services/common/cost_client`：bill_match/price_compose 加 spec 必填透传。

## 3. 服务器迁移步骤（已执行）

```bash
git pull
docker exec ce-postgres psql -U cost -d ce_cost -c "DROP TABLE IF EXISTS bill_spec CASCADE"
CE_PG_DSN=... uv run python -m cost.load_pg --init-schema --scan-dir data/structured   # 复合主键重建 + 三版灌入
uv run python -m cost.bill_index --doc-id GB-50854 --doc-id GB-50856 --collection cost_bill_spec_kb  # 2024 库重建（补 cast_type 字段）
pkill -f service.knowledge_api && setsid uv run python -m service.knowledge_api > /tmp/knowledge.log 2>&1 < /dev/null &  # 重启服务加载新代码
```

> 坑：① 服务是常驻进程，git pull 不热加载 → 必须重启（否则跑旧路由/旧 compose，曾返回错版本行）。
> ② 2024 collection 旧 schema 无 cast_type 字段 → search_bill 报 `field cast_type not exist`，须重建（带 cast_type）。
> ③ 重建 2024 collection **必须显式 --doc-id（或用新 --spec）**，否则把 PG 里的 2013 也灌进 2024 库破坏隔离。

## 4. 验证结果（端到端通过）

PG 三版共存（复合主键）：
```
 GB/T 50854-2013 | 561
 GB/T 50854-2024 | 472
 GB/T 50856-2024 | 1183
```

端点冒烟（重启 + 2024 重建后）：

| 测试 | 结果 |
|---|---|
| `/bill/match {spec:2013}` | ✅ 走 2013 库（spec_version GB/T 50854-2013，带 cast_type） |
| `/bill/match {spec:2024}` | ✅ 走 2024 库（spec_version GB/T 50854-2024，带 cast_type） |
| `/bill/match` 不传 spec | ✅ 422 Field required |
| `/price/compose?spec=2024` 010401002 | ✅ 实心砖墙（2024）+ 6 定额——**跨版本串库已修**（旧代码误返 2013 砖砌挖孔桩护壁） |
| `/price/compose?spec=2013` | ✅ 501「组价数据未就绪」 |

## 5. 结论与下一步

- **结论 ✅**：版本严格隔离 Phase 1（机制 + 关系库 + 向量库 + API + 任务层客户端）端到端打通。2024 组价零回归。
- **下一步**：① Phase 2 数据——2013 全功能组价需收「真实项目实际采用的定额版本 + 价格时点」+ 建 2013 清单→定额映射
  （`bill_quota_map` 须加版本维度，见 BACKLOG）；② 召回提升（模板索引增强 / sparse，见 BACKLOG，与隔离正交）。
