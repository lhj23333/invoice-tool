from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from http.client import HTTPSConnection, HTTPException
from ipaddress import ip_address
from pathlib import Path
import re
from socket import create_connection, getaddrinfo, SOCK_STREAM
from ssl import SSLCertVerificationError, create_default_context
from urllib.parse import quote, unquote, urljoin, urlsplit

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .domain import UserError
from .extract import MAX_BYTES, SUPPORTED
from .outlook_auth import OUTLOOK_IMAP_HOST, PERSONAL_OUTLOOK_DOMAINS


OUTLOOK_HOSTS = {"outlook.office.com", "outlook.office365.com", "outlook.live.com", "outlook.cloud.microsoft"}
IMAP_HOSTS = {"qq.com": "imap.qq.com", "foxmail.com": "imap.qq.com", "163.com": "imap.163.com",
              "126.com": "imap.126.com", "yeah.net": "imap.yeah.net",
              **dict.fromkeys(PERSONAL_OUTLOOK_DOMAINS, OUTLOOK_IMAP_HOST)}
MAX_MESSAGE_BYTES = MAX_BYTES * 4
INVOICE_HINT = re.compile(r"发票|下载|invoice|download", re.I)
MIME_SUFFIX = {"application/pdf": ".pdf", "application/xml": ".xml", "text/xml": ".xml",
               "image/png": ".png", "image/jpeg": ".jpg", "application/ofd": ".ofd"}


def mailbox_profile(address, host="", port="993"):
    address = address.strip()
    local, _, domain = address.rpartition("@")
    if not re.fullmatch(r"[^@\s\x00-\x1f]+", local) or "." not in domain:
        raise UserError("请填写完整的邮箱地址。")
    try:
        domain = domain.encode("idna").decode("ascii").lower()
        host = (host.strip() or IMAP_HOSTS.get(domain, "")).rstrip(".").encode("idna").decode("ascii").lower()
        port = int(port or 993)
    except (UnicodeError, ValueError) as exc:
        raise UserError("邮箱地址、IMAP 服务器或端口格式无效。") from exc
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain):
        raise UserError("邮箱域名格式无效。")
    if (domain in PERSONAL_OUTLOOK_DOMAINS or host in OUTLOOK_HOSTS) and (host, port) != (OUTLOOK_IMAP_HOST, 993):
        raise UserError("Outlook OAuth 收票请使用 outlook.office365.com，SSL/TLS 端口为 993。")
    if not host:
        raise UserError("请填写邮件服务商提供的 IMAP 服务器。")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or not 1 <= port <= 65535:
        raise UserError("IMAP 服务器或端口格式无效。")
    return f"{local}@{domain}", host, port


def domain_list(value):
    domains = set()
    for name in re.split(r"[\s,，]+", value.strip()):
        if not name:
            continue
        try:
            name = name.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UserError("下载域名格式无效。") from exc
        if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}", name):
            raise UserError("请填写完整域名，例如 invoice.example.com。")
        domains.add(name)
    return sorted(domains)


def date_bounds(start, end):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if (first.isoformat(), last.isoformat()) != (start, end) or last < first:
            raise ValueError
        return tuple(datetime.combine(day, time.min).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                     for day in (first, last + timedelta(days=1)))
    except (ValueError, OverflowError, OSError) as exc:
        raise UserError("请填写有效的起止日期，格式为 YYYY-MM-DD，结束日期须在开始日期当天或之后。") from exc


def _url(value):
    try:
        parts = urlsplit(value)
        if parts.scheme == "https" and parts.hostname and not parts.username and parts.password is None and parts.port in (None, 443):
            return parts
    except ValueError:
        pass
    raise UserError("链接不是有效的 HTTPS 地址。")


def _redirect_url(url, location):
    try:
        return urljoin(url, location)
    except ValueError as exc:
        raise UserError("下载跳转地址无效，请打开原邮件处理。") from exc


class InvoiceLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.links = {}

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs = dict(attrs)
            self.current = (attrs.get("href") or "", [attrs.get("title") or ""])
        elif tag == "img" and self.current:
            self.current[1].append(dict(attrs).get("alt") or "")

    def handle_data(self, text):
        if self.current:
            self.current[1].append(text)

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            url, parts = self.current
            self.current = None
            label = " ".join(" ".join(parts).split())
            try:
                path = unquote(urlsplit(url).path)
            except ValueError:
                path = ""
            if url and (INVOICE_HINT.search(label) or any(path.lower().endswith(ext) for ext in SUPPORTED)):
                self.links.setdefault(url, label[:160] or "发票下载链接")


def _read(response, max_bytes):
    length = response.getheader("Content-Length", "")
    if length.isdigit() and int(length) > max_bytes:
        raise UserError("下载内容超过大小限制。")
    data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UserError("下载内容超过大小限制。")
    return data


