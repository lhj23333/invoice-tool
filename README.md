# Invoice Workbench - Windows 发票管理工具

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/Platform-Windows-green.svg" alt="Platform">
  <img src="https://img.shields.io/badge/GUI-Tkinter-orange.svg" alt="GUI">
</p>

**Invoice Workbench** 是基于 **Python + Tkinter** 的本地发票管理工具，支持发票收集、信息核对、组合选票和报销批次导出。

---

## ✨ 特性

- **📥 文件导入**：支持 PDF、XML、图片、OFD 及 EML 邮件附件。
- **🔍 核对预览**：提取票面信息，支持 PDF / 图片预览和人工核对。
- **🧮 组合选票**：按目标金额匹配发票，支持必选、排除和手动调整。
- **📦 批次管理**：整票占用、状态跟踪，导出原件和报销清单。
- **💾 本地管理**：保留源文件，工作区可通过网盘在多台电脑间轮流使用。

---

## ⚡ 快速开始

### 安装与启动

需要 **Windows Python 3.12（含 Tcl/Tk）**，在项目根目录执行：

```powershell
$python = py -3.12 -c "import sys; print(sys.executable)"
.\setup.ps1 -Python $python
.\start.ps1
```

已有便携包时，解压后双击 `InvoiceWorkbench.exe` 即可，无需另外安装 Python。

### 基本使用

设置公司信息 → 导入发票文件夹 → 核对票面信息 → 组合选票 → 确认批次并导出。

左侧栏提供 **发票资料 / 报销批次 / 设置** 三个独立页面。

---

## 📁 项目结构

```text
InvoiceWorkbench/
├── invoice_workbench/       # 应用源码
│   ├── gui.py              # 主窗口与发票管理
│   ├── selection.py        # 组合选票窗口
│   ├── domain.py           # 校验与选票算法
│   ├── extract.py          # 票面信息提取
│   ├── store.py            # 数据存储与批次导出
│   ├── session.py          # 会话与写入锁
│   ├── workflow.py         # 文件导入流程
│   └── labels.py           # 共用界面标签
├── tools/                  # 打包和开发辅助工具
├── tests/                  # 自动化测试
├── samples/                # 合成发票样例
├── app.py                  # 程序入口
├── requirements.txt        # 固定版本依赖
├── setup.ps1               # 环境安装
└── start.ps1               # 启动脚本
```

---

## 🛠️ 测试与构建

```powershell
# 运行测试
.\.venv\Scripts\python.exe -m unittest discover -v

# 构建 Windows 便携包
.\.venv\Scripts\python.exe tools\build_portable.py
```

构建输出位于 `dist/`。已有便携运行时的开发目录也可使用 `.\runtime\python.exe` 执行上述命令。

---

## 📌 使用说明

- 发票经人工核对后才能选用；整票归属一个未取消的批次，超额不结转。
- 多电脑轮流使用时，同步整个工作区，切换前退出程序并等待同步完成。
- 当前专注本地文件管理，暂不包含 OCR 和 Outlook 自动收取。
