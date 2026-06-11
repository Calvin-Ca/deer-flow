# ce-bim（BIM 底座层）· 进度 TODO

> BIM 底座层的执行进度。需求/设计见同目录 `PRD.md`；依赖服务/环境见 `DEV.md`；操作命令见 `README.md`。
> 落地原则：**边界现在划，能力按第一个消费方（CostAgent）需要长**（YAGNI，见 `PRD.md §7`）。

---

## Phase 0：立项与边界（✅ 本次）

- [x] 确立 BIM 底座层定位：项目 BIM 模型单一 owner + BIM 原语服务，与 `ce-code` 平级（横切共享资产，非 CostAgent 私有输入）
- [x] 三条核心设计判断：GlobalId 连接键 / 几何只读量确定性 / 渲染做成共享前端包 `ce-bim-viewer`
- [x] 写入文档：`ce-bim/{PRD,DEV,TODO,README}.md`；根 `CLAUDE.md` 目录表；`cost_agent_tech.md`（ce-cost 降为消费方）
- [ ] 新建独立 uv 项目骨架（`pyproject.toml`，端口 :8102）

## Phase 1：底座原语（CostAgent 所需，先做）（⬜ 待办）

> 只实现第一个消费方需要的原语：按 GlobalId 取量/属性/空间结构。

- [ ] `store/`：IFC 原件存 MinIO；`POST /model/ingest`（上传/登记 → 解析建索引，返回 model_id）
- [ ] `parse/`：IfcOpenShell 提取构件 + **基础几何量** + 属性 + 空间结构，**每项带 GlobalId**
- [ ] `GET /model/{id}`：回 IFC 原件（前端 web-ifc 渲染拉取，与取量同源）
- [ ] `GET /model/{id}/elements`（按 type/storey 过滤）、`GET /element/{guid}`、`POST /quantity`（按 GlobalId 批量取量）、`GET /spatial`（空间结构树）
- [ ] `GET /health`（含 store / parser 依赖地址）
- [ ] 解析索引：P0 落 JSON/对象存储，P1 再入 PG 供过滤查询

## Phase 2：共享前端 viewer 包 `ce-bim-viewer`（⬜ 待办）

- [ ] web-ifc / `@thatopen/components` + Three.js + Vue3 组件封装
- [ ] 交互：拾取/多选、隔离/隐藏、剖切、测量、属性面板、空间树导航
- [ ] **按属性着色**（低置信标红 / 未处理灰显 / 异常红色 / 按类型分色）
- [ ] 以 GlobalId 为键的双栏联动接口（与消费方业务表对接）
- [ ] 首个接入：`ce-cost` 造价复核台 import 本包

## Phase 3：第二消费方——模型驱动规范合规审查（⬜ 待办）

> 把 BIM 与现有规范轨焊起来（见 `PRD.md §5`），验证底座的"横切复用"价值。

- [ ] 审图轨取 BIM 构件属性（按 GlobalId）→ 与 `ce-code` 防火强条 `applicable_scope` 谓词比对
- [ ] 校验结果按 GlobalId 回 viewer 红色定位

## 未来（P2+，按需）

- [ ] 大模型性能瓶颈 → 切 xeokit + 后端 XKT 转换（转换保 GlobalId）
- [ ] 4D 进度 / 碰撞检测 / FM 资产空间管理 等更多消费方

---

**依赖与边界提醒**：底座自身不依赖 :8100/:8101，可独立起；不做扣减/清单/组价（→ `ce-cost`）、不做规范校验/碰撞/4D（→ 消费方）。
