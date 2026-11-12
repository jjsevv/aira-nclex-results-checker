"""
NCLEX Quick Results watcher.
Logs in to Pearson VUE, checks the Quick Results page every few minutes,
and sends a Telegram message when results become available.
"""
import datetime
import os
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

USERNAME = os.environ["PEARSON_USERNAME"]
PASSWORD = os.environ["PEARSON_PASSWORD"]
import re

# Clean up common copy-paste mistakes: spaces, line breaks, "bot" prefix, full URLs.
_raw_token = "".join(os.environ["TELEGRAM_BOT_TOKEN"].split())
_m = re.search(r"\d+:[A-Za-z0-9_-]+", _raw_token)
TG_TOKEN = _m.group(0) if _m else _raw_token
TG_CHAT = "".join(os.environ["TELEGRAM_CHAT_ID"].split())

RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "330"))          # how long one run keeps watching
CHECK_EVERY = int(os.environ.get("CHECK_EVERY_SECONDS", "240"))  # 4 minutes between checks
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

BASE = "https://wsr.pearsonvue.com"
SIGNIN_URL = BASE + "/testtaker/signin/SignInPage/NCLEXTESTING"
RESULTS_URL = BASE + "/testtaker/registration/ViewQuickResults/NCLEXTESTING"
MANILA = datetime.timezone(datetime.timedelta(hours=8))


def now():
    return datetime.datetime.now(MANILA).strftime("%a %d %b, %I:%M %p")


def telegram(msg):
    data = urllib.parse.urlencode(
        {"chat_id": TG_CHAT, "text": msg, "disable_web_page_preview": "true"}
    ).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data, timeout=20
        )
    except Exception as e:  # never print the token
        print("Telegram send failed:", type(e).__name__)


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


def on_signin_page(page):
    return "signin" in page.url.lower() or page.locator("#inputPassword").count() > 0


def dismiss_cookie_banner(page):
    """Close the OneTrust cookie popup (Reject All), then remove it if it is still there."""
    for sel in ("#onetrust-reject-all-handler", "button.ot-pc-refuse-all-handler",
                "#onetrust-accept-btn-handler"):
        try:
            btn = page.locator(sel)
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=5000)
                time.sleep(1)
                break
        except Exception:
            pass
    page.evaluate(
        "() => { const e = document.getElementById('onetrust-consent-sdk'); if (e) e.remove(); }"
    )


def login(page):
    page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)  # give the cookie popup time to appear
    dismiss_cookie_banner(page)
    page.fill("#inputUserName", USERNAME)
    page.fill("#inputPassword", PASSWORD)
    try:
        page.click("#submitButton", timeout=10000)
    except Exception:
        dismiss_cookie_banner(page)
        page.press("#inputPassword", "Enter")
    page.wait_for_load_state("domcontentloaded", timeout=60000)
    time.sleep(4)
    return not on_signin_page(page)


def open_results(page):
    """Returns the page text, or None if Pearson sent us back to sign-in."""
    page.goto(RESULTS_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    if on_signin_page(page):
        return None
    dismiss_cookie_banner(page)
    body = page.inner_text("body")
    if "quick results" not in body.lower():
        link = page.locator("a[href*='ViewQuickResults']")
        if link.count() > 0:
            link.first.click()
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            time.sleep(3)
            body = page.inner_text("body")
    return body


def classify(text):
    t = text.lower()
    if "results not available" in t or "not available at this time" in t:
        return "NOT_READY"
    if "quick results" in t:
        return "READY"
    return "UNKNOWN"


LABELS = {
    "NOT_READY": "Results not available yet",
    "READY": "AVAILABLE",
    "UNKNOWN": "Could not read the status",
}


def login_failed():
    telegram(
        f"⚠️ NCLEX watcher: Pearson login FAILED ({now()}).\n"
        "The watcher has stopped itself so your account does not get locked.\n"
        "Check the PEARSON_USERNAME and PEARSON_PASSWORD secrets, then re-enable the workflow."
    )
    set_output("result", "login_failed")


def main():
    deadline = time.time() + RUN_MINUTES * 60
    first_check = True
    unknown_alerted = False
    errors_in_a_row = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(locale="en-US").new_page()

        try:
            ok = login(page)
        except Exception as e:
            print(now(), "sign-in page error:", type(e).__name__)
            telegram(
                f"⚠️ NCLEX watcher could not load the Pearson sign-in page ({now()}). "
                "It will try again on the next run."
            )
            set_output("result", "run_finished")
            return
        if not ok:
            login_failed()
            return

        while time.time() < deadline:
            try:
                text = open_results(page)
                if text is None:  # session expired, log in again
                    if not login(page):
                        login_failed()
                        return
                    text = open_results(page) or ""
                status = classify(text)
                errors_in_a_row = 0
                print(now(), status)

                if first_check:
                    first_check = False
                    prefix = "🧪 TEST: " if TEST_MODE else "👀 "
                    telegram(
                        f"{prefix}NCLEX watcher is running ({now()}).\n"
                        f"Current status: {LABELS[status]}.\n"
                        f"Checking every {CHECK_EVERY // 60} minutes."
                    )

                if status == "READY":
                    for _ in range(3):
                        telegram(
                            "🎉 NCLEX QUICK RESULTS ARE AVAILABLE!\n"
                            f"Detected {now()}.\n"
                            f"Log in and buy them here: {SIGNIN_URL}"
                        )
                        time.sleep(60)
                    set_output("result", "ready")
                    return

                if status == "UNKNOWN" and not unknown_alerted:
                    unknown_alerted = True
                    telegram(
                        f"❓ NCLEX watcher: the Quick Results page looked different ({now()}).\n"
                        f"Please check it yourself: {SIGNIN_URL}"
                    )

                if TEST_MODE:
                    set_output("result", "test_done")
                    return

            except Exception as e:
                errors_in_a_row += 1
                print(now(), "check error:", type(e).__name__)
                if errors_in_a_row == 5:
                    telegram(
                        f"⚠️ NCLEX watcher: 5 failed checks in a row ({now()}). "
                        "It will keep trying. If this repeats, check the Actions log on GitHub."
                    )

            time.sleep(CHECK_EVERY)

        set_output("result", "run_finished")


if __name__ == "__main__":
    main()