def download_invoice(url, blocked):
    for _ in range(6):
        parts = _url(url)
        try:
            host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UserError("下载域名格式无效。") from exc
        if any(host == domain or host.endswith("." + domain) for domain in blocked):
            raise UserError("下载域名已在黑名单中，请打开原邮件处理。")
        context = create_default_context()
        conn = HTTPSConnection(host, timeout=15, context=context)
        try:
            addresses = getaddrinfo(host, 443, type=SOCK_STREAM)
            resolved = [ip_address(row[4][0]) for row in addresses]
            if not resolved or any(not address.is_global or address.is_multicast or address.is_reserved for address in resolved):
                raise UserError("下载域名未指向公网地址。")
            # 使用已检查的地址连接，同时保留域名用于 TLS 证书校验。
            with create_connection(addresses[0][4][:2], timeout=15) as socket:
                conn.sock = context.wrap_socket(socket, server_hostname=host)
                path = parts.path or "/"
                path = quote(path, safe="/%:@!$&'()*+,;=-._~")
                if parts.query:
                    path += "?" + quote(parts.query, safe="/%?:@!$&'()*+,;=-._~[]")
                conn.request("GET", path)
                with conn.getresponse() as response:
                    if response.status in {301, 302, 303, 307, 308} and (location := response.getheader("Location")):
                        url = _redirect_url(url, location)
                        continue
                    if response.status != 200:
                        raise UserError(f"下载失败（HTTP {response.status}），请打开原邮件处理。")
                    content_type = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
                    if content_type in {"text/html", "application/xhtml+xml"}:
                        raise UserError("链接返回网页，可能需要登录或验证码，请打开原邮件处理。")
                    disposition = Message()
                    disposition["Content-Disposition"] = response.getheader("Content-Disposition", "")
                    name = _filename(disposition.get_filename() or unquote(parts.path.rsplit("/", 1)[-1]))
                    if not any(name.lower().endswith(ext) for ext in SUPPORTED):
                        suffix = MIME_SUFFIX.get(content_type)
                        if not suffix:
                            raise UserError("链接未返回支持的发票文件，请打开原邮件处理。")
                        name = "发票" + suffix
                    return name, _read(response, MAX_BYTES)
        except (OSError, HTTPException) as exc:
            raise UserError("下载连接失败，请重试或打开原邮件。") from exc
        finally:
            conn.close()
    raise UserError("下载跳转次数过多，请打开原邮件处理。")


def _filename(name):
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def _message_body(response):
    raw = response.get(b"BODY[]")
    if not isinstance(raw, bytes) or not raw:
        return None, "邮箱未返回邮件正文，请重新收取或按主题到邮箱中查看。"
    if len(raw) > MAX_MESSAGE_BYTES:
        return None, "邮件超过 80 MB，请按主题到邮箱中查看。"
    return raw, ""


def _folder_options(listing):
    folders = [name for flags, _, name in listing if b"\\noselect" not in {flag.lower() for flag in flags}]
    if not folders:
        raise UserError("该邮箱没有可读取的文件夹。")
    folders.sort(key=lambda name: (name.casefold() != "inbox", name.casefold()))
    return [(name, "收件箱" if name.casefold() == "inbox" else name) for name in folders]


class IMAPMailbox:
    def __init__(self, profile, password="", outlook_oauth=None):
        self.profile = profile
        self.password = password
        self.outlook_oauth = outlook_oauth

    @contextmanager
    def _connection(self, *, allow_interactive=False, cancelled=None):
        address, host, port = self.profile
        outlook = host == OUTLOOK_IMAP_HOST
        token = None
        if outlook:
            if not self.outlook_oauth:
                raise UserError("请通过 Microsoft 浏览器授权连接 Outlook IMAP。")
            token = self.outlook_oauth.access_token(allow_interactive=allow_interactive, cancelled=cancelled)
        if cancelled is not None and cancelled.is_set():
            raise UserError("已取消 Outlook 邮箱连接。")
        client = None
        try:
            context = create_default_context()
            client = IMAPClient(host, port=port, ssl=True, ssl_context=context, timeout=20)
            client.normalise_times = False
            if outlook:
                client.oauth2_login(address, token)
            else:
                client.login(address, self.password)
            yield client
        except LoginError:
            if outlook:
                raise UserError("Outlook 拒绝 IMAP 登录。请确认已在 Outlook 网页版开启 IMAP，且授权账号与填写的邮箱一致。") from None
            raise UserError("邮箱认证失败。请确认已开启 IMAP，并使用服务商生成的客户端授权码或应用专用密码。") from None
        except SSLCertVerificationError:
            raise UserError("无法验证 IMAP 服务器证书，请核对服务器地址。") from None
        except (IMAPClientError, OSError) as exc:
            raise UserError("无法读取 IMAP 邮箱，请检查服务器地址、端口、网络和 IMAP 服务状态。") from exc
        finally:
            if client is not None:
                client.shutdown()

    def folders(self, *, cancelled=None):
        with self._connection(allow_interactive=True, cancelled=cancelled) as client:
            return _folder_options(client.list_folders())

    def messages(self, folder, start, end):
        first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
        # IMAP 日期搜索使用服务器时区，预留相邻日期，再按本机日期边界精确筛选。
        since = date.fromordinal(max(1, first.date().toordinal() - 1))
        before = date.fromordinal(min(date.max.toordinal(), last.date().toordinal() + 1))
        with self._connection() as client:
            validity = client.select_folder(folder, readonly=True).get(b"UIDVALIDITY", 0)
            ids = sorted(client.search(["SINCE", since, "BEFORE", before]), reverse=True)
            for offset in range(0, len(ids), 50):
                batch = ids[offset:offset + 50]
                metadata = client.fetch(batch, ["INTERNALDATE", "RFC822.SIZE", "BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)]"])
                for uid in batch:
                    row = metadata.get(uid)
                    if not row:
                        continue
                    received = row[b"INTERNALDATE"]
                    if not first <= received < last:
                        continue
                    header = next((value for key, value in row.items() if key.startswith(b"BODY[HEADER.FIELDS ")), b"")
                    message = BytesParser(policy=policy.default).parsebytes(header or b"")
                    info = {"message_id": str(message.get("Message-ID") or f"{validity}:{uid}"),
                            "mailbox": self.profile[0], "folder": folder, "received": received.isoformat(),
                            "sender": str(message.get("From") or ""), "subject": str(message.get("Subject") or "")}
                    if row[b"RFC822.SIZE"] > MAX_MESSAGE_BYTES:
                        yield info, None, "服务器报告邮件超过 80 MB，请按主题到邮箱中查看。"
                        continue
                    # RFC822.SIZE 用于提前限制下载量。完整 FETCH 响应按实际收到的字节检查。
                    body = client.fetch([uid], ["BODY.PEEK[]"]).get(uid, {})
                    raw, error = _message_body(body)
                    yield info, raw, error


