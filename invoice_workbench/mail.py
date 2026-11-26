from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesHeaderParser, BytesParser
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
from http.client import HTTPSConnection, HTTPException
from ipaddress import ip_address
from pathlib import Path
import re
from socket import create_connection, getaddrinfo, SOCK_STREAM
from ssl import SSLCertVerificationError, create_default_context
from time import monotonic
import unicodedata
from urllib.parse import quote, unquote, urljoin, urlsplit

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError, ProtocolError
from imapclient.response_types import BodyData

from .domain import UserError
from .extract import (DECORATIVE_IMAGE, IMAGE_SUFFIXES, MAX_BYTES, SUPPORTED, has_invoice_content, has_invoice_title,
                      has_required_fields, inspect_mail_document)
from .outlook_auth import OUTLOOK_IMAP_HOST, PERSONAL_OUTLOOK_DOMAINS
from .recognition import mail_qr


OUTLOOK_HOSTS = {"outlook.office.com", "outlook.office365.com", "outlook.live.com", "outlook.cloud.microsoft"}
IMAP_HOSTS = {"qq.com": "imap.qq.com", "foxmail.com": "imap.qq.com", "163.com": "imap.163.com",
              "126.com": "imap.126.com", "yeah.net": "imap.yeah.net",
              **dict.fromkeys(PERSONAL_OUTLOOK_DOMAINS, OUTLOOK_IMAP_HOST)}
MAIL_HEADERS = "FROM SUBJECT MESSAGE-ID DATE CONTENT-TYPE CONTENT-DISPOSITION CONTENT-TRANSFER-ENCODING"
PREVIEW_BYTES = 64 * 1024
DEEP_PREVIEW_BYTES = 512 * 1024
LARGE_MAIL_BYTES = 5 * 1024 * 1024
CONNECT_TIMEOUT = 6.0  # 单个 IP 的 TCP + TLS 建连上限，读取超时仍为 15s
CONNECT_BUDGET = 15.0  # 单次多 IP 回退的累计建连预算
MAIL_SCREENING_VERSION = 1
SCREEN_CACHE_SIZE = 1024
MAX_FORWARDED_BYTES = MAX_BYTES * 2
INVOICE_HINT = re.compile(r"[发發]\s*票|\binvoices?\b", re.I)
INVOICE_GUIDANCE = re.compile(
    r"[发發]\s*票\s*(?:申请|登记|开具|管理|报销)?\s*(?:流程|说明|指南|规则|政策|操作手册|常见问题)"
    r"|\binvoices?\s+(?:(?:application|submission|approval)\s+)?"
    r"(?:procedures?|instructions?|guides?|polic(?:y|ies)|rules?)\b", re.I
)
DELIVERY_HINT = re.compile(r"已开具|已发送|附件|附上|随附|请查收|请查阅|下载|查看|获取|"
                           r"attached|attachment|enclosed|issued|download|view", re.I)
IRRELEVANT_LINK = re.compile(r"退订|隐私|帮助|指南|常见问题|流程|操作说明|"
                             r"unsubscribe|privacy|help|guide|faq|\b(?:procedures?|instructions?)\b", re.I)
# 订单、物流等导航链接即使所在段落提到“发票/下载”也不是发票下载地址。
NON_INVOICE_LINK = re.compile(
    r"查看订单|订单详情|订单跟踪|查看物流|物流查询|我的订单|购物车|立即购买|取消订阅|"
    r"\borders?\b|\btracking\b|\blogistics?\b|\bunsubscribe\b|\bpreferences?\b|\bcart\b|\bcheckout\b",
    re.I)
URL = re.compile(r"https?://[^\s<>\"'，。]+", re.I)
MIME_SUFFIX = {"application/pdf": ".pdf", "application/xml": ".xml", "text/xml": ".xml",
               "image/png": ".png", "image/jpeg": ".jpg", "application/ofd": ".ofd", "application/x-ofd": ".ofd"}
BODY_TYPES = {"text/plain", "text/html"}
BLOCK_TAGS = {"p", "div", "li", "br", "tr", "table", "section", "hr", "h1", "h2", "h3"}


@dataclass(frozen=True)
class MailPart:
    section: str
    mime_section: str
    media: str
    name: str
    disposition: str
    encoding: str
    size: int
    charset: str = ""
    forwarded: tuple = ()

    @property
    def suffix(self):
        return Path(self.name).suffix.lower() or MIME_SUFFIX.get(self.media, "")


