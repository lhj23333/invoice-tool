# Invoice Workbench - Windows 发票管理工具

![Python](https://img.shields.io/badge/Python-%3E%3D3.11-blue.svg)![Platform](https://img.shields.io/badge/Platform-Windows-green.svg)![GUI](https://img.shields.io/badge/GUI-Tkinter-orange.svg)

**Invoice Workbench** 是基于 **Python + Tkinter** 的本地发票管理工具，支持发票导入、自动核验、重复文件整理、组合选票和报销批次导出。

---

## ✨ 特性

- **📥 文件导入**：支持 PDF、XML、图片、OFD 及 EML 邮件附件。
- **🔍 自动核验**：自动比较原件提取结果与记录中的金额、票号、日期、抬头和税号；字段不完整或冲突时停止选用。
- **📂 重复整理**：导入后列出源目录中内容完全相同的发票副本，确认后删除重复副本并至少保留一份；选择“否”继续保留文件。
- **🧮 组合选票**：按目标金额匹配发票，支持必选、排除和手动调整。
- **📦 批次管理**：整票占用，导出平铺的发票文件与一份 CSV 明细。
- **💾 本地管理**：工作区保存原件副本，可通过网盘在多台电脑间轮流使用；源文件仅在确认整理重复副本时删除。

---

## ⚡ 快速开始

### 安装与启动

需要 **Windows Python 3.11 及以上的 Python 3（含 Tcl/Tk 和 pip）**。固定依赖中的 Pillow 13 要求 Python >= 3.11；

```powershell
# 安装依赖
.\setup.ps1

# 启动程序
.\start.ps1
```

需要指定解释器时，可使用 `-Python` 传入可执行文件路径或命令名：

```powershell
# 指定 Python 安装路径
.\setup.ps1 -Python "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe"

# 检查环境
.\start.ps1 -CheckEnvironment
```

### 基本使用

设置公司信息 → 导入并自动核验 → 选择是否清理重复副本 → 组合选票 → 确认批次并导出。

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

# 只读复测指定目录，临时副本和验证工作区自动清理
.\.venv\Scripts\python.exe tools\validate_import.py --source "<本次验证的发票目录>"

# 构建 Windows 便携包
.\.venv\Scripts\python.exe tools\build_portable.py
```

构建输出位于 `dist/`。已有便携运行时的开发目录也可使用 `.\runtime\python.exe` 执行上述命令。

---
