"""Read a logged-in workbench and write back only verified filing successes.

Selectors are deliberately local configuration: the live authenticated DOM has
not been recorded. No credentials are written to disk, screenshots or logs.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit, urljoin

from core import Company, ValidationError
from runtime_checks import configure_browser_environment

TAX_ID = re.compile(r"[A-Z0-9]{18}")
CHECKS = "✓✔√"
PARTIAL_ZERO_LABEL = "部分0申报（个税0申报，有社保，公积金，增值税，所得税0申报）"


def validate_months(settings):
    for key in ("work_month", "tax_month"):
        if not re.fullmatch(r"[1-9]\d{3}-(0[1-9]|1[0-2])", settings.get(key, "")):
            raise ValidationError(f"请显式设置 {key}（YYYY-MM）")


def filing_status(text: str, pressed: str | None) -> str:
    """Use only observable text/ARIA; never infer status from a CSS class."""
    compact = re.sub(r"\s+", "", text)
    if pressed == "true" or any(mark in compact for mark in CHECKS):
        return "done"
    if compact == "个税" and pressed in (None, "false"):
        return "pending"
    raise ValidationError("个税待办状态不明确")


def parse_rows(rows: list[dict], settings: dict) -> list[Company]:
    """Pure validation boundary.

    A company row only needs a company name. Tax id and the password box are
    not required; a logged-in workbench list can be read directly.
    """
    validate_months(settings)
    month = settings["tax_month"]
    by_tax = settings.get("expected_people_by_tax_id", {})
    by_name = settings.get("expected_people_by_name", {})
    items, seen_names, seen_tax = [], set(), set()
    for row in rows:
        tax_id = (row.get("tax_id") or "").strip().upper()
        if tax_id and not TAX_ID.fullmatch(tax_id):
            raise ValidationError("工作台公司税号格式错误")
        if not row.get("has_individual_tax"):
            if tax_id:
                if tax_id in seen_tax:
                    raise ValidationError("工作台存在重复税号，无法唯一定位公司")
                seen_tax.add(tax_id)
            continue
        name = (row.get("name") or "").strip()
        if not name:
            raise ValidationError("公司名称不能为空")
        if name in seen_names:
            raise ValidationError("工作台存在重复公司名称，无法唯一定位公司")
        seen_names.add(name)
        if tax_id:
            if tax_id in seen_tax:
                raise ValidationError("工作台存在重复税号，无法唯一定位公司")
            seen_tax.add(tax_id)
        status = filing_status(row.get("button_text", ""), row.get("aria_pressed"))
        if status == "done":
            continue
        if name in by_name:
            people = by_name[name]
        elif tax_id and tax_id in by_tax:
            people = by_tax[tax_id]
        else:
            people = settings.get("expected_people", 1)
        company = Company(
            name, tax_id, month, people,
            settings.get("allow_missing_insurance", False),
            settings.get("generate_zero", False),
        )
        company.validate()
        items.append(company)
    return items


def browser_profile_locked(exc):
    """Chrome exits immediately when this app's profile is already open."""
    text = str(exc)
    return type(exc).__name__ == "TargetClosedError" or "TargetClosedError" in text or "exitCode=21" in text


def release_profile_browser(profile):
    """Close only the Chrome that is using this workbench profile."""
    import subprocess
    marker = str(profile).replace("'", "''").lower()
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -and $_.CommandLine.ToLower().Contains('{marker}') }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", command], check=False, capture_output=True)


def browser_network_failure(exc):
    """Proxy and other browser network errors can be retried without the system proxy."""
    text = str(exc)
    if "net::ERR_" in text or "ERR_PROXY" in text:
        return True
    return "Page.goto" in text and type(exc).__name__ in ("TimeoutError", "Error")


