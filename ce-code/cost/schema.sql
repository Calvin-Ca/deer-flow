-- cost/schema.sql —— 结构化造价数据关系库（PostgreSQL 16，库 ce_cost）单一事实源 DDL
--
-- 用途：把服务器上手敲建好的 bill_spec 落成可复现 DDL，并一次建齐组价主体表
--       （quota_item / quota_resource / resource / resource_price / hist_bill）与
--       辅助表 aux_table。全部幂等（IF NOT EXISTS），可重复执行、可纳入 git 审计。
--
-- 治理铁律（TODO Phase C / DEV §3.3）：规范库与定额库为强一致、可审计资产，**所有口径表
--   强制带 doc_id + version + region + effective_priority**；价格走动态管道，带 effective_period
--   时效、不参与口径优先级排序。effective_priority 越小越优先（深圳本地 = 1）。
--
-- 跑法（服务器，单行）：
--   docker exec -i ce-postgres psql -U cost -d ce_cost < cost/schema.sql
-- 或本地 psycopg：load_pg.py --init-schema 会先执行本文件。

-- btree_gist：resource_price 的 EXCLUDE 约束要让等值列参与 gist，须在建表前装好
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. 清单规范库（GB 50500 计价 + GB/T 50854 计量，9 位全国统一编码）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bill_spec (
  code            CHAR(9) NOT NULL,           -- 前 9 位全国统一编码
  name            TEXT NOT NULL,              -- 清单项目名称
  unit            TEXT,                       -- 计量单位（首选；措施项目类可空）
  unit_options    JSONB DEFAULT '[]'::jsonb,  -- 可选计量单位（规范一码配多单位，如刷油 kg/m²）
  calc_rule       TEXT,                       -- 工程量计算规则（GB/T 50854）
  feature_schema  JSONB DEFAULT '[]'::jsonb,  -- 项目特征项模板（已拆 list）
  work_content    JSONB DEFAULT '[]'::jsonb,  -- 工作内容（已拆 list）
  chapter         TEXT,                       -- 所属分部（祖先链根）
  provenance      JSONB,                      -- 溯源 {node_path, caption, page}
  -- 治理字段 ──
  doc_id          TEXT NOT NULL DEFAULT 'GB-50854',  -- 收录文档标识（GB-50854 / GB-50500）
  spec_version    TEXT NOT NULL,              -- 规范版本（canonical，如 GB/T 50854-2024）
  region          TEXT NOT NULL DEFAULT '全国', -- 适用地区（国标 = 全国）
  effective_priority SMALLINT NOT NULL DEFAULT 1,  -- 口径优先级（越小越优先）
  -- 复合主键：同 9 位码跨国标版本（GB/T 50854-2013 vs -2024）须共存隔离，不可只按 code
  -- （2013/2024 同码不同义，单 code 主键会互相覆盖、令取数串版本，见 notebooks E6/E9）。
  PRIMARY KEY (code, spec_version)
);
CREATE INDEX IF NOT EXISTS idx_bill_spec_chapter ON bill_spec (chapter);
CREATE INDEX IF NOT EXISTS idx_bill_spec_doc ON bill_spec (doc_id, spec_version);

-- 辅助/参数表（土石分类表、工作面宽度表…）：列头异构、不归一化，原样留矩形 body 供 calc_rule 查表
CREATE TABLE IF NOT EXISTS aux_table (
  id          BIGSERIAL PRIMARY KEY,
  chapter     TEXT,                           -- 所属分部
  caption     TEXT,                           -- 表标题（如「表 A.4.3-1 土分类表」）
  kind        TEXT,                           -- 粗分类 classification / parameter / unknown
  header      JSONB,                          -- 表头行
  body        JSONB,                          -- 完整矩形二维表
  provenance  JSONB,                          -- 溯源 {node_path, caption, page}
  doc_id      TEXT NOT NULL DEFAULT 'GB-50854',
  spec_version TEXT NOT NULL,
  UNIQUE (doc_id, caption, chapter)           -- 同文档同表唯一（续表 caption 不同 → 各占一行）
);