@dataclass(frozen=True)
class MailMessage:
    metadata: dict
    header: bytes
    parts: tuple[MailPart, ...]
    bodies: tuple[MailPart, ...]

    @property
    def uid(self):
        return self.metadata["imap_uid"]


class PartFetchRejected(UserError):
    pass


class CollectionCancelled(Exception):
    """用户取消收票，用于立即停止后续网络请求。"""


def _cancelled(value):
    return value is not None and value.is_set()


def _timeline(value):
    """把 IMAP 返回的时间统一为带时区的 UTC 时间，缺失时区时按 UTC 处理。"""
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def _header_datetime(header):
    """INTERNALDATE 缺失或无法解析时，回退到邮件头 Date 字段。"""
    if not isinstance(header, bytes) or not header:
        return None
    try:
        message = BytesHeaderParser(policy=policy.default).parsebytes(header)
        stamp = parsedate_to_datetime(str(message.get("Date") or ""))
    except (TypeError, ValueError, IndexError, HeaderParseError):
        return None
    return stamp if isinstance(stamp, datetime) else None


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


def _invoice_hint(text):
    normalized = unicodedata.normalize("NFKC", text)
    return bool(INVOICE_HINT.search(INVOICE_GUIDANCE.sub(" ", normalized)))


def _delivery_hint(text):
    return any(_invoice_hint(line) and DELIVERY_HINT.search(line) for line in text.splitlines())


def _find_link(url, label, nearby, subject):
    if not url.lower().startswith(("https://", "http://")):
        return False
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        path = ""
    filename = Path(path).name
    # 明确的非发票导航（订单、物流、退订等）即使所在段落提到“发票/下载”也不抓取。
    if IRRELEVANT_LINK.search(label) or NON_INVOICE_LINK.search(label) or NON_INVOICE_LINK.search(path):
        return False
    suffix = Path(filename).suffix.lower()
    # 链接自身的锚文本、title 或文件名带发票特征时才直接采用。
    if _invoice_hint(label) or _invoice_hint(filename):
        return True
    # 仅凭上下文还不够：还要有受支持的文件扩展名，或明确的“点击这里”类下载措辞。
    if not (_invoice_hint(nearby) or _invoice_hint(subject)):
        return False
    return (suffix in SUPPORTED
            or (_delivery_hint(nearby) and label.casefold() in {"点击这里", "点击此处", "click here"}))


class InvoiceLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.blocks = [[]]
        self.anchors = []
        self.hidden = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden = tag
        if self.hidden:
            return
        if tag in BLOCK_TAGS and self.blocks[-1]:
            self.blocks.append([])
        if tag == "a":
            attrs = dict(attrs)
            self.current = (attrs.get("href") or "", [attrs.get("title") or ""], len(self.blocks) - 1)
        elif tag == "img" and self.current:
            self.current[1].append(dict(attrs).get("alt") or "")

    def handle_data(self, text):
        if self.hidden or not text.strip():
            return
        self.blocks[-1].append(text)
        if self.current:
            self.current[1].append(text)

    def handle_endtag(self, tag):
        if tag == self.hidden:
            self.hidden = None
            return
        if self.hidden:
            return
        if tag == "a" and self.current:
            self._finish_anchor()
        if tag in BLOCK_TAGS and self.blocks[-1]:
            self.blocks.append([])

    def _finish_anchor(self):
        url, parts, index = self.current
        self.current = None
        if url:
            self.anchors.append((url, " ".join(" ".join(parts).split()), index))

    def discover(self, subject, links):
        if self.current:
            self._finish_anchor()
        paragraphs = [" ".join(parts) for parts in self.blocks]
        for url, label, index in self.anchors:
            nearby = " ".join(paragraphs[max(0, index - 1):index + 1])
            if _find_link(url, label, nearby, subject):
                links.setdefault(url, label[:160] or "发票下载链接")
        return "\n".join(paragraphs)


def _read(response, max_bytes):
    length = response.getheader("Content-Length", "")
    if length.isdigit() and int(length) > max_bytes:
        raise UserError("下载内容超过大小限制。")
    data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UserError("下载内容超过大小限制。")
    return data


