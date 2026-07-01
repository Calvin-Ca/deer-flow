// 本地文件直连——桌面核心价值：原生选文件框读 PDF/图纸，绕开浏览器上传限制。
const { ipcMain, dialog, BrowserWindow } = require("electron");
const fs = require("node:fs/promises");
const path = require("node:path");

function registerFileIpc() {
  // 打开选文件框，返回选中路径 + 基本信息（不读内容，避免大文件占内存）。
  ipcMain.handle("dialog:openFiles", async (_evt, opts = {}) => {
    const win = BrowserWindow.getFocusedWindow();
    const { canceled, filePaths } = await dialog.showOpenDialog(win, {
      properties: opts.multi ? ["openFile", "multiSelections"] : ["openFile"],
      filters: opts.filters || [
        { name: "图纸/文档", extensions: ["pdf", "png", "jpg", "jpeg", "dwg", "xlsx"] },
        { name: "全部文件", extensions: ["*"] },
      ],
    });
    if (canceled) return { canceled: true, files: [] };
    const files = await Promise.all(
      filePaths.map(async (p) => {
        const st = await fs.stat(p);
        return { path: p, name: path.basename(p), size: st.size };
      }),
    );
    return { canceled: false, files };
  });

  // 按路径读文件内容（base64），供渲染层拿到后走 multipart 传远程 MinerU 等。
  ipcMain.handle("file:read", async (_evt, filePath) => {
    const buf = await fs.readFile(filePath);
    return { name: path.basename(filePath), base64: buf.toString("base64") };
  });
}

module.exports = { registerFileIpc };
