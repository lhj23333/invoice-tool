# Invoice Workbench

Windows 发票工作台，用于导入、核验和归档发票，按目标金额选票并导出报销材料。

## 特性

- **导入发票**可扫描文件夹、选择多个文件或按需从邮箱收票。支持 PDF、XML、OFD、PNG、JPEG 和 EML 中的发票附件。
- **核验与归档**使用本机 OCR，自动归并同票原件。核验异常可在「导入记录」查看，源文件副本经确认后才删除。
- **按额度选票**支持必选、排除和手动调整。导出 ZIP 包含发票原件和 CSV 明细。

## 仓库结构

- [invoice_workbench/](invoice_workbench/) 保存应用源码。
- [setup.ps1](setup.ps1) 和 [start.ps1](start.ps1) 用于安装和启动。
- [tools/](tools/) 提供验证与便携包构建工具。
- [tests/](tests/) 和 [samples/](samples/) 保存测试与样例；便携包只分发运行所需的 `samples/`。

## 快速开始

使用**便携版**时，解压后运行 `InvoiceWorkbench.exe`。

运行**源码版**需要 Windows x64 和 Python 3.11 或以上版本（含 Tcl/Tk 和 pip）。首次安装需要联网。

```powershell
.\setup.ps1
.\start.ps1
```

1. 在「发票资料 → 导入发票」导入，在「导入记录」查看结果。
2. 在「设置 → 常规与存储 → 报销公司」填写购买方抬头或税号。核验通过且匹配公司的发票可按额度选用。
3. 在「发票资料」按额度选票，在「报销批次」导出材料。报销状态可在发票详情更新。

## 配置

### 工作区

- 默认使用应用目录下的 `data/`。在「设置 → 常规与存储 → 工作区存储位置」可选择其他目录，包括网盘同步目录。
- 选择新目录会复制当前记录和归档原件，选择已有工作区会打开其中的记录。扫描工作区时会跳过程序管理的目录。
- 「恢复初始化」会清空记录和设置，保留工作区位置、原件及导出文件。

### 邮箱收票

1. 启用邮箱的 IMAP 服务，在「设置 → 邮箱收票」保存邮箱地址。可在「高级服务器设置」配置 IMAP 服务器和下载域名黑名单。
2. 在「导入发票 → 从邮箱收票」连接邮箱，再选择文件夹和日期。Outlook 使用浏览器授权，其他邮箱使用授权码或应用专用密码。
3. 再次收票时复用已完成的结果。勾选「深度扫描无线索的图片和扫描件」可补查之前延后的内容，勾选「重新扫描已处理邮件」可重新读取整个日期范围。
4. 通过原件核验的发票会归档，同票来源自动归并。在「导入记录 → 待审核」查看暂时无法确认的附件、二维码图片和邮件线索，可核验附件、补充原件或排除。

源码版在「高级服务器设置」填写 Outlook OAuth 客户端 ID。应用注册需配置 `http://localhost` 回调和委托 IMAP 权限。便携版由发布者提供客户端 ID。参见 [Microsoft 应用注册](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)与 [IMAP OAuth](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)。

## 命令参考

```powershell
# 检查启动环境
.\start.ps1 -CheckEnvironment

# 运行自动化测试
.\.venv\Scripts\python.exe -m unittest discover -v

# 验证导入和导出，保留源文件
.\.venv\Scripts\python.exe tools\validate_import.py --source "<发票目录>"

# 构建便携包（输出到 dist/）
.\.venv\Scripts\python.exe tools\build_portable.py --outlook-client-id "<应用程序客户端 ID>"
```

## 故障排查

- **无法启动时**，检查启动环境和 `startup-error.log`。安装和启动脚本支持用 `-Python` 指定解释器。
- **数据库未连接时**，确认其他设备已关闭该工作区、索引已同步后，点击「重新连接」。也可打开其他已有工作区。
- **收票失败时**，在「导入记录」查看原因，必要时打开原邮件。