def _connect_checked(host, addresses, context, timeout=CONNECT_TIMEOUT, budget=CONNECT_BUDGET):
    """按 DNS 返回顺序尝试建立 TCP + TLS 连接，仅在所有地址失败时抛出异常。

    地址均已通过公网校验。单个地址的等待时间收紧，并为整轮回退设累计预算，
    避免前几个黑洞节点各等满超时，把成功地址拖到几十秒之后。
    """
    failure = None
    deadline = monotonic() + budget
    for family, _, proto, _, address in addresses:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        candidate = None
        try:
            candidate = create_connection(address[:2], timeout=min(timeout, remaining))
            return context.wrap_socket(candidate, server_hostname=host)
        except OSError as exc:
            failure = exc
            if candidate is not None:
                candidate.close()
    raise OSError("无法连接下载地址") from failure


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
            # 使用已检查的地址逐个尝试连接，同时保留域名用于 TLS 证书校验。
            with _connect_checked(host, addresses, context) as socket:
                conn.sock = socket
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


def _metadata_name(media, params, disposition, disposition_params):
    fields = [
        (header, key.decode("ascii"), value.decode("utf-8", "replace"))
        for header, prefix, raw in (("Content-Type", b"name", params),
                                    ("Content-Disposition", b"filename", disposition_params))
        for key, value in zip(raw[::2], raw[1::2])
        if isinstance(key, bytes) and isinstance(value, bytes)
        and re.fullmatch(prefix + rb"(?:\*\d+\*?|\*)?", key.lower())
    ]
    if not fields:
        return ""
    mime = Message()
    mime["Content-Type"] = media
    if disposition:
        mime["Content-Disposition"] = disposition
    for header, key, value in fields:
        mime.set_param(key, value, header=header)
    name = mime.get_filename() or ""
    try:
        name = str(make_header(decode_header(name)))
    except (HeaderParseError, LookupError, UnicodeError):
        pass  # 候选 MIME 部分下载时按原始邮件头重新解码文件名。
    return _filename(name)


def _parts(structure, section="", depth=0):
    if depth > 32 or not isinstance(structure, BodyData) or not structure:
        raise UserError("邮箱返回的邮件结构无法读取。")
    if structure.is_multipart:
        for index, child in enumerate(structure[0], 1):
            prefix = f"{section}.{index}" if section else str(index)
            yield from _parts(child, prefix, depth=depth + 1)
        return
    if len(structure) < 7 or not all(isinstance(structure[index], bytes) for index in (0, 1, 5)):
        raise UserError("邮箱返回的邮件部分信息不完整。")
    media = b"/".join(structure[:2]).decode("ascii", "replace").lower()
    offset = {"message/rfc822": 11}.get(media, 9 if media.startswith("text/") else 8)
    extension = structure[offset] if len(structure) > offset else None
    disposition, disposition_params = "", ()
    if isinstance(extension, tuple) and len(extension) == 2 and isinstance(extension[0], bytes):
        disposition = extension[0].decode("ascii", "replace").lower()
        disposition_params = extension[1] or ()
    params = structure[2] or ()
    name = _metadata_name(media, params, disposition, disposition_params)
    charset = next((value.decode("ascii", "replace") for key, value in zip(params[::2], params[1::2])
                    if isinstance(key, bytes) and key.lower() == b"charset" and isinstance(value, bytes)), "")
    size = structure[6]
    if not isinstance(size, int) or size < 0:
        raise UserError("邮箱返回的邮件部分大小无效。")
    mime_section = f"{section}.MIME" if section else ""
    section = section or "1"
    encoding = structure[5].decode("ascii", "replace").lower()
    forwarded = ()
    if media == "message/rfc822" and encoding in {"7bit", "8bit", "binary"} and len(structure) > 8:
        nested = structure[8]
        if isinstance(nested, tuple) and nested:
            body = BodyData.create(nested)
            prefix = section if body.is_multipart else f"{section}.1"
            forwarded = tuple(_parts(body, prefix, depth=depth + 1))
    yield MailPart(section, mime_section, media, name, disposition, encoding, size, charset, forwarded)


def _body_parts(parts):
    return tuple(part for part in parts if part.size and part.media in BODY_TYPES and not part.name
                 and part.disposition != "attachment")


def _part_requests(part, limit, *, include_mime=True):
    data = f"BODY.PEEK[{part.section}]"
    if part.size > limit:
        data += f"<0.{limit + 1}>"
    return (f"BODY.PEEK[{part.mime_section}]", data) if include_mime and part.mime_section else (data,)


