// 结果导出 Excel——桌面用户要的是“保存到我指定的文件夹”，而非浏览器下载。
const { ipcMain, dialog, BrowserWindow } = require("electron");
const ExcelJS = require("exceljs");

function registerExcelIpc() {
  // data: { sheetName?, columns?: [{header, key, width}], rows: [obj], defaultName? }
  ipcMain.handle("excel:save", async (_evt, data = {}) => {
    const win = BrowserWindow.getFocusedWindow();
    const { canceled, filePath } = await dialog.showSaveDialog(win, {
      defaultPath: data.defaultName || "组价结果.xlsx",
      filters: [{ name: "Excel", extensions: ["xlsx"] }],
    });
    if (canceled || !filePath) return { canceled: true };

    const wb = new ExcelJS.Workbook();
    const ws = wb.addWorksheet(data.sheetName || "Sheet1");
    if (Array.isArray(data.columns) && data.columns.length) {
      ws.columns = data.columns;
    } else if (Array.isArray(data.rows) && data.rows.length) {
      ws.columns = Object.keys(data.rows[0]).map((k) => ({ header: k, key: k }));
    }
    (data.rows || []).forEach((r) => ws.addRow(r));

    await wb.xlsx.writeFile(filePath);
    return { canceled: false, path: filePath };
  });
}

module.exports = { registerExcelIpc };
