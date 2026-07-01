# desktop（Electron 桌面壳）进度

路 A 独立项目：不接 monorepo，相对路径消费 `../frontend` 的 standalone 产物。业务功能全部由 frontend 透传，壳只做「壳 + 本地能力」。

## 已完成

- [x] 骨架落地（`cef99f1b`）：main / preload / next-server / menu / config / ipc(files,excel) / electron-builder.yml
- [x] `frontend/next.config.js` 加 `output:'standalone'`（唯一改到的现有文件，对 web 部署无影响）
- [x] electron 二进制下载修复：`onlyBuiltDependencies` + `packageManager` 字段（`cc2f784c`/`e9a2df43`）
- [x] 服务器 `pnpm install` 通过，electron 二进制经 npmmirror 镜像装好
- [x] **风险点1 过关**：`frontend` 加 standalone 后 `pnpm build` 成功，`.next/standalone/server.js` 生成

## 待办（按优先级）

- [ ] **风险点2：起窗 + 登录态**（需显示器 → Mac）。两个终端：`cd frontend && pnpm dev`，`cd desktop && pnpm dev`。
      重点验 `http://127.0.0.1` 下 SSR 鉴权的 Secure cookie 能否写入、登录是否正常。**最可能踩的坑。**
- [ ] （可选，服务器可做，不需显示器）standalone server + 代理烟雾测试：
      手动 `node server.js` 起 standalone（喂 `DEER_FLOW_INTERNAL_GATEWAY_BASE_URL`），curl 验证能出页面 + 代理转发到远程网关。
- [ ] 前端渐进接入本地能力：在合适的上传/导出入口调用 `window.deerflowDesktop.*`
      （`openFiles`/`readFile` 走 MinerU 上传、`saveExcel` 导组价结果）。`isDesktop` 判断切换桌面 UI。不接不影响 web。
- [ ] 打包出安装包：`frontend pnpm build` → `desktop pnpm build:mac`（须在 macOS）/ `build:win`（windows runner）。
      验证 extraResources 三段映射（standalone + .next/static + public）拼出的目录能正常跑。
- [ ] electron-updater 接更新服务器（GitHub releases 或内网静态服务器），骨架已留接口未接。
- [ ] Mac 分发若要免 Gatekeeper 拦截：Apple 开发者账号做签名 + 公证（内部自用可暂缓）。

## 备注

- 开发轨（测窗口/登录）electron 连 `next dev`(:3000)，**不用 standalone**；打包轨才用 standalone。两者产物不同别混。
- 远程网关默认地址 `http://172.19.3.136:8001` 在 `src/config.js`，用户可在 `userData/config.json` 覆盖。
