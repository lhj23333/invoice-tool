from functools import cached_property
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
import re
from threading import Event
from time import monotonic
from urllib.parse import parse_qs
from uuid import UUID
import webbrowser

from msal import PublicClientApplication
from requests import RequestException

from .domain import UserError


OUTLOOK_IMAP_HOST = "outlook.office365.com"
PERSONAL_OUTLOOK_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}
OUTLOOK_IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All"
CLIENT_ID_FILE = "outlook-oauth-client-id.txt"
CLIENT_ID_ENV = "INVOICE_WORKBENCH_OUTLOOK_CLIENT_ID"
AUTHORIZATION_TIMEOUT = 180


def validate_client_id(value):
    try:
        identity = UUID(value.strip())
        if not identity.int:
            raise ValueError
    except ValueError as exc:
        raise UserError("Outlook OAuth 客户端 ID 格式无效。请使用应用注册的「应用程序（客户端）ID」。") from exc
    return str(identity)


def configured_client_id(app_root, *, local_client_id=""):
    value = local_client_id.strip() or os.environ.get(CLIENT_ID_ENV, "").strip()
    if value:
        return validate_client_id(value)
    path = app_root / CLIENT_ID_FILE
    if not path.is_file():
        return ""
    try:
        return validate_client_id(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        raise UserError("无法读取发布包的 Outlook 授权配置。") from exc


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    timeout = 1

    def do_POST(self):
        if self.path != "/" or self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.send_error(400)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), max_num_fields=32)
        except (ValueError, TimeoutError):
            self.send_error(400)
            return
        response = {name: values[0] for name, values in fields.items() if len(values) == 1}
        if response.get("state") != self.server.oauth_state or not (response.get("code") or response.get("error")):
            self.send_error(400)
            return
        self.server.oauth_response = response
        body = ("Microsoft 授权未完成，请返回发票工作台查看提示。" if response.get("error")
                else "已收到 Microsoft 授权结果，请返回发票工作台。").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class OutlookOAuth:
    def __init__(self, address, application_id):
        if not application_id:
            raise UserError("请在「设置 → IMAP 收票」填写并保存 Outlook OAuth Client ID。请使用 Microsoft Entra 应用注册的「应用程序（客户端）ID」。")
        self.address = address
        self.application_id = validate_client_id(application_id)
        self.account = None

    @cached_property
    def app(self):
        tenant = "consumers" if self.address.rpartition("@")[2].lower() in PERSONAL_OUTLOOK_DOMAINS else "common"
        return PublicClientApplication(self.application_id, authority=f"https://login.microsoftonline.com/{tenant}", timeout=20)

    def _authorize(self, scopes, cancelled):
        with HTTPServer(("127.0.0.1", 0), OAuthCallbackHandler) as callback:
            flow = self.app.initiate_auth_code_flow(scopes, redirect_uri=f"http://localhost:{callback.server_port}",
                                                    login_hint=self.address, response_mode="form_post")
            callback.oauth_state = flow["state"]
            callback.oauth_response = None
            if cancelled.is_set():
                raise UserError("已取消 Outlook 邮箱连接。")
            if not webbrowser.open(flow["auth_uri"]):
                raise UserError("无法打开浏览器，请检查 Windows 默认浏览器设置。")
            deadline = monotonic() + AUTHORIZATION_TIMEOUT
            while not cancelled.is_set():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError
                callback.timeout = min(0.1, remaining)
                callback.handle_request()
                if callback.oauth_response is not None:
                    break
        if cancelled.is_set():
            raise UserError("已取消 Outlook 邮箱连接。")
        return self.app.acquire_token_by_auth_code_flow(flow, callback.oauth_response)

    def access_token(self, *, allow_interactive=True, cancelled=None):
        cancelled = cancelled if cancelled is not None else Event()
        if cancelled.is_set():
            raise UserError("已取消 Outlook 邮箱连接。")
        scopes = [OUTLOOK_IMAP_SCOPE]
        try:
            if self.account:
                cached = self.app.acquire_token_silent(scopes, account=self.account)
                if cached and cached.get("access_token"):
                    return cached["access_token"]
            if not allow_interactive:
                raise UserError("Outlook 登录已失效，请重新选择「从邮箱收票」完成授权。")
            result = self._authorize(scopes, cancelled)
        except TimeoutError:
            raise UserError("等待 Microsoft 授权结果超时。请查看浏览器提示，核对应用注册的 http://localhost 重定向 URI 后重试。") from None
        except RequestException:
            raise UserError("无法连接 Microsoft 登录服务，请检查网络后重试。") from None
        except OSError:
            raise UserError("无法启动 Microsoft 浏览器授权，请检查浏览器及本机回调连接。") from None
        if not result.get("access_token"):
            code = re.search(r"\bAADSTS[0-9]+\b", result.get("error_description") or "")
            if result.get("error") == "access_denied":
                raise UserError("Microsoft 授权已取消，请重新开始收票。")
            error = f"（{code.group()}）" if code else ""
            raise UserError(f"Microsoft 授权未完成{error}。请核对浏览器登录的邮箱账号和 Microsoft Entra 应用授权配置。")
        username = (result.get("id_token_claims") or {}).get("preferred_username") or self.address
        accounts = self.app.get_accounts(username=username)
        if not accounts:
            accounts = self.app.get_accounts()
        self.account = accounts[0] if len(accounts) == 1 else None
        return result["access_token"]
