// 桌面壳配置：远程后端网关地址。
// 免配置分发——默认地址编译进包；少数情况用户可在 userData/config.json 覆盖，不用改代码/填 IP。
const { app } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

// 内置默认：远程 GPU 服务器网关（CLAUDE.md 内网地址）。分发前按目标环境改这里即可。
const DEFAULT_GATEWAY_URL = "http://172.19.3.136:8001";

function getUserConfigPath() {
  return path.join(app.getPath("userData"), "config.json");
}

// 读取用户覆盖配置（存在则合并到默认之上）。
function readUserConfig() {
  try {
    const raw = fs.readFileSync(getUserConfigPath(), "utf8");
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

function getGatewayURL() {
  const user = readUserConfig();
  const url = (user.gatewayURL || DEFAULT_GATEWAY_URL).trim();
  return url.replace(/\/+$/, "");
}

// 写回用户覆盖（供“高级设置”入口调用；骨架先留 API）。
function setGatewayURL(url) {
  const cfg = readUserConfig();
  cfg.gatewayURL = String(url).trim();
  fs.writeFileSync(getUserConfigPath(), JSON.stringify(cfg, null, 2), "utf8");
  return cfg.gatewayURL;
}

module.exports = { DEFAULT_GATEWAY_URL, getGatewayURL, setGatewayURL, getUserConfigPath };