class Workbench:
    def __init__(self, settings: dict, base_dir: str | Path, log=None):
        self.settings = settings
        self.base_dir = Path(base_dir)
        self.log = log or (lambda message: None)
        self.selectors = settings.get("selectors", settings.get("web_selectors", {}))
        self.page = self.context = self.playwright = None
        self.stop = lambda: False
        self.partial_list = False

    def _url(self):
        month = self.settings.get("work_month", "")
        validate_months(self.settings)
        parts = urlsplit(self.settings.get("url", ""))
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValidationError("请设置有效的工作台地址 url")
        return urlunsplit((parts.scheme, parts.netloc, "/finance/tax",
                           urlencode({"ym": month, "tab": "pending", "situation": "all_zero"}), ""))

    def open(self):
        # Frozen releases carry the Playwright-matched Chromium with them.
        # Configure this before starting its driver, which reads the environment.
        configure_browser_environment()
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
        target = self._url()
        self.playwright = sync_playwright().start()
        try:
            try:
                self._open_target(target, BrowserTimeout, None)
            except Exception as exc:
                self._close_context()
                if browser_profile_locked(exc):
                    self.log("上次的工作台浏览器还开着，先关掉再重新打开。")
                    release_profile_browser(self.base_dir / "data" / "browser_profile")
                    time.sleep(1)
                    self._open_target(target, BrowserTimeout, None)
                elif browser_network_failure(exc):
                    self.log("网页连接失败，改为不走系统代理再打开一次。")
                    self._open_target(target, BrowserTimeout, ["--no-proxy-server"])
                else:
                    raise
        except Exception:
            self.close()
            raise

    def _close_context(self):
        try:
            if self.context is not None:
                self.context.close()
        finally:
            self.page = self.context = None

    def _open_target(self, target, browser_timeout, args):
        options = {"headless": False}
        if not configure_browser_environment():
            # Keep the existing source/developer launch behavior unchanged.
            options["channel"] = "chrome"
        if args:
            options["args"] = args
        self.context = self.playwright.chromium.launch_persistent_context(
            str(self.base_dir / "data" / "browser_profile"), **options)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(target, wait_until="domcontentloaded", timeout=60000)
        if "/login" in urlsplit(self.page.url).path:
            self.log("请在打开的浏览器中登录工作台；完成后程序自动继续。")
            deadline = time.monotonic() + 300
            while "/login" in urlsplit(self.page.url).path:
                if self.stop():
                    raise ValidationError("用户已停止登录等待")
                if time.monotonic() >= deadline:
                    raise ValidationError("工作台登录超时")
                try:
                    self.page.wait_for_url(lambda url: "/login" not in urlsplit(str(url)).path,
                                           timeout=1000)
                except browser_timeout:
                    pass
            self.page.goto(target, wait_until="domcontentloaded", timeout=60000)
        self._assert_scope()

    def _assert_scope(self, verification=False):
        if self.page is None:
            raise ValidationError("工作台尚未打开")
        parts = urlsplit(self.page.url)
        q = parse_qs(parts.query)
        target = urlsplit(self._url())
        if (parts.netloc != target.netloc or parts.scheme != target.scheme or
                parts.path != target.path or q.get("ym") != [self.settings["work_month"]] or
                (not verification and q.get("tab") != ["pending"]) or
                (not verification and not self.partial_list and q.get("situation") != ["all_zero"])):
            raise ValidationError("工作台不在指定月份的待办零申报页面，禁止继续")

    def _rows(self, verification=False):
        self._assert_scope(verification)
        table = self.page.locator(self.selectors.get("table", "table"))
        if table.count() != 1:
            raise ValidationError("公司表格必须唯一，请配置 selectors.table")
        table.wait_for(state="visible")
        return table, table.locator(self.selectors.get("row", "tbody tr"))

    def _check_pagination(self):
        # No guessed pagination traversal: every enabled next-page control is
        # a completeness problem until its actual DOM has been configured.
        explicit = self.selectors.get("pagination")
        candidates = ([self.page.locator(explicit)] if explicit else [
            self.page.get_by_role("link", name=re.compile(r"^(下一页|下页|Next|Next page|›|»)$", re.I)),
            self.page.get_by_role("button", name=re.compile(r"^(下一页|下页|Next|Next page|›|»)$", re.I)),
            self.page.locator('a[rel="next"]'),
        ])
        for locator in candidates:
            for index in range(locator.count()):
                node = locator.nth(index)
                if node.is_visible() and node.is_enabled() and node.get_attribute("aria-disabled") != "true":
                    raise ValidationError("检测到分页，尚未配置并验证遍历策略；请将工作台切换为全部显示后继续")

    def _verification_url(self):
        pending = urlsplit(self._url())
        def valid(value):
            parts = urlsplit(value)
            query = parse_qs(parts.query)
            return (parts.scheme == pending.scheme and parts.netloc == pending.netloc and
                    parts.path == pending.path and query.get("ym") == [self.settings["work_month"]] and
                    query.get("tab") != ["pending"])
        configured = self.settings.get("verify_url", "")
        if configured:
            value = urljoin(self.page.url, configured)
            if not valid(value):
                raise ValidationError("verify_url须是同站点同工作月份的非待办公司列表")
            return value
        links = self.page.get_by_role("link", name=re.compile(r"^(全部|全部公司|全部记录|已完成|已申报)$"))
        found = set()
        for index in range(links.count()):
            link = links.nth(index)
            href = link.get_attribute("href")
            if href and link.is_visible():
                value = urljoin(self.page.url, href)
                if valid(value):
                    found.add(value)
        if len(found) != 1:
            raise ValidationError("不能唯一发现完成核验列表，请配置 verify_url；未进行工作台回填")
        return found.pop()

    def _optional_tax_id(self, row):
        """Tax id is not required on a company row. Read it only when configured."""
        selector = self.selectors.get("tax_id_input")
        if not selector:
            return ""
        fields = row.locator(selector)
        if fields.count() == 0:
            return ""
        if fields.count() != 1:
            raise ValidationError("公司行税号输入框不唯一")
        value = fields.input_value().strip().upper()
        if not value:
            return ""
        if not TAX_ID.fullmatch(value):
            raise ValidationError("公司行税号须为18位大写字母或数字")
        return value

    @staticmethod
    def _cell_value(cell):
        text = cell.inner_text().strip()
        if text:
            return text
        fields = cell.locator("input:not([type=password]), textarea")
        values = [fields.nth(i).input_value().strip() for i in range(fields.count())]
        values = [value for value in values if value]
        if len(values) > 1:
            raise ValidationError("公司名称单元格存在多个输入值")
        return values[0] if values else ""

    def _name_getter(self, table):
        name_selector = self.selectors.get("name_cell")
        if name_selector:
            def read(row):
                cell = row.locator(name_selector)
                if cell.count() != 1:
                    raise ValidationError("公司名称单元格不唯一或不存在")
                return self._cell_value(cell)
            return read
        headers = table.locator("thead th")
        matches = [i for i in range(headers.count())
                   if headers.nth(i).inner_text().strip() in ("公司", "公司名称")]
        if len(matches) != 1:
            raise ValidationError("无法唯一找到公司名称表头，请配置 selectors.name_cell")
        index = matches[0]

        def read(row):
            cell = row.locator("td").nth(index)
            if cell.count() != 1:
                raise ValidationError("公司名称单元格不唯一或不存在")
            return self._cell_value(cell)
        return read

    def _tax_button(self, row):
        selector = self.selectors.get("individual_tax_button")
        if selector:
            found = row.locator(selector)
        else:
            # Include unfamiliar individual-tax labels so validation rejects
            # ambiguous state rather than silently treating the row as absent.
            found = row.get_by_role("button", name=re.compile(r"个税"))
        if found.count() > 1:
            raise ValidationError("公司行存在多个个税按钮")
        return found

    def _password_field(self, row):
        field = row.locator(self.selectors.get("password_input", "input[type=password]"))
        if field.count() != 1:
            raise ValidationError("公司行的密码输入框不唯一或不存在")
        if field.get_attribute("type") != "password":
            raise ValidationError("密码控件必须为 password 类型，程序不会切换显示密码")
        return field

    def select_partial_zero(self):
        """Open the partial-zero filter. Individual tax is still a zero return."""
        if self.page is None:
            raise ValidationError("工作台尚未打开")
        selects = self.page.locator("select")
        clicked = False
        for index in range(selects.count()):
            select = selects.nth(index)
            options = select.locator("option")
            for option_index in range(options.count()):
                option = options.nth(option_index)
                text = re.sub(r"\s+", "", option.inner_text() or "")
                if "部分0申报" in text and "个税0申报" in text and "有社保" in text:
                    value = option.get_attribute("value")
                    if value is None:
                        continue
                    select.select_option(value=value)
                    clicked = True
                    break
            if clicked:
                break
        if not clicked:
            choice = self.page.get_by_text(PARTIAL_ZERO_LABEL, exact=False)
            if choice.count() == 0:
                raise ValidationError("工作台没有找到部分0申报选项")
            choice.first.click()
        self.page.wait_for_load_state("domcontentloaded")
        self.partial_list = True
        self.log("已切换到部分0申报，按顺序办理个税")

    def read_companies(self):
        table, rows = self._rows()
        self._check_pagination()
        read_name = self._name_getter(table)
        snapshots = []
        for i in range(rows.count()):
            row = rows.nth(i)
            button = self._tax_button(row)
            tax_id = self._optional_tax_id(row)
            if button.count() == 0:
                snapshots.append({"has_individual_tax": False, "tax_id": tax_id})
                continue
            snapshots.append({"has_individual_tax": True, "name": read_name(row),
                              "tax_id": tax_id, "button_text": button.inner_text(),
                              "aria_pressed": button.get_attribute("aria-pressed")})
        return parse_rows(snapshots, self.settings)

    def _find_row(self, name, verification=False):
        table, rows = self._rows(verification)
        self._check_pagination()
        read_name = self._name_getter(table)
        wanted = name.strip()
        matches = []
        for i in range(rows.count()):
            row = rows.nth(i)
            if self._tax_button(row).count() and read_name(row).strip() == wanted:
                matches.append(row)
        if len(matches) != 1:
            raise ValidationError("无法按公司名称唯一定位工作台公司")
        return matches[0]

    def get_password(self, name):
        value = self._password_field(self._find_row(name)).input_value()
        if not value:
            raise ValidationError("工作台密码为空")
        return value

    def _wait_refresh(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            self.page.wait_for_timeout(1200)

    def mark_success(self, company):
        """Click 个税 on the open list, then wait for that page to refresh."""
        if self.page is None or company.month != self.settings["tax_month"]:
            return False
        try:
            row = self._find_row(company.name)
            button = self._tax_button(row)
            if button.count() != 1:
                self.log("工作台没有找到该公司的个税按钮")
                return False
            try:
                status = filing_status(button.inner_text(), button.get_attribute("aria-pressed"))
            except ValidationError:
                status = "pending"
            if status == "done":
                self.log(f"工作台个税已勾选：{company.name}")
            else:
                self.log(f"点击工作台个税：{company.name}")
                button.click()
            self._wait_refresh()
            self.log("工作台已刷新，继续下一家")
            return True
        except Exception:
            self.log("工作台个税点击未完成，继续下一家前请看一眼页面。")
            return False

    def close(self):
        try:
            if self.context is not None:
                self.context.close()
        finally:
            if self.playwright is not None:
                self.playwright.stop()
            self.page = self.context = self.playwright = None
