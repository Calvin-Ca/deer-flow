# desktop（Electron 桌面壳）进度

路 A 独立项目：不接 monorepo，相对路径消费 `../frontend` 的 standalone 产物。业务功能全部由 frontend 透传，壳只做「壳 + 本地能力」。

## 已完成

- [x] 骨架落地（`cef99f1b`）：main / preload / next-server / menu / config / ipc(files,excel) / electron-builder.yml
- [x] `frontend/next.config.js` 加 `output:'standalone'`（唯一改到的现有文件，对 web 部署无影响）
- [x] electron 二进制下载修复：`onlyBuiltDependencies` + `packageManager` 字段（`cc2f784c`/`e9a2df43`）
- [x] 服务器 `pnpm install` 通过，electron 二进制经 npmmirror 镜像装好
- [x] **风险点1 过关**：`frontend` 加 standalone 后 `pnpm build` 成功，`.next/standalone/server.js` 生成

## 待办（按优先级）

- [x] **风险点2a：起窗**（Mac，2026-07-02 过关）：Mac 用 corepack 装 pnpm@10.26.2（`corepack enable --install-directory ~/.npm-global/bin pnpm`，非 sudo）。
      两终端：`cd frontend && pnpm dev`（:3000），`cd desktop && pnpm dev`（连 localhost:3000）。窗口正常打开，SSR 重定向链 `/ 307 → /workspace 307 → /login 200` 渲染出登录页。
- [x] **风险点2b：登录态 + Secure cookie**（Mac，2026-07-02 过关）。「最可能踩的坑」实测**不成坑**。
      dev 轨网关代理由 frontend dev 的 `DEER_FLOW_INTERNAL_GATEWAY_BASE_URL` 决定（默认 `127.0.0.1:8001`），desktop `config.js` 网关仅打包/standalone 轨用、dev 轨用不到。
      网关连通方式：**VSCode 远程 SSH 已自动把服务器 8001 转发到 Mac 127.0.0.1:8001**（`localhost:8001/health=200`），frontend dev 默认代理即命中，无需 env override / 额外隧道。
      验证结果：管理员账号登录成功并保持登录态（`/workspace 200`，非弹回 login）；`/api/v1/auth/setup-status → {"needs_setup":false}`（管理员已存在）。
      **根因**：后端 `backend/app/gateway/routers/auth.py:135 _set_session_cookie` 的 `secure=is_secure_request(request)` 跟随请求 scheme——`http://localhost` 下 `is_https=False` → cookie **不带 Secure** → 正常写入。
      ⚠️ **桌面场景 UX 注意**：http 下 `max_age=None` → auth cookie 是**内存态 session cookie**，**关闭 app 即失效**（web 走 https 有 max_age 记住登录）。桌面壳每次重启需重登，功能不阻塞，后续如需「记住登录」再单独处理。
- [x] standalone server + 代理烟雾测试（服务器，2026-07-02 过关）：`frontend pnpm build` → 拷 `.next/static`+`public` 进 standalone → 喂桌面壳那套 env（`SKIP_ENV_VALIDATION=1 NODE_ENV=production HOSTNAME=127.0.0.1 PORT=3100 DEER_FLOW_INTERNAL_GATEWAY_BASE_URL=http://127.0.0.1:8001`）起 `node server.js`。
      结果 `GET / → 307`（SSR 重定向 login）+ `GET /api/v1/auth/setup-status → {"needs_setup":false}`（代理转发到网关成功）。**注意**：此测能通是因为 build 与运行在同一进程、build 时该 env 也在场——恰好掩盖了下面「打包」里发现的 rewrites 冻结问题。
- [ ] 前端渐进接入本地能力：在合适的上传/导出入口调用 `window.deerflowDesktop.*`
      （`openFiles`/`readFile` 走 MinerU 上传、`saveExcel` 导组价结果）。`isDesktop` 判断切换桌面 UI。不接不影响 web。