def _decode_part(message, part, response, limit, *, preview=False):
    key = f"BODY[{part.section}]".encode("ascii")
    encoded = response.get(key + b"<0>", response.get(key))
    if not isinstance(encoded, bytes):
        raise UserError("邮箱未返回所请求的邮件内容。")
    if not preview:
        if len(encoded) > limit:
            raise UserError("邮件附件超过 20 MB。")
        header = message.header if not part.mime_section else response.get(f"BODY[{part.mime_section}]".encode("ascii"))
        if isinstance(header, bytes):
            parsed = BytesHeaderParser(policy=policy.default).parsebytes(header)
            if parsed.get_content_type() == part.media and str(parsed.get("Content-Transfer-Encoding", "")).lower() == part.encoding:
                parsed.set_payload(encoded)
                return parsed, len(encoded)
    # 使用 BODYSTRUCTURE 的类型和编码处理缺少邮件头的部分与正文预览。
    parsed = EmailMessage(policy=policy.default)
    parsed.set_type(part.media)
    if part.charset:
        parsed.set_param("charset", part.charset)
    parsed["Content-Transfer-Encoding"] = part.encoding
    parsed.set_payload(encoded[:limit])
    return parsed, len(encoded)


def _fetch_parts(client, uids, requests):
    try:
        return client.fetch(uids, requests)
    except IMAPClientAbortError:
        raise
    except ProtocolError:
        raise UserError("邮箱返回的邮件部分响应格式无效。") from None
    except IMAPClientError:
        raise PartFetchRejected("邮箱拒绝读取所请求的邮件部分。") from None


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
        self.forwarded_mode = {"sections": True}

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

def _ignorable_message(parts, subject):
    """超大邮件若既无受支持附件、正文，也无发票关键词，则在正文拉取前跳过。"""
    if _invoice_hint(subject) or sum(part.size for part in parts) <= LARGE_MAIL_BYTES:
        return False
    if any(part.media in BODY_TYPES and not part.name and part.disposition != "attachment" for part in parts):
        return False
    return not any(part.suffix in SUPPORTED or any(child.suffix in SUPPORTED for child in part.forwarded)
                   for part in parts)


def _message_batches(client, profile, folder, bounds, max_bodies, stats, rows, previous, checkpoint,
                     *, cancelled=None):
    first, last = map(datetime.fromisoformat, bounds)
    # 日期搜索预留相邻日期，再按本机日期边界精确筛选。
    since = date.fromordinal(max(1, first.date().toordinal() - 1))
    before = date.fromordinal(min(date.max.toordinal(), last.date().toordinal() + 1))
    validity = client.select_folder(folder, readonly=True).get(b"UIDVALIDITY", 0)
    ids = sorted(client.search(["SINCE", since, "BEFORE", before]), reverse=True)
    checkpoint["uidvalidity"] = validity
    if validity and validity == previous.get("uidvalidity"):
        completed = previous.get("messages", {})
        minimum = "deep" if max_bodies == 8 else "default"
        reusable = {uid for uid in ids if isinstance(entry := completed.get(str(uid)), (list, tuple)) and len(entry) > 1
                    and first <= _timeline(entry[0]) < last and (entry[1] == "deep" or minimum == "default")}
        stats["reused"] += len(reusable)
        ids = [uid for uid in ids if uid not in reusable]
    for offset in range(0, len(ids), 50):
        if _cancelled(cancelled):
            raise CollectionCancelled()
        batch = ids[offset:offset + 50]
        stats["metadata_reads"] += len(batch)
        responses = client.fetch(batch, ["INTERNALDATE", "BODYSTRUCTURE", f"BODY.PEEK[HEADER.FIELDS ({MAIL_HEADERS})]"])
        messages = []
        for uid in batch:
            row = responses.get(uid, {})
            metadata = {"message_id": f"{validity}:{uid}", "mailbox": profile[0], "imap_host": profile[1], "imap_port": profile[2],
                        "folder": folder, "imap_uid": uid, "imap_uidvalidity": validity}
            header = next((value for key, value in row.items() if key.startswith(b"BODY[HEADER.FIELDS ")), None)
            received = row.get(b"INTERNALDATE")
            if not isinstance(received, datetime):
                # 部分服务器的 INTERNALDATE 不带时区，imapclient 会解析成 None，回退解析 Date 头。
                received = _header_datetime(header)
            if not isinstance(received, datetime):
                checkpoint["messages"][str(uid)] = None
                rows.append({"file": "邮件头", **metadata, "error": "邮箱未返回有效的邮件收件时间。"})
                continue
            received = _timeline(received)
            metadata["received"] = received.isoformat()
            try:
                if not first <= received < last:
                    continue
                stats["scanned"] += 1
                checkpoint["messages"][str(uid)] = None
                if not isinstance(header, bytes) or not header:
                    rows.append({"file": "邮件头", **metadata, "error": "邮箱未返回邮件头。"})
                    continue
                message = BytesHeaderParser(policy=policy.default).parsebytes(header)
                metadata.update(message_id=str(message.get("Message-ID") or metadata["message_id"]),
                                sender=str(message.get("From") or ""),
                                subject=str(message.get("Subject") or ""))
                try:
                    parts = tuple(_parts(row.get(b"BODYSTRUCTURE")))
                except UserError as exc:
                    rows.append({"file": "邮件结构", **metadata, "error": str(exc)})
                    continue
                if _ignorable_message(parts, metadata["subject"]):
                    stats["filtered"] += 1
                    continue
                bodies = _body_parts(parts)
                if len(bodies) > max_bodies:
                    stats["partial_bodies"] += len(bodies) - max_bodies
                messages.append((MailMessage(metadata, header, parts, bodies[:max_bodies]), len(bodies) > max_bodies))
            except (ValueError, TypeError, LookupError) as exc:
                rows.append({"file": "邮件头", **metadata, "error": f"无法读取邮件时间或结构：{exc}"})
        yield messages