-- 计价口径：费用构成规则（GB 50500 正文锚定）。2024 版 50500 无清单项目录，故不进
-- bill_spec；本表供组价引擎程序化读「综合单价/工程造价由什么构成」。每行一个构成项。
CREATE TABLE IF NOT EXISTS price_composition (
  id            BIGSERIAL PRIMARY KEY,
  composite     TEXT NOT NULL,                 -- 被构成的费用（综合单价 / 工程造价）
  kind          TEXT NOT NULL,                 -- unit_rate（综合单价层）/ project_cost（工程造价层）
  seq           SMALLINT NOT NULL,             -- 构成项次序
  component     TEXT NOT NULL,                 -- 构成项（人工费 / 材料费 / …）
  note          TEXT,                          -- 口径补充（如 不含增值税）
  provenance    JSONB,                         -- 溯源 {node_path, clause}
  -- 治理字段 ──
  doc_id        TEXT NOT NULL DEFAULT 'GB-50500',
  spec_version  TEXT NOT NULL,
  region        TEXT NOT NULL DEFAULT '全国',
  effective_priority SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (doc_id, composite, seq)             -- 幂等 upsert 键
);
CREATE INDEX IF NOT EXISTS idx_price_composition_composite ON price_composition (composite);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. 定额库（组价主体；MVP 取深圳市消耗量标准 SJG 171/170-2024，region=深圳 priority=1）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS quota_item (
  id            BIGSERIAL PRIMARY KEY,
  quota_code    TEXT NOT NULL,                -- 定额子目编号
  name          TEXT NOT NULL,
  unit          TEXT NOT NULL,
  base_price    NUMERIC,                      -- 基价（取「参考综合单价」，不含税综合单价）
  labor_cost    NUMERIC,                      -- 人工费
  material_cost NUMERIC,                      -- 材料费
  machine_cost  NUMERIC,                      -- 机械费
  work_content  TEXT,                         -- 工作内容（定额表 caption 抽出）
  chapter       TEXT,                         -- 所属分部/章
  provenance    JSONB,
  -- 治理字段 ──
  doc_id        TEXT NOT NULL,                -- SZ-SJG171 / SZ-SJG170
  spec_version  TEXT NOT NULL,                -- 如 SJG 171-2024
  region        TEXT NOT NULL DEFAULT '深圳',
  effective_priority SMALLINT NOT NULL DEFAULT 1,
  UNIQUE (region, quota_code, spec_version)
);
CREATE INDEX IF NOT EXISTS idx_quota_item_chapter ON quota_item (chapter);

-- 资源（人材机）主数据
CREATE TABLE IF NOT EXISTS resource (
  id        BIGSERIAL PRIMARY KEY,
  res_code  TEXT,                             -- 资源编码（如有）
  name      TEXT NOT NULL,
  spec      TEXT,                             -- 规格型号
  category  TEXT NOT NULL,                    -- 人工 / 材料 / 机械
  unit      TEXT NOT NULL,
  doc_id    TEXT,
  -- NULLS NOT DISTINCT（PG15+）：spec 为 NULL 时也算同一行，保证 upsert 幂等
  UNIQUE NULLS NOT DISTINCT (category, name, spec, unit)
);

