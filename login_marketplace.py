"""Mo trinh duyet, dang nhap UpPromote Marketplace, roi mo trang Find Offers."""

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://marketplace.uppromote.com/login"
OFFERS_URL = (
    "https://marketplace.uppromote.com/offers/find-offers"
    "?page=1&per_page=20&tab=all-offers"
)
EMAIL = "agena.academy.edu@gmail.com"
PASSWORD = "Turereview@123"


def login_success_url(url: str) -> bool:
    path = url.split("?", 1)[0]
    return "/login" not in path


def launch_browser(playwright, headless: bool = True):
    """Uu tien Chrome he thong, roi moi dung Chromium cua Playwright."""
    attempts = (
        {"channel": "chrome", "headless": headless},
        {"channel": "msedge", "headless": headless},
        {"headless": headless},
        {"channel": "chrome", "headless": False},
        {"headless": False},
    )
    last_error: Exception | None = None
    for kwargs in attempts:
        try:
            return playwright.chromium.launch(**kwargs)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        "Khong mo duoc trinh duyet. Cai Chrome hoac chay: python -m playwright install chromium"
    ) from last_error


def login_and_get_tokens(headless: bool = True) -> tuple[str, str]:
    """Dang nhap va tra ve (access_token, refresh_token). Khong in token."""
    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headless=headless)
        page = browser.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.get_by_placeholder("Enter your email").wait_for(timeout=30_000)
        page.get_by_placeholder("Enter your email").fill(EMAIL)
        page.get_by_placeholder("Enter your password").fill(PASSWORD)
        login_button = page.get_by_role("button", name="Login")
        if login_button.count() == 0:
            login_button = page.locator("button, [class*='loginButton']").filter(
                has_text="Login"
            )
        login_button.first.click()
        page.wait_for_url(login_success_url, timeout=45_000)
        cookies = {cookie["name"]: cookie["value"] for cookie in page.context.cookies()}
        browser.close()

    access = cookies.get("marketplace_access_token") or ""
    refresh = cookies.get("marketplace_refresh_token") or ""
    if not access or not refresh:
        raise RuntimeError("Khong lay duoc token sau khi dang nhap.")
    return access, refresh


def main() -> None:
    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headless=False)
        context = browser.new_context(no_viewport=True)
        page = context.new_page()

        print("Dang mo trang dang nhap...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.get_by_placeholder("Enter your email").wait_for(timeout=30_000)

        print("Dang dien thong tin dang nhap...")
        email_input = page.get_by_placeholder("Enter your email")
        password_input = page.get_by_placeholder("Enter your password")
        email_input.fill(EMAIL)
        password_input.fill(PASSWORD)

        login_button = page.get_by_role("button", name="Login")
        if login_button.count() == 0:
            login_button = page.locator("button, [class*='loginButton']").filter(has_text="Login")
        login_button.first.click()

        try:
            page.wait_for_url(login_success_url, timeout=45_000)
        except PlaywrightTimeout:
            error = page.locator(".styles_errorInfo__V4LBW, [class*='errorInfo']")
            if error.count() > 0:
                print("Dang nhap that bai:", error.first.inner_text())
            else:
                print("Dang nhap that bai hoac het thoi gian cho.")
            print("Trinh duyet van mo. Nhan Enter de dong.")
            input()
            browser.close()
            return

        print("Dang nhap thanh cong. Dang mo trang Find Offers...")
        page.goto(OFFERS_URL, wait_until="domcontentloaded")
        page.wait_for_url("**/offers/find-offers**", timeout=30_000)
        print("Da mo:", page.url)
        print("Nhan Enter de dong trinh duyet...")
        input()
        browser.close()


if __name__ == "__main__":
    main()