def _read_previews(client, batch, limit):
    groups = {}
    for message in batch:
        requests = tuple(request for part in message.bodies for request in _part_requests(part, limit, include_mime=False))
        if requests:
            groups.setdefault(requests, []).append(message.uid)
    responses = {}
    for requests, uids in groups.items():
        for offset in range(0, len(uids), 12):
            group = uids[offset:offset + 12]
            try:
                responses.update(_fetch_parts(client, group, requests))
            except UserError as exc:
                responses.update(dict.fromkeys(group, str(exc)))
    return responses


def _mail_links(part, subject, links, *, limit=None):
    try:
        content = part.get_content()
    except LookupError:
        raise UserError("邮件正文使用了无法读取的字符编码。") from None
    if not isinstance(content, str):
        raise UserError("邮件正文的 MIME 格式无效。")
    content = content[:limit]
    if part.get_content_type() == "text/html":
        parser = InvoiceLinks()
        parser.feed(content)
        parser.close()
        return parser.discover(subject, links)
    lines = content.splitlines()
    cleaned = [URL.sub("", line) for line in lines]
    for index, line in enumerate(lines):
        context = " ".join(cleaned[max(0, index - 1):index + 1])
        for match in URL.finditer(line):
            link = match.group().rstrip(".,)")
            if _find_link(link, cleaned[index].strip(), context, subject):
                links.setdefault(link, "发票下载链接")
    return "\n".join(cleaned)


def _fetch_part(client, message, part, limit):
    response = _fetch_parts(client, [message.uid], _part_requests(part, limit, include_mime=not part.name)).get(message.uid, {})
    return _decode_part(message, part, response, limit)


def _read_attachment(client, message, part, stats, *, mime=None):
    if mime is None:
        limit = MAX_BYTES
        if part.encoding == "base64":
            limit = MAX_BYTES * 4 // 3 + MAX_BYTES // 16 + 4096
        elif part.encoding == "quoted-printable":
            limit = MAX_BYTES * 4
        if part.size > limit:
            raise UserError("邮件附件超过 20 MB。")
        mime, received = _fetch_part(client, message, part, limit)
        stats["attachment_bytes"] += received
    name = _filename(mime.get_filename() or part.name)
    if not Path(name).suffix:
        name = (name or "邮件附件") + part.suffix
    if Path(name).suffix.lower() not in SUPPORTED:
        raise UserError("邮件附件的文件名与支持的发票格式不匹配。")
    return name, mime.get_payload(decode=True) or b""


