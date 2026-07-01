# deer-flow 桌面壳（Electron）

路 A 独立项目：不接 monorepo，用相对路径消费 `../frontend` 的 standalone 产物。桌面壳只做「壳 + 本地能力」，业务功能全部由 frontend 透传。

## 架构

```
Electron main (Node)
  ├─ fork  frontend/.next/standalone/server.js  → 127.0.0.1:<空闲端口>
  │        （proxy 方案：rewrites 把 /api/* 转发到远程网关，渲染层同源、零 CORS）
  ├─ BrowserWindow  加载 http://127.0.0.1:<port>
  ├─ IPC：本地文件导入 / Excel 导出 / 网关地址读写
  └─ 安全基线：contextIsolation + sandbox + 外链走系统浏览器
        │  HTTP 直连
        ▼
  远程 GPU 服务器 gateway:8001（Qwen3-8B / Milvus / PG / MinerU）
```

## 功能

| 类别 | 功能 | 状态 |
|---|---|---|
| 继承 web | deer-flow 全部页面/对话/agent 编排/组价问答 | 透传，白拿 |
| 桌面独有 | 独立窗口 + 跨平台原生菜单 + 关窗生命周期 | ✅ |
| 桌面独有 | 连远程后端（默认地址内置 + userData 覆盖） | ✅ |
| 桌面独有 | 本地文件导入（原生选文件框读 PDF/图纸） | ✅ IPC 打通 |
| 桌面独有 | 结果导出 Excel（原生保存框落盘） | ✅ IPC 打通 |
| 桌面独有 | 外链走系统浏览器 + 安全基线 | ✅ |
| 桌面独有 | 自动更新（electron-updater） | 🔲 留接口 |

渲染层通过 `window.deerflowDesktop.*` 调用本地能力（`openFiles / readFile / saveExcel / getGatewayURL / setGatewayURL / isDesktop / platform`）。frontend 不调用也完全不影响现有 web 逻辑。

## 开发

```
# 终端 A：起 frontend dev（热更新）
cd frontend && pnpm dev
# 终端 B：起 electron 壳，直连 dev server
cd desktop && pnpm install && pnpm dev
```

## 打包

standalone 产物不含 `.next/static` 和 `public`，`electron-builder.yml` 已用三段 extraResources 拼好。出包前先构建 frontend：

```
cd frontend && pnpm build          # 生成 .next/standalone
cd desktop  && pnpm build:win      # 或 build:mac（须在 macOS 上）/ build:linux
```

> Mac 包必须在 macOS 上构建；公网分发 Mac 需 Apple 签名+公证，内部自用可跳过。

## 配置远程地址

默认 `http://172.19.3.136:8001`（见 `src/config.js`）。用户可在 `userData/config.json` 写 `{"gatewayURL": "http://..."}` 覆盖，无需改代码。

## 待验证（骨架未打包票，需实测）

1. `frontend` 加 `output:'standalone'` 后能否顺利 `pnpm build`（依赖 tracing）。
2. `http://127.0.0.1` 下 SSR 鉴权的 Secure cookie 登录态是否正常（最可能踩的坑）。
3. 前端有无硬编码公网域名假设（`assetPrefix` / 绝对 URL 拼接）。