- [x] 打包出安装包（Mac，2026-07-02 **端到端闭环**：打包 Magent app 内登录成功）：`CSC_IDENTITY_AUTO_DISCOVERY=false pnpm run dist:mac`（免签 ad-hoc）→ 出 `dist/Magent-0.1.0-arm64.dmg`(134M) + `dist/mac-arm64/Magent.app`。
      品牌：`productName: Magent`、`appId: com.caic.magent.desktop`；图标用前端智能体 M 图标（`frontend/src/app/icon.svg` 深色圆角方块+白 serif M），高清版落 `desktop/resources/icon.svg`+`icon.png`(1024)，electron-builder 自动生成 mac `.icns`/win `.ico`。userData 仍 = package.json `name`(`deer-flow-desktop`)，不随 productName 变。
      ✅ 已验：extraResources 三段映射打进 bundle 且**运行时可服务**（打包 app 内置 standalone：`GET / → 307`、`_next/static/chunks/* → 200`）；主进程 + fork 的 standalone 子进程正常起；userData 目录名 = package.json `name`（`deer-flow-desktop`，非 productName `DeerFlow`）。
      ⚠️ **已知限制（不阻断本 interim）**：`next.config.js` 的 `rewrites()` 在 `next build` 时求值、烘进 `.next/routes-manifest.json`，**standalone 运行时不再执行** → `config.js` 的 userData 网关覆盖对 /api 客户端代理**无效**，网关地址在 build 时冻结。**规避**：`dist:*` 脚本用 `build:frontend` 在 build 时烘入默认网关 `172.19.3.136:8001`，端到端验证登录成功（Mac 2026-07-02）。「每台各自配网关」仍需下面的重构。
      ⚠️ 体积虚胖：build 有 NFT「whole project traced」警告，standalone 顶层把整个 frontend（含 CLAUDE.md/Dockerfile/node_modules）都打进去了，dmg 134M，后续该收敛。
      端到端验证（2026-07-02）：服务器网关改绑 `0.0.0.0:8001` 后，Mac 直连 `172.19.3.136:8001` = 200；打包 app 内置 standalone 代理 `/api/v1/auth/setup-status → {"needs_setup":false}`；GUI 登录成功。
- [ ] **[desktop] 网关运行时可配重构**（用户 2026-07-02 定方向，且确认「每台各自配网关」→ **必须项，非可选**；build 时烘死的快路对本场景不成立）：Next rewrites 无法运行时配网关，须把 `/api/*` 代理从 rewrites 挪出。
      方案：electron 主进程在 window 加载的 URL 前置一层轻反代——`/api/*`（含 `/api/langgraph` SSE 流）转发到运行时 `getGatewayURL()`（读 userData/config.json），其余转发到内部 standalone 端口。要点：① SSE/流式不可缓冲，需 pipe（可引 `http-proxy` 依赖或 node http 手写 pipe）；② SSR 服务端 fetch 走 `getInternalServiceURL(env)` 是运行时读 env、本就可配，只客户端 /api 这条要修；③ 修完 rewrites 里 `/api` 分支可留可删（被前置反代拦截先于 Next）。
      追加：因「每台各自配」，需**填网关入口**——现在只能手改 `userData/config.json`，对普通用户不友好；`config.js` 已留 `setGatewayURL` + IPC `config:setGatewayURL`，做个简单设置界面或首启引导。
- [ ] **打 Windows 安装包**：配置已就绪（`build:win` = `electron-builder --win`，`electron-builder.yml` `win: target: nsis`，`oneClick:false`）。
      **打包一律走 `pnpm run dist:win`**（= `build:frontend` 烘默认网关 + `build:win`）；`dist:mac`/`dist:linux` 同理。默认网关 `http://172.19.3.136:8001`（与 `src/config.js` 一致）已在 `build:frontend` 烘进 rewrites，2026-07-02 验过 manifest 命中。
      构建环境：**本机 arm64 Mac 无 wine，不宜交叉打 win**；须在 Windows 机器或 GitHub Actions windows runner 上跑 `pnpm run dist:win`。
      **interim 决定（2026-07-02）**：先填死默认网关（=Mac 默认），内网 win 用户直连这个固定网关即可用；「每台各自配」延后到「网关运行时可配重构」。
      ~~唯一硬前置~~ **已满足（2026-07-02）**：服务器网关已改绑 `0.0.0.0:8001`（Mac 直连 200 实测）。win 用户在内网能连到即可用。剩下只是「在 Windows 机/CI 上跑 `pnpm run dist:win`」的工程活。
- [ ] electron-updater 接更新服务器（GitHub releases 或内网静态服务器），骨架已留接口未接。
- [ ] Mac 分发若要免 Gatekeeper 拦截：Apple 开发者账号做签名 + 公证（内部自用可暂缓）。

## 备注

- 开发轨（测窗口/登录）electron 连 `next dev`(:3000)，**不用 standalone**；打包轨才用 standalone。两者产物不同别混。
- 远程网关默认地址 `http://172.19.3.136:8001` 在 `src/config.js`。⚠️ **目前 `userData/config.json` 覆盖仅对 SSR 服务端 fetch 生效，对 /api 客户端代理无效**（rewrites build 时冻结，见「打包」缺陷）；真运行时可配需完成「网关运行时可配重构」。
- 打包态 userData 路径 = `~/Library/Application Support/deer-flow-desktop/`（取 package.json `name`，非 productName）。
- 本机测打包 app 需网关可达：Mac 直连 `172.19.3.136:8001` 被拒（仅 ping 通），用 SSH 隧道 `ssh -fN -L <本地口>:127.0.0.1:8001 caic@172.19.3.136`；VSCode 远程断开后其自动转发会变僵尸口（accept 但 hang），换新端口起隧道最省事。
