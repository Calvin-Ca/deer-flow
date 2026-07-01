// preload：contextBridge 白名单，只暴露少数受控方法给渲染层。
// 渲染层（现有 frontend）可渐进调用 window.deerflowDesktop.*；不调用也完全不影响 web 逻辑。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("deerflowDesktop", {
  // 本地文件
  openFiles: (opts) => ipcRenderer.invoke("dialog:openFiles", opts),
  readFile: (filePath) => ipcRenderer.invoke("file:read", filePath),
  // 导出
  saveExcel: (data) => ipcRenderer.invoke("excel:save", data),
  // 远程网关地址（免配置分发的“高级设置”用）
  getGatewayURL: () => ipcRenderer.invoke("config:getGatewayURL"),
  setGatewayURL: (url) => ipcRenderer.invoke("config:setGatewayURL", url),
  // 标识：渲染层可据此判断“运行在桌面壳里”，切换本地文件 UI
  isDesktop: true,
  platform: process.platform,
});