-- 工料机含量：定额子目 → 资源（多对多 + 含量）
CREATE TABLE IF NOT EXISTS quota_resource (
  id           BIGSERIAL PRIMARY KEY,
  quota_id     BIGINT NOT NULL REFERENCES quota_item(id) ON DELETE CASCADE,
  resource_id  BIGINT NOT NULL REFERENCES resource(id) ON DELETE CASCADE,
  consumption  NUMERIC NOT NULL,             -- 含量
  UNIQUE (quota_id, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_quota_resource_quota ON quota_resource (quota_id);
CREATE INDEX IF NOT EXISTS idx_quota_resource_res ON quota_resource (resource_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. 价格库（动态独立管道：信息价月更，带时效；不参与口径优先级排序）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS resource_price (
  id               BIGSERIAL PRIMARY KEY,
  resource_id      BIGINT NOT NULL REFERENCES resource(id) ON DELETE CASCADE,
  region           TEXT NOT NULL DEFAULT '深圳',
  price            NUMERIC NOT NULL,
  price_type       TEXT NOT NULL DEFAULT '信息价', -- 信息价 / 市场价 / 历史价
  effective_period DATERANGE NOT NULL,            -- 时效区间
  doc_id           TEXT,                          -- SZ-JGXX-PRICE
  -- 同资源同地区同来源时效不重叠（按期取价的前提）
  EXCLUDE USING gist (resource_id WITH =, region WITH =, price_type WITH =, effective_period WITH &&)
);
CREATE INDEX IF NOT EXISTS idx_resource_price_res ON resource_price (resource_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3b. 知识图谱 P0（PG 关联表模拟）：清单 → 定额 APPLIES 边，跑通组价取数路径
--     （构件→清单 MAPS_TO 待 BIM 接入；定额→工料机 CONSUMES 即 quota_resource）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bill_quota_map (
  id           BIGSERIAL PRIMARY KEY,
  bill_code    CHAR(9) NOT NULL,             -- 清单编码（GB 50854，9 位）
  bill_spec_version TEXT NOT NULL,           -- 清单所属国标版本（如 GB/T 50854-2024）——
                                             -- **版本隔离**：同 9 位码跨版本不同义，映射须按版本区分，
                                             -- 否则 2013/2024 同码共用映射、组价串版本（见 notebooks E6/E9 + BACKLOG）
  quota_code   TEXT NOT NULL,                -- 定额子目编号（SJG）
  quota_doc_id TEXT NOT NULL,                -- 定额来源（SZ-SJG171/170），与 quota_code 共同定位
  relation     TEXT NOT NULL DEFAULT 'APPLIES',
  confidence   NUMERIC,                      -- 映射置信度（0~1）
  source       TEXT,                         -- 来源：auto_name_substr / manual / …
  note         TEXT,
  UNIQUE (bill_code, bill_spec_version, quota_code, quota_doc_id)
);
CREATE INDEX IF NOT EXISTS idx_bill_quota_map_bill ON bill_quota_map (bill_code, bill_spec_version);
CREATE INDEX IF NOT EXISTS idx_bill_quota_map_quota ON bill_quota_map (quota_code);

-- 定额资源 → 信息价物料 价格映射（资源同物异名对齐，拉高组价价覆盖）。
-- 规则归一键命中 = 确定匹配（confidence 1.0），amount = 含量 × 单价 × unit_factor。
CREATE TABLE IF NOT EXISTS resource_price_map (
  id                BIGSERIAL PRIMARY KEY,
  quota_resource_id BIGINT NOT NULL REFERENCES resource(id) ON DELETE CASCADE, -- 定额侧资源
  price_resource_id BIGINT NOT NULL REFERENCES resource(id) ON DELETE CASCADE, -- 信息价侧物料
  unit_factor       NUMERIC NOT NULL DEFAULT 1.0,  -- 定额单位/信息价单位 换算（千块↔块=1000）
  confidence        NUMERIC,                       -- 1.0=规则归一精确命中（非猜测）
  method            TEXT,                          -- rule_canon_exact / semantic / manual
  UNIQUE (quota_resource_id, price_resource_id)
);
CREATE INDEX IF NOT EXISTS idx_resource_price_map_q ON resource_price_map (quota_resource_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. 历史工程库（脱敏，供相似案例对标与异常检测；[可缓]）
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hist_bill (
  id           BIGSERIAL PRIMARY KEY,
  project_id   BIGINT,
  bill_code    CHAR(9),
  feature      JSONB,
  quantity     NUMERIC,
  unit_price   NUMERIC,
  project_type TEXT,
  region       TEXT,
  completed_at DATE
);
CREATE INDEX IF NOT EXISTS idx_hist_bill_code ON hist_bill (bill_code);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. 费率库（深圳市建设工程计价费率标准 2023，SZ-FLBZ-2023）
--    安全文明施工/夜间施工/赶工/总承包服务费/增值税/附加税费/工程保险费的参考范围 + 推荐值。
--    费率是「综合单价之上算工程造价」的乘数，与口径表同为静态、带治理字段。
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fee_rate (
  id            BIGSERIAL PRIMARY KEY,
  fee_category  TEXT NOT NULL,                 -- 费用大类（安全文明施工措施费/增值税/工程保险费…）
  fee_name      TEXT NOT NULL,                 -- 具体费用名（同大类或更细）
  applicable    TEXT,                          -- 适用范围（专业工程/工程类别/材料设备/项目名称），可空
  ref_low       NUMERIC,                       -- 参考范围下限
  ref_high      NUMERIC,                       -- 参考范围上限
  recommended   NUMERIC,                       -- 推荐费率/系数/税率
  unit          TEXT NOT NULL DEFAULT '%',     -- % / ‰ / 系数
  provenance    JSONB,                         -- 溯源 {page, caption}
  -- 治理字段 ──
  doc_id        TEXT NOT NULL DEFAULT 'SZ-FLBZ-2023',
  spec_version  TEXT NOT NULL,
  region        TEXT NOT NULL DEFAULT '深圳',
  effective_priority SMALLINT NOT NULL DEFAULT 1,
  -- applicable 为 NULL 也算同一行（PG15+），保证 upsert 幂等
  UNIQUE NULLS NOT DISTINCT (doc_id, fee_category, fee_name, applicable)
);
CREATE INDEX IF NOT EXISTS idx_fee_rate_cat ON fee_rate (fee_category);
