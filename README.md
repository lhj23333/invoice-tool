# Invoice Workbench - Windows 发票管理工具

![Python](https://img.shields.io/badge/Python-%3E%3D3.11-blue.svg)![Platform](https://img.shields.io/badge/Platform-Windows-green.svg)![GUI](https://img.shields.io/badge/GUI-Tkinter-orange.svg)

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

需要 **Windows Python 3.11 及以上的 Python 3（含 Tcl/Tk 和 pip）**。固定依赖中的 Pillow 13 要求 Python >= 3.11；

```powershell
.\setup.ps1
.\start.ps1
```

脚本自动检查项目运行环境、已激活的 Python 环境、PATH 中的 Python、已注册的 Python 安装和 `py -3`，支持虚拟环境的 `Scripts` 与 `bin` 目录。安装依赖后会检查依赖能否加载；

需要指定解释器时，可使用 `-Python` 传入可执行文件路径或命令名：

```powershell
.\setup.ps1 -Python "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe"
.\start.ps1 -CheckEnvironment
```

`start.ps1` 同样支持 `-Python`。显式指定的解释器不满足要求时会显示错误。

切换 Python 安装时需要重新创建项目的 `.venv`；脚本会在发现已有虚拟环境属于其他 Python 安装时停止，防止混用不同版本的库。

便携包根据构建解释器动态确定 Python DLL、标准库归档及 Tcl/Tk 路径。已有便携包时，解压后双击 `InvoiceWorkbench.exe` 即可，无需另外安装 Python。

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