def _read_forwarded(client, message, part, stats):
    # 将封装邮件的头和正文作为单独的 MIME 部分读取。
    if part.size > MAX_FORWARDED_BYTES:
        raise UserError("转发邮件附件超过 40 MB。")
    sections = f"{part.section}.HEADER", f"{part.section}.TEXT"
    requests = [f"BODY.PEEK[{section}]" for section in sections]
    response = _fetch_parts(client, [message.uid], requests).get(message.uid, {})
    header, body = (response.get(f"BODY[{section}]".encode("ascii")) for section in sections)
    if not isinstance(header, bytes) or not header or not isinstance(body, bytes):
        raise UserError("邮箱未返回转发邮件的完整内容。")
    size = len(header) + len(body)
    stats["attachment_bytes"] += size
    if size > MAX_FORWARDED_BYTES:
        raise UserError("转发邮件附件超过 40 MB。")
    mime = BytesParser(policy=policy.default).parsebytes(header.rstrip(b"\r\n") + b"\r\n\r\n" + body)
    return mime.walk()


def _preview_forwarded(client, message, part, stats, limit, max_bodies, subjects, links):
    bodies = _body_parts(part.forwarded)
    stats["partial_bodies"] += max(0, len(bodies) - max_bodies)
    bodies = bodies[:max_bodies]
    header = f"{part.section}.HEADER.FIELDS (SUBJECT)"
    requests = [f"BODY.PEEK[{header}]"]
    requests.extend(request for body in bodies for request in _part_requests(body, limit, include_mime=False))
    if not bodies and (attachment := next((child for child in part.forwarded if child.suffix in SUPPORTED), None)):
        requests.append(f"BODY.PEEK[{attachment.section}]<0.1>")
    response = _fetch_parts(client, [message.uid], requests).get(message.uid, {})
    raw = response.get(f"BODY[{header}]".encode("ascii"))
    if not isinstance(raw, bytes):
        raise UserError("邮箱未返回转发邮件的主题。")
    title = BytesHeaderParser(policy=policy.default).parsebytes(raw).get("Subject")
    if title:
        subjects.append(str(title))
    stats["body_bytes"] += len(raw)
    text = []
    for body in bodies:
        mime, received = _decode_part(message, body, response, limit, preview=True)
        stats["body_bytes"] += received
        stats["partial_bodies"] += int(received > limit or body.size > limit)
        text.append(_mail_links(mime, "\n".join(subjects), links))
    return text


def _stage_mail_file(data, name, row, staging, digest, *, evidence=None, hinted=False):
    path = staging / digest
    if not path.exists():
        path.write_bytes(data)
    row.update(file=name, payload=path, mail_evidence=evidence, mail_digest=digest, mail_hint=hinted)


def _screen_file(data, name, row, staging, stats, cache, *, allow_ocr, hinted,
                 image_context=False, image_ocr=True):
    suffix = Path(name).suffix.lower()
    digest = sha256(data).hexdigest()
    key = digest, suffix
    if len(cache) >= SCREEN_CACHE_SIZE and key not in cache:
        cache.pop(next(iter(cache)))
    evidence, deferred, qr, standalone = cache.get(key, (None, False, False, False))
    if evidence is None and suffix in IMAGE_SUFFIXES and key not in cache:
        if not data or len(data) > MAX_BYTES:
            raise UserError("图片为空或超过 20 MB。")
        qr, standalone = mail_qr(data)
        cache[key] = (None, False, qr, standalone)
    if qr and standalone:
        if not image_context:
            return False
        row["mail_review"] = "发票交付内容含二维码，请人工查看。"
    elif not image_ocr:
        if qr and image_context:
            row["mail_review"] = "发票交付内容含二维码，请人工查看。"
        else:
            stats["deferred"] += 1
            return False
    else:
        if evidence is None or deferred and allow_ocr:
            evidence, deferred, ran_ocr = inspect_mail_document(data, suffix, allow_ocr=allow_ocr)
            cache[key] = (evidence, deferred, qr, standalone)
            stats["ocr_files"] += int(ran_ocr)
        if deferred:
            stats["deferred"] += 1
            return False
        title = has_invoice_title(evidence)
        if not (has_invoice_content(evidence) or title or suffix == ".ofd" and allow_ocr):
            if qr and image_context:
                row["mail_review"] = "发票交付内容含二维码，请人工查看。"
            elif hinted and evidence.get("warnings"):
                row["error"] = "附件未识别出发票内容。" + " ".join(evidence["warnings"])
                return True
            else:
                return False
        if not (hinted or title or qr and image_context or suffix == ".ofd"
                or suffix == ".xml" and has_required_fields(evidence)):
            return False
    _stage_mail_file(data, name, row, staging, digest, evidence=evidence, hinted=hinted)
    return True