def _mail_links(part, links):
    try:
        content = part.get_content()
    except LookupError:
        return "邮件正文使用了无法读取的字符编码，请打开原邮件处理。"
    if part.get_content_type() == "text/html":
        parser = InvoiceLinks()
        parser.feed(content)
        parser.close()
        links.update(parser.links)
    else:
        for line in content.splitlines():
            for match in re.finditer(r"https?://[^\s<>\"'，。]+", line):
                link = match.group().rstrip(".,)")
                try:
                    path = unquote(urlsplit(link).path)
                except ValueError:
                    continue
                if INVOICE_HINT.search(line) or any(path.lower().endswith(ext) for ext in SUPPORTED):
                    links.setdefault(link, "发票下载链接")
    return ""


def _collect_message(raw, metadata, blocked, staging, sequence):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    rows = []
    links = {}
    xml_in_message = False

    def stage(row, data, index):
        path = staging / f"{sequence}-{index}"
        path.write_bytes(data)
        row["payload"] = path

    def read_attachment(row, part, index):
        data = part.get_payload(decode=True) or b""
        if len(data) > MAX_BYTES:
            row["error"] = "邮件附件超过 20 MB。"
        else:
            stage(row, data, index)

    for part in message.walk():
        if part.is_multipart():
            continue
        name = _filename(part.get_filename() or "")
        suffix = Path(name).suffix.lower()
        if suffix == ".xml" or part.get_content_type() in {"application/xml", "text/xml"}:
            xml_in_message = True
        if suffix in SUPPORTED:
            if part.get_content_disposition() == "inline" and not INVOICE_HINT.search(name + " " + metadata["subject"]):
                continue
            row = {"file": name, **metadata, "attachment": True}
            index = len(rows)
            rows.append(row)
            read_attachment(row, part, index)
        elif not name and part.get_content_disposition() != "attachment" and part.get_content_type() in {"text/html", "text/plain"}:
            if problem := _mail_links(part, links):
                rows.append({"file": "邮件正文", **metadata, "error": problem})
    for link, label in links.items():
        try:
            name, data = download_invoice(link, blocked)
        except UserError as exc:
            rows.append({"file": label, **metadata, "error": str(exc)})
        else:
            row = {"file": name, **metadata}
            stage(row, data, len(rows))
            rows.append(row)

    if xml_in_message:
        for row in rows:
            row["xml_in_message"] = True
    elif rows:
        path = staging / f"mail-{sequence}.eml"
        path.write_bytes(raw)
        for row in rows:
            row["original_message"] = path
    return rows


def collect(mailbox, folder, bounds, blocked, staging, progress):
    rows = []
    scanned = 0

    try:
        for metadata, raw, error in mailbox.messages(folder, *bounds):
            scanned += 1
            if progress:
                progress(f"正在读取第 {scanned} 封邮件…")
            if error:
                rows.append({"file": "邮件原件", **metadata, "error": error})
                continue
            rows.extend(_collect_message(raw, metadata, blocked, staging, scanned))
    except UserError as exc:
        rows.append({"file": "邮件列表", "error": str(exc)})
    return scanned, rows
