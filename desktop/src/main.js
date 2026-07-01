// Electron 主进程：起 Next 子进程 → 建窗加载 → 注册 IPC → 生命周期 / 安全基线。
const { app, BrowserWindow, ipcMain, shell } = require("electron");
const path = require("node:path");
const { startNextServer } = require("./next-server");
const { buildMenu } = require("./menu");
const { registerFileIpc } = require("./ipc/files");
const { registerExcelIpc } = require("./ipc/excel");
const { getGatewayURL, setGatewayURL } = require("./config");

let mainWindow = null;
let nextChild = null;

function registerConfigIpc() {
  ipcMain.handle("config:getGatewayURL", () => getGatewayURL());
  ipcMain.handle("config:setGatewayURL", (_evt, url) => setGatewayURL(url));
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true, // 安全基线：渲染层拿不到 Node
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // 外链走系统浏览器，不在应用窗口内打开
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  // 拦截导航到外部源（只允许留在本地 Next 内）
  mainWindow.webContents.on("will-navigate", (evt, url) => {
    const target = new URL(url);
    if (target.host !== new URL(mainWindow.__appURL).host) {
      evt.preventDefault();
      shell.openExternal(url);
    }
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());

  // 开发态：直连 next dev（热更新）；打包态：起 standalone 子进程。
  const devURL = process.env.DEERFLOW_DESKTOP_DEV_URL;
  let appURL;
  if (devURL) {
    appURL = devURL;
  } else {
    const started = await startNextServer();
    appURL = started.url;
    nextChild = started.child;
  }
  mainWindow.__appURL = appURL;
  await mainWindow.loadURL(appURL);
}

app.whenReady().then(() => {
  buildMenu();
  registerConfigIpc();
  registerFileIpc();
  registerExcelIpc();
  createWindow();

  app.on("activate", () => {
    // Mac 习惯：Dock 点击且无窗口时重建
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

// 关窗行为：Mac 保留进程在 Dock，Windows/Linux 关窗即退
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

// 退出时清掉 Next 子进程
app.on("before-quit", () => {
  if (nextChild && !nextChild.killed) nextChild.kill();
});