def _collect_message(client, message, response, limit, blocked, staging, stats, cache, progress, *,
                     deep_scan, forwarded_mode, cancelled=None):
    if _cancelled(cancelled):
        raise CollectionCancelled()
    metadata = message.metadata
    subject = metadata["subject"]
    links = {}
    notes = []
    body_text = []
    candidates = []
    subjects = [subject]
    if isinstance(response, str):
        notes.append({"file": "邮件正文", **metadata, "error": response})
    else:
        for body in message.bodies:
            try:
                mime, received = _decode_part(message, body, response, limit, preview=True)
                stats["body_bytes"] += received
                if received > limit or body.size > limit:
                    stats["partial_bodies"] += 1
                body_text.append(_mail_links(mime, subject, links))
            except UserError as exc:
                notes.append({"file": "邮件正文", **metadata, "error": str(exc)})
    pending_parts = list(reversed(message.parts))
    while pending_parts:
        if _cancelled(cancelled):
            raise CollectionCancelled()
        part = pending_parts.pop()
        if part.media != "message/rfc822":
            candidates.append((part, None))
            continue
        if part.forwarded and forwarded_mode["sections"]:
            try:
                preview = _preview_forwarded(client, message, part, stats, limit, 8 if deep_scan else 4, subjects, links)
            except PartFetchRejected:
                # 部分 IMAP 服务器拒绝读取转发邮件的内部节，后续转发邮件使用有大小限制的读取。
                forwarded_mode["sections"] = False
            except UserError as exc:
                pending_parts.extend(reversed(part.forwarded))
                notes.append({"file": "转发邮件正文", **metadata, "error": str(exc)})
                continue
            else:
                pending_parts.extend(reversed(part.forwarded))
                body_text.extend(preview)
                continue
        try:
            forwarded = _read_forwarded(client, message, part, stats)
        except UserError as exc:
            notes.append({"file": part.name or "转发邮件附件", **metadata, "error": str(exc)})
            continue
        for mime in forwarded:
            if title := mime.get("Subject"):
                subjects.append(str(title))
            if mime.is_multipart():
                continue
            media = mime.get_content_type()
            name = _filename(mime.get_filename() or "")
            disposition = mime.get_content_disposition() or ""
            if media in BODY_TYPES and not name and disposition != "attachment":
                context = "\n".join(subjects)
                try:
                    body_text.append(_mail_links(mime, context, links, limit=limit))
                except UserError as exc:
                    notes.append({"file": "转发邮件正文", **metadata, "error": str(exc)})
            else:
                candidates.append((MailPart("", "", media, name, disposition, "", 0), mime))
    subjects = "\n".join(subjects)
    delivery = _delivery_hint(subjects + "\n" + "\n".join(body_text))
    invoice_mail = _invoice_hint(subjects) or delivery or bool(links)
    has_document = any(part.suffix in SUPPORTED - IMAGE_SUFFIXES for part, _ in candidates)
    examined = False
    for position, (part, mime) in enumerate(candidates):
        suffix = part.suffix
        if suffix not in SUPPORTED:
            continue
        file_hint = _invoice_hint(part.name)
        hinted = invoice_mail or file_hint
        allow_ocr = deep_scan or hinted
        image_context = delivery or file_hint or bool(links)
        image_ocr = True
        if suffix in IMAGE_SUFFIXES:
            if part.disposition == "inline" and DECORATIVE_IMAGE.fullmatch(Path(part.name).stem) and not file_hint:
                continue
            if not allow_ocr:
                stats["deferred"] += 1
                continue
            image_ocr = not (part.disposition == "inline" and has_document and not file_hint and not deep_scan)
            if not image_ocr and not image_context:
                stats["deferred"] += 1
                continue
        examined = True
        section = part.section or f"forwarded:{position}"
        row = {"file": part.name or "邮件附件" + suffix, **metadata, "mail_part": f"attachment:{section}"}
        data = None
        if _cancelled(cancelled):
            raise CollectionCancelled()
        try:
            if progress:
                progress("正在下载候选附件…")
            name, data = _read_attachment(client, message, part, stats, mime=mime)
            if progress:
                progress("正在识别候选附件…")
            if _screen_file(data, name, row, staging, stats, cache, allow_ocr=allow_ocr, hinted=hinted,
                            image_context=image_context, image_ocr=image_ocr):
                notes.append(row)
        except UserError as exc:
            if (file_hint and delivery and suffix == ".pdf" and data and len(data) <= MAX_BYTES
                    and data.startswith(b"%PDF-")):
                row["mail_review"] = str(exc)
                _stage_mail_file(data, name, row, staging, sha256(data).hexdigest(), hinted=True)
                notes.append(row)
            elif hinted:
                notes.append({**row, "error": str(exc)})
            elif not deep_scan:
                stats["deferred"] += 1
    for link, label in links.items():
        if _cancelled(cancelled):
            raise CollectionCancelled()
        examined = True
        row = {"file": label, **metadata, "mail_part": "link:" + sha256(link.encode()).hexdigest()}
        try:
            if progress:
                progress("正在下载发票链接中的文件…")
            name, data = download_invoice(link, blocked)
            stats["attachment_bytes"] += len(data)
            if progress:
                progress("正在识别链接文件…")
            if _screen_file(data, name, row, staging, stats, cache, allow_ocr=True, hinted=True, image_context=True):
                notes.append(row)
            else:
                notes.append({**row, "mail_review": "链接文件未识别出发票内容，请核对原邮件。"})
        except UserError as exc:
            notes.append({**row, "mail_review": str(exc), "mail_retry": True})
    if examined or delivery:
        stats["candidates"] += 1
    if delivery and not any(row.get("payload") or row.get("mail_review") for row in notes):
        context = next((line.strip()[:300] for text in body_text for line in text.splitlines()
                        if _delivery_hint(line)), "")
        notes.append({"file": "邮件内容", **metadata, "mail_part": "body", "mail_context": context,
                      "mail_review": "发现发票交付提示，未找到可识别的发票附件或链接。"})
    return notes


