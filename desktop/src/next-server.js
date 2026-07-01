// 启动 frontend 的 Next standalone 子进程，并把 API 代理指向远程网关。
// 采用 proxy 方案：渲染层只跟本地 Next 同源，rewrites 自动转发到远程 → 零 CORS、SSR/鉴权原样保留。
const { app } = require("electron");
const { fork } = require("node:child_process");
const net = require("node:net");
const path = require("node:path");
const { getGatewayURL } = require("./config");

// 取一个空闲端口，避免和用户机上其它服务撞。
function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

// standalone server.js 的位置：打包后在 resources/standalone，开发未打包时用相对路径吃 frontend 产物。
function resolveServerEntry() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "standalone", "server.js");
  }
  return path.join(__dirname, "..", "..", "frontend", ".next", "standalone", "server.js");
}

// 轮询直到 Next 起好并能响应。
function waitForReady(url, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const start = process.hrtime.bigint();
    const tick = () => {
      const req = net.connect({ host: "127.0.0.1", port: new URL(url).port }, () => {
        req.destroy();
        resolve();
      });
      req.on("error", () => {
        req.destroy();
        const elapsedMs = Number((process.hrtime.bigint() - start) / 1000000n);
        if (elapsedMs > timeoutMs) return reject(new Error("Next 子进程启动超时"));
        setTimeout(tick, 300);
      });
    };
    tick();
  });
}

// 启动子进程；返回 { url, child }。
async function startNextServer() {
  const port = await getFreePort();
  const url = `http://127.0.0.1:${port}`;
  const entry = resolveServerEntry();

  const child = fork(entry, [], {
    cwd: path.dirname(entry),
    env: {
      ...process.env,
      NODE_ENV: "production",
      HOSTNAME: "127.0.0.1",
      PORT: String(port),
      // 关键：rewrites 分支据此把 /api/* 转发到远程网关（server-only，渲染层无感）。
      DEER_FLOW_INTERNAL_GATEWAY_BASE_URL: getGatewayURL(),
      // 桌面运行在 http://127.0.0.1，跳过可能因 Secure cookie 失效的严格校验（后续按需收紧）。
      SKIP_ENV_VALIDATION: "1",
    },
    stdio: ["ignore", "inherit", "inherit", "ipc"],
  });

  await waitForReady(url);
  return { url, child };
}

module.exports = { startNextServer };
