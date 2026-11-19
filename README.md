# Invoice Workbench

Windows 发票工作台。集中管理发票原件，自动核验票面信息，按目标金额选票并导出报销材料。

## 特性

- 支持扫描本地资料库和通过 IMAP 按需收票。可导入 PDF、XML、PNG、JPG、OFD，以及 EML 中的发票附件。
- 在本机识别图片、扫描 PDF 和 OFD。同票原件自动归并，核验异常可在导入记录中查看。
- 发现内容相同的源文件时列出待整理的副本，确认后才删除。
- 按目标金额组合选票，支持必选、排除和手动调整。批次导出为包含发票原件和一份 CSV 明细的 ZIP。

## 快速开始

便携版解压后运行 `InvoiceWorkbench.exe`。从源码运行需要 Windows x64 和 Python 3.11 或以上版本，Python 须包含 Tcl/Tk 和 pip。首次安装需要网络。

```powershell
.\setup.ps1
.\start.ps1
```

1. 在「设置 → 发票资料库」选择存放原件的文件夹。也可以在首次本地导入时选择。
2. 在「发票资料 → 导入发票」扫描资料库或从邮箱收票，在「导入记录」查看结果。
3. 在「设置 → 报销公司」填写购买方抬头或税号。核验通过且匹配公司的发票可按额度选用。
4. 在「报销批次」导出材料，在发票详情更新单张发票的报销状态。

## 配置

### 邮箱收票

1. 在邮箱网页端启用 IMAP，在「设置 → IMAP 收票」填写并保存邮箱地址。常见 Outlook、QQ 和网易邮箱可自动匹配服务器，其他邮箱填写服务商提供的 SSL/TLS 地址和端口。
2. 点击「导入发票 → 从邮箱收票」。Outlook 在浏览器授权，其他邮箱使用客户端授权码或应用专用密码。连接后选择文件夹和日期。
3. 可在「IMAP 收票」设置禁止下载的域名。登录凭证仅在本次运行期间保留。

从源码连接 Outlook 时，可在设置中填写 Microsoft Entra 应用的客户端 ID。注册时选择「移动和桌面应用程序」，使用 `http://localhost` 回调地址，并授予 Exchange Online 委托 IMAP 权限。连接个人邮箱时，应用注册须支持个人 Microsoft 帐户。便携版由发布者提供客户端 ID。[Microsoft 的应用注册说明](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app) · [IMAP OAuth 说明](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth)

### 本地数据

资料库用于保存和扫描发票原件。应用目录下的 `data/` 默认保存设置、发票索引和历史记录。在「设置 → 系统数据库」恢复初始化会清空索引、报销批次、导入历史和设置，保留资料库路径、原件和已导出的文件。

## 仓库结构

- [invoice_workbench/](invoice_workbench/) 保存应用源码。
- [tools/](tools/) 提供验证和便携包构建工具。
- [tests/](tests/) 和 [samples/](samples/) 分别保存自动化测试和样例。
- [app.py](app.py) 是程序入口，[setup.ps1](setup.ps1) 和 [start.ps1](start.ps1) 分别安装和启动应用。

## 命令参考

```powershell
# 检查启动环境
.\start.ps1 -CheckEnvironment

# 运行自动化测试
.\.venv\Scripts\python.exe -m unittest discover -v

# 验证指定目录的导入与导出流程，保留源文件
.\.venv\Scripts\python.exe tools\validate_import.py --source "<发票目录>"

# 完成源码安装后构建便携包，输出到 dist/
.\.venv\Scripts\python.exe tools\build_portable.py --outlook-client-id "<应用程序客户端 ID>"
```

## 故障排查

- 无法启动时运行 `.\start.ps1 -CheckEnvironment`，并查看应用目录中的 `startup-error.log`。安装或启动脚本可通过 `-Python` 指定解释器。
- 提示数据库被外部修改或存在遗留会话时，核对同步状态，关闭其他正在使用该数据库的窗口，再点击「重新连接」。
- 邮件附件或直链导入失败时，在「导入记录」查看原因，按邮件主题查找原件。