def collect(mailbox, folder, bounds, blocked, staging, progress, *, deep_scan=False, previous=None,
            checkpoint=None, cancelled=None):
    if _cancelled(cancelled):
        raise CollectionCancelled()
    rows = []
    stats = dict.fromkeys(("scanned", "candidates", "filtered", "deferred", "partial_bodies",
                           "body_bytes", "attachment_bytes", "ocr_files", "reused", "metadata_reads"), 0)
    limit = DEEP_PREVIEW_BYTES if deep_scan else PREVIEW_BYTES
    previous = previous or {}
    checkpoint = checkpoint if checkpoint is not None else {}
    checkpoint["messages"] = {}
    cache = {}
    forwarded_mode = mailbox.forwarded_mode
    try:
        with mailbox._connection(cancelled=cancelled) as client:
            batches = _message_batches(client, mailbox.profile, folder, bounds, 8 if deep_scan else 4,
                                       stats, rows, previous, checkpoint, cancelled=cancelled)
            for batch in batches:
                if _cancelled(cancelled):
                    raise CollectionCancelled()
                if progress:
                    progress(f"正在筛选邮件，已读取 {stats['scanned']} 封邮件头…")
                preview_batch_size = 12 if deep_scan else 50
                for offset in range(0, len(batch), preview_batch_size):
                    if _cancelled(cancelled):
                        raise CollectionCancelled()
                    group = batch[offset:offset + preview_batch_size]
                    previews = _read_previews(client, [message for message, _ in group], limit)
                    for message, more_bodies in group:
                        if _cancelled(cancelled):
                            raise CollectionCancelled()
                        before = stats["partial_bodies"], stats["deferred"]
                        found = _collect_message(client, message, previews.get(message.uid, {}), limit, blocked,
                                                 staging, stats, cache, progress, deep_scan=deep_scan,
                                                 forwarded_mode=forwarded_mode, cancelled=cancelled)
                        rows.extend(found)
                        if not any(row.get("error") or row.get("mail_retry") for row in found):
                            partial = more_bodies or before != (stats["partial_bodies"], stats["deferred"])
                            mode = "default" if partial and not deep_scan else "deep"
                            checkpoint["messages"][str(message.uid)] = message.metadata["received"], mode
    except UserError as exc:
        rows.append({"file": "邮件列表", "error": str(exc)})
    return stats, rows
