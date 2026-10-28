#!/usr/bin/env python3
"""
SmartThings PAT Rotator for samsung_familyhub_fridge
Generates a fresh PAT via Playwright and pushes it to HA via REST API.

Usage:
  python pat_rotator.py           # normal headless run
  python pat_rotator.py --debug   # non-headless, for initial setup / selector troubleshooting
  python pat_rotator.py --clear-state  # delete saved browser state and re-login
"""

import asyncio
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Configuration — all overridable via environment variables
# ---------------------------------------------------------------------------
SAMSUNG_EMAIL    = os.environ["SAMSUNG_EMAIL"]
SAMSUNG_PASSWORD = os.environ["SAMSUNG_PASSWORD"]
SAMSUNG_TOTP_SECRET = os.environ.get("SAMSUNG_TOTP_SECRET", "")  # TOTP base32 secret, optional

HA_URL          = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN        = os.environ["HA_TOKEN"]          # HA long-lived access token
HA_ENTITY_ID    = os.environ.get("HA_ENTITY_ID", "input_text.smartthings_pat")

# Where to persist browser cookies/localStorage between runs
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/browser_state.json"))

PAT_PAGE_URL = "https://account.smartthings.com/tokens"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pat_rotator")

# net::ERR_NETWORK_CHANGED (interface/route changed mid-request) is a
# transient OS-level network condition, not a page/selector problem -
# retrying the same navigation almost always succeeds a few seconds later.
NETWORK_CHANGED_RETRY_ATTEMPTS = 3
NETWORK_CHANGED_RETRY_DELAY_SECONDS = 5


async def goto_with_retry(page, url: str, **kwargs) -> None:
    """page.goto() that retries on a transient network-changed error.

    A network change mid-redirect (e.g. SmartThings -> Samsung's OAuth
    authorize URL) doesn't always make page.goto() itself raise - Playwright
    can let the navigation "complete" by landing on Chrome's own internal
    chrome-error://chromewebdata/ page instead. Confirmed directly: a run
    logged "Landed at: chrome-error://chromewebdata/" immediately *after*
    this function returned normally, with no exception ever thrown, even
    though the browser-level log showed ERR_NETWORK_CHANGED on that exact
    redirect hop moments earlier. So both a thrown exception AND a
    chrome-error:// landing need to trigger a retry - checking the
    exception alone missed this case entirely.
    """
    for attempt in range(1, NETWORK_CHANGED_RETRY_ATTEMPTS + 1):
        last_attempt = attempt == NETWORK_CHANGED_RETRY_ATTEMPTS
        try:
            await page.goto(url, **kwargs)
        except Exception as e:
            if "ERR_NETWORK_CHANGED" not in str(e) or last_attempt:
                raise
            log.warning(
                "Network changed navigating to %s (attempt %s/%s) - retrying in %ss",
                url, attempt, NETWORK_CHANGED_RETRY_ATTEMPTS, NETWORK_CHANGED_RETRY_DELAY_SECONDS,
            )
            await asyncio.sleep(NETWORK_CHANGED_RETRY_DELAY_SECONDS)
            continue

        if page.url.startswith("chrome-error://"):
            if last_attempt:
                raise RuntimeError(
                    f"Navigation to {url} landed on a browser error page "
                    f"({page.url}) after {NETWORK_CHANGED_RETRY_ATTEMPTS} attempts"
                )
            log.warning(
                "Landed on a browser error page (%s) navigating to %s "
                "(attempt %s/%s) - retrying in %ss",
                page.url, url, attempt, NETWORK_CHANGED_RETRY_ATTEMPTS, NETWORK_CHANGED_RETRY_DELAY_SECONDS,
            )
            await asyncio.sleep(NETWORK_CHANGED_RETRY_DELAY_SECONDS)
            continue

        return


# ---------------------------------------------------------------------------
# Login helpers
# ---------------------------------------------------------------------------
async def do_login(page) -> None:
    """Handle Samsung SSO login. Called only when the tokens page requires auth."""
    log.info("Login required — starting Samsung SSO flow")

    # Samsung redirects to account.samsung.com for auth
    # Wait for the email input — Samsung uses #account, #id or input[name="loginId"]
    for sel in ["#account", "#id", "input[name='loginId']", "input[type='email']"]:
        try:
            await page.wait_for_selector(sel, timeout=8_000)
            email_field = await page.query_selector(sel)
            if email_field:
                await email_field.fill(SAMSUNG_EMAIL)
                log.info(f"Filled email using selector: {sel}")
                break
        except PWTimeout:
            continue
    else:
        raise RuntimeError("Could not find email input — check page structure at " + page.url)

    # Click the initial "Next" / "Sign In" button
    for btn_sel in ["#btnNext", "button:has-text('Next')", "button[type='submit']", ".btn-login", "button:text('Sign in')"]:
        btn = await page.query_selector(btn_sel)
        if btn and await btn.is_visible():
            await btn.click()
            log.info(f"Clicked next/signin using selector: {btn_sel}")
            break

    await page.wait_for_timeout(2_000)

    # Password field
    for sel in ["#password", "#pw", "input[name='password']", "input[type='password']"]:
        try:
            await page.wait_for_selector(sel, timeout=8_000)
            pw_field = await page.query_selector(sel)
            if pw_field and await pw_field.is_visible():
                await pw_field.fill(SAMSUNG_PASSWORD)
                await pw_field.press("Enter")
                log.info(f"Filled password using selector: {sel}")
                break
        except PWTimeout:
            continue
    else:
        raise RuntimeError("Could not find password input")

    await page.wait_for_timeout(3_000)

    # TOTP / 2FA (optional)
    if SAMSUNG_TOTP_SECRET:
        await _handle_totp(page)

    # Wait until we land back on account.smartthings.com or hits password notice
    await page.wait_for_timeout(3_000)
    current = page.url
    if "change-password" in current:
        log.info("Detected 'Change password regularly' notice page")
        for sel in ["button:has-text('Not now')", "a:has-text('Not now')", "button:text('Not now')", ".btn-notnow"]:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    log.info(f"Clicked 'Not now' button using selector: {sel}")
                    break
            except Exception as e:
                log.debug(f"Failed checking 'Not now' selector {sel}: {e}")
        await page.wait_for_timeout(3_000)

    try:
        await page.wait_for_url("*account.smartthings.com*", timeout=20_000)
        log.info("Login successful — back on account.smartthings.com")
    except PWTimeout:
        # Some flows redirect differently; check current URL manually
        current = page.url
        if "change-password" in current:
            log.warning("Still on change-password page, trying another click on 'Not now'")
            for sel in ["button:has-text('Not now')", "a:has-text('Not now')", "button:text('Not now')"]:
                try:
                    btn = await page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(5_000)
                        break
                except Exception:
                    pass
        current = page.url
        if "smartthings.com" not in current:
            raise RuntimeError(f"Login may have failed — unexpected URL: {current}")


async def _handle_totp(page) -> None:
    """Enter a TOTP code if a 2FA prompt is detected."""
    selectors = [
        "#otp",
        "input[name='otp']",
        "input[name='mfaCode']",
        "input[name='verificationCode']",
        "input[placeholder*='code' i]",
        ".otp-input"
    ]
    
    log.info("Checking for 2FA / TOTP prompt...")
    combined_selector = ", ".join(selectors)
    active_sel = None
    
    try:
        # Wait up to 10 seconds for any 2FA field to appear
        await page.wait_for_selector(combined_selector, timeout=10_000, state="visible")
        for sel in selectors:
            field = await page.query_selector(sel)
            if field and await field.is_visible():
                active_sel = sel
                break
    except PWTimeout:
        log.info("No visible 2FA prompt appeared within timeout — skipping TOTP")
        return

    if active_sel:
        try:
            import pyotp  # only import if actually used
            code = pyotp.TOTP(SAMSUNG_TOTP_SECRET).now()
            field = await page.query_selector(active_sel)
            await field.fill(code)
            log.info(f"Entered TOTP code {code} into {active_sel}")
            await field.press("Enter")
            await page.wait_for_timeout(3_000)
        except Exception as e:
            log.warning(f"Failed to enter TOTP code: {e}")


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------
async def generate_new_pat(page) -> tuple[str, str]:
    """Click 'Generate new token', fill in the form, and return the raw token string and its label."""

    # --- Find the Generate button ---
    # The tokens page can still be rendering its token list (a loading
    # spinner) when we arrive - page.goto()'s "networkidle" only means no
    # network activity, not that the SPA has finished its own client-side
    # render. 5s per selector wasn't enough on a slow render; 15s costs
    # nothing against a 20h rotation cadence.
    gen_btn = None
    for sel in [
        "button:has-text('Generate new token')",
        "a:has-text('Generate new token')",
        "[data-testid='create-token-button']",
        "button:has-text('New token')",
    ]:
        try:
            await page.wait_for_selector(sel, timeout=15_000)
            gen_btn = page.locator(sel).first
            if await gen_btn.is_visible():
                break
        except PWTimeout:
            continue

    if gen_btn is None:
        # Fallback: screenshot for debugging
        screenshot_path = str(STATE_FILE.parent / "debug_tokens_page.png")
        await page.screenshot(path=screenshot_path)
        raise RuntimeError(
            "Could not find 'Generate new token' button. "
            f"Screenshot saved to {screenshot_path}"
        )

    await gen_btn.click()
    log.info("Clicked 'Generate new token'")
    
    # Wait for the scopes checkboxes to render dynamically
    try:
        log.info("Waiting for scope checkboxes to load...")
        await page.wait_for_selector("input[type='checkbox']", timeout=10_000)
    except Exception as e:
        log.warning(f"Timeout waiting for checkboxes to appear: {e}")
        try:
            body_html = await page.content()
            log.info(f"Page HTML content (first 3000 chars): {body_html[:3000]}")
            inputs = await page.query_selector_all("input")
            log.info(f"Page has {len(inputs)} input elements:")
            for idx, inp in enumerate(inputs):
                typ = await inp.get_attribute("type")
                name = await inp.get_attribute("name")
                val = await inp.get_attribute("value")
                log.info(f"  [{idx}] type={typ} name={name} value={val}")
        except Exception as html_err:
            log.warning(f"Failed to dump page HTML: {html_err}")

    # --- Token name ---
    name_input = None
    for sel in [
        "input[placeholder*='token name' i]",
        "input[placeholder*='name' i]",
        "input[aria-label*='name' i]",
        "input[type='text']",
    ]:
        name_input = await page.query_selector(sel)
        if name_input and await name_input.is_visible():
            break

    if name_input is None:
        screenshot_path = str(STATE_FILE.parent / "debug_gen_form.png")
        await page.screenshot(path=screenshot_path)
        raise RuntimeError(f"Could not find token name input. Screenshot saved to {screenshot_path}")

    pat_label = f"HA-Fridge-{datetime.now().strftime('%Y%m%d%H%M')}"
    await name_input.fill(pat_label)
    log.info(f"Named token: {pat_label}")

    # --- Scopes: try "Select All", else check all boxes ---
    selected_all = False
    for sel in [
        "button:has-text('Select All')",
        "button:has-text('Select all')",
        "a:has-text('Select All')",
        "[data-testid='select-all']",
    ]:
        btn = await page.query_selector(sel)
        if btn and await btn.is_visible():
            await btn.click()
            log.info("Clicked 'Select All' scopes")
            selected_all = True
            break

    if not selected_all:
        cbs = await page.query_selector_all("input[type='checkbox']")
        checked = 0
        for cb in cbs:
            if not await cb.is_checked():
                await cb.check()
                checked += 1
        log.info(f"Manually checked {checked} scope checkboxes")

    await page.wait_for_timeout(500)

    # --- Submit ---
    for sel in [
        "button:has-text('Generate Token')",
        "button:has-text('Generate token')",
        "button:has-text('Generate')",
        "button[type='submit']",
    ]:
        submit = await page.query_selector(sel)
        if submit and await submit.is_visible():
            await submit.click()
            log.info(f"Submitted form via: {sel}")
            break

    await page.wait_for_timeout(3_000)

    # --- Capture token value ---
    # SmartThings shows the token once immediately after generation.
    # It's typically in a read-only input, a code block, or a modal.
    token = await _scrape_token_value(page)
    if not token:
        screenshot_path = str(STATE_FILE.parent / "debug_token_result.png")
        await page.screenshot(path=screenshot_path)
        raise RuntimeError(
            "Could not scrape token value from result page. "
            f"Screenshot saved to {screenshot_path}"
        )

    log.info(f"Successfully captured PAT: {token[:8]}…")
    return token, pat_label


async def _scrape_token_value(page) -> str | None:
    """Try multiple strategies to pull the newly created token value from the DOM."""
    uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'

    # Strategy 1: Playwright text regex locator (handles Shadow DOM automatically)
    try:
        # Find any element whose text contains a UUID
        locator = page.locator(f"text=/\\b{uuid_pattern}\\b/i")
        count = await locator.count()
        for i in range(count):
            text = (await locator.nth(i).text_content() or "").strip()
            match = re.search(uuid_pattern, text, re.IGNORECASE)
            if match:
                val = match.group(0)
                if _looks_like_pat(val):
                    return val
    except Exception as ex:
        log.debug(f"Strategy 1 (Playwright text regex) failed: {ex}")

    # Strategy 2: read-only input fields (most common pattern for "copy this token" UIs)
    for sel in [
        "input[readonly]",
        "input[type='text'][readonly]",
        ".token-value input",
        "[data-testid='token-value']",
        ".copy-token",
    ]:
        try:
            els = await page.query_selector_all(sel)
            for el in els:
                tag = await el.evaluate("e => e.tagName")
                val = await el.input_value() if tag == "INPUT" else await el.inner_text()
                val = val.strip()
                match = re.search(uuid_pattern, val, re.IGNORECASE)
                if match and _looks_like_pat(match.group(0)):
                    return match.group(0)
        except Exception:
            continue

    # Strategy 3: Check all divs, spans, paragraphs, code, pre, inputs in Shadow DOM
    try:
        locators = page.locator("div, span, p, code, pre, input, td")
        count = await locators.count()
        for i in range(count):
            loc = locators.nth(i)
            try:
                tag = await loc.evaluate("e => e.tagName")
                text = await loc.input_value() if tag == "INPUT" else await loc.text_content()
                if text:
                    text = text.strip()
                    match = re.search(uuid_pattern, text, re.IGNORECASE)
                    if match and _looks_like_pat(match.group(0)):
                        return match.group(0)
            except Exception:
                continue
    except Exception as ex:
        log.debug(f"Strategy 3 failed: {ex}")

    # Strategy 4: regex scan over visible text content of the whole page (fallback)
    try:
        content = await page.evaluate("() => document.body.innerText")
        uuid_re = re.compile(
            r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',
            re.IGNORECASE,
        )
        matches = uuid_re.findall(content)
        if matches:
            return matches[-1]
    except Exception:
        pass

    return None


def _looks_like_pat(s: str) -> bool:
    """Return True if s looks like a SmartThings PAT (UUID4 format)."""
    return bool(re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        s.strip(),
        re.IGNORECASE,
    ))


async def delete_old_pats(page, keep_token_name: str) -> None:
    """Find and delete all tokens starting with 'HA-Fridge' or 'HA Fridge' except keep_token_name."""
    log.info("Starting cleanup of old PATs...")
    try:
        # Navigate back to the token list page
        log.info(f"Navigating to tokens page to clean up: {PAT_PAGE_URL}")
        await page.goto(PAT_PAGE_URL, wait_until="networkidle", timeout=20_000)

        # Loop until no other HA-Fridge tokens are found
        failed_token_names = set()
        attempts_count = 0
        while True:
            if attempts_count >= 10:
                log.info("Reached maximum deletion attempts limit of 10. Breaking loop to avoid getting stuck.")
                break

            # Look for any elements containing "HA-Fridge" or "HA Fridge" text
            elements = page.locator("text=/HA[- ]Fridge/")
            count = await elements.count()
            if count == 0:
                log.info("No tokens starting with 'HA-Fridge' or 'HA Fridge' found on page.")
                break

            target_locator = None
            target_name = None

            for i in range(count):
                el = elements.nth(i)
                text = (await el.text_content() or "").strip()
                clean_text = re.sub(r'\s*--\s*$', '', text).strip()
                
                # Check if it matches an old token and has NOT already failed to delete in this run
                if (text.startswith("HA-Fridge-") or text.startswith("HA Fridge ")) and text != keep_token_name and clean_text != keep_token_name and text not in failed_token_names:
                    target_locator = el
                    target_name = text
                    break

            if not target_locator:
                log.info("All remaining 'HA-Fridge' / 'HA Fridge' tokens are current or failed to delete.")
                break

            log.info(f"Identified old token to delete: '{target_name}'")

            # Look up parent nodes to find a delete button
            delete_btn = None
            parent = target_locator
            for level in range(5):
                parent = parent.locator("..")
                # Locate any buttons or links inside the ancestor row/container
                buttons = await parent.locator("button, a, [role='button']").all()
                for btn in buttons:
                    if not await btn.is_visible():
                        continue
                    btn_text = (await btn.text_content() or "").lower()
                    aria_label = (await btn.get_attribute("aria-label") or "").lower()
                    title = (await btn.get_attribute("title") or "").lower()
                    test_id = (await btn.get_attribute("data-testid") or "").lower()
                    btn_class = (await btn.get_attribute("class") or "").lower()

                    if (
                        any(x in btn_text for x in ["delete", "remove", "revoke", "destroy", "close", "trash"]) or
                        any(x in aria_label for x in ["delete", "remove", "revoke", "trash"]) or
                        any(x in title for x in ["delete", "remove", "revoke", "trash"]) or
                        any(x in test_id for x in ["delete", "remove", "revoke", "trash"]) or
                        any(x in btn_class for x in ["delete", "remove", "revoke", "trash"])
                    ):
                        delete_btn = btn
                        break
                if delete_btn:
                    break

            if delete_btn:
                log.info(f"Clicking delete button for old token '{target_name}'")
                await delete_btn.click()
                await page.wait_for_timeout(1_500)

                # Look for a DOM-based confirm modal button
                modal_selectors = [
                    "[role='dialog'] button:has-text('Delete')",
                    "[role='alertdialog'] button:has-text('Delete')",
                    ".modal button:has-text('Delete')",
                    ".modal-content button:has-text('Delete')",
                    ".dialog button:has-text('Delete')",
                    "div[class*='modal' i] button:has-text('Delete')",
                    "div[class*='dialog' i] button:has-text('Delete')",
                    "div[class*='popup' i] button:has-text('Delete')",
                    "button:has-text('Delete')", # fallback
                    "button:has-text('Confirm')",
                    "button:has-text('Yes')",
                    "button:has-text('Remove')",
                    "button:has-text('Revoke')",
                    "[data-testid*='confirm']",
                    "[data-testid*='delete']",
                ]
                
                clicked_modal = False
                for modal_sel in modal_selectors:
                    try:
                        modal_btn = page.locator(modal_sel)
                        if await modal_btn.count() > 0:
                            for j in range(await modal_btn.count()):
                                m_btn = modal_btn.nth(j)
                                if await m_btn.is_visible():
                                    log.info(f"Clicking modal confirm button using selector: {modal_sel}")
                                    await m_btn.click()
                                    clicked_modal = True
                                    break
                        if clicked_modal:
                            break
                    except Exception as modal_ex:
                        log.debug(f"Modal check failed for {modal_sel}: {modal_ex}")

                if clicked_modal:
                    # Wait for the deleted token's text element to disappear from the DOM
                    try:
                        log.info(f"Waiting for token '{target_name}' to be deleted from list...")
                        await target_locator.wait_for(state="detached", timeout=10_000)
                        log.info(f"Token '{target_name}' successfully deleted and removed from DOM.")
                    except Exception as wait_ex:
                        log.warning(f"Timeout waiting for token '{target_name}' to disappear: {wait_ex}")
                        log.info("Marking token as failed to delete in this run to avoid infinite loop.")
                        failed_token_names.add(target_name)
                        log.info("Reloading page to refresh the list...")
                        await page.reload(wait_until="networkidle")
                else:
                    log.warning(f"Could not confirm modal for old token '{target_name}'")
                    break
                
                attempts_count += 1
            else:
                log.warning(f"Could not find delete button for old token '{target_name}'")
                break

    except Exception as e:
        log.warning(f"Failed to complete PAT cleanup: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Main browser flow
# ---------------------------------------------------------------------------
async def run_browser(debug: bool = False) -> str:
    async with async_playwright() as pw:
        use_headless = not debug and os.environ.get("USE_HEADED") != "true"
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if use_headless:
            launch_headless = False
            launch_args.append("--headless=new")
        else:
            launch_headless = False

        browser = await pw.chromium.launch(
            headless=launch_headless,
            args=launch_args
        )

        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "extra_http_headers": {
                "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Linux"',
            }
        }
        if STATE_FILE.exists():
            log.info(f"Loading saved browser state from {STATE_FILE}")
            ctx_kwargs["storage_state"] = str(STATE_FILE)

        context = await browser.new_context(**ctx_kwargs)

        # Apply stealth overrides to bypass headless bot detection
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)

        page = await context.new_page()

        # Log browser console and network failures
        page.on("console", lambda msg: log.info(f"BROWSER CONSOLE: {msg.text}"))
        page.on("pageerror", lambda err: log.error(f"BROWSER ERROR: {err}"))
        page.on("requestfailed", lambda req: log.warning(f"BROWSER REQ FAILED: {req.url} - {req.failure if req.failure else 'unknown'}"))
        async def handle_response(resp):
            try:
                log.info(f"BROWSER RESP: {resp.status} {resp.url}")
                if "token-options" in resp.url or "tokens" in resp.url:
                    text = await resp.text()
                    log.info(f"API BODY [{resp.url}]: {text[:2000]}")
            except Exception as e:
                log.debug(f"Failed to read response body for {resp.url}: {e}")
        page.on("response", lambda resp: asyncio.create_task(handle_response(resp)))

        # Register dialog listener to auto-accept confirmation dialogs
        async def handle_dialog(dialog):
            log.info(f"Dialog appeared: '{dialog.message}' ({dialog.type}) - accepting")
            await dialog.accept()
        page.on("dialog", lambda d: asyncio.create_task(handle_dialog(d)))

        try:
            log.info(f"Navigating to {PAT_PAGE_URL}")
            await goto_with_retry(page, PAT_PAGE_URL, wait_until="networkidle", timeout=30_000)
            log.info(f"Landed at: {page.url}")

            # If we were redirected to a login page, authenticate first
            if "account.smartthings.com/tokens" not in page.url:
                await do_login(page)
                await goto_with_retry(page, PAT_PAGE_URL, wait_until="networkidle", timeout=20_000)

            token, pat_label = await generate_new_pat(page)

            # Clean up old tokens before saving state (skipped as they naturally expire/disappear after 24 hours)
            # try:
            #     await delete_old_pats(page, pat_label)
            # except Exception as e:
            #     log.warning(f"Error during old PAT cleanup: {e}", exc_info=True)

            # Persist session for next run
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            await context.storage_state(path=str(STATE_FILE))
            log.info(f"Saved browser state to {STATE_FILE}")

            return token

        except Exception as e:
            # Take a screenshot on failure to diagnose selector issues
            try:
                screenshot_path = str(STATE_FILE.parent / "failure_screenshot.png")
                await page.screenshot(path=screenshot_path)
                log.info(f"Saved failure screenshot to {screenshot_path}")
            except Exception as ss_err:
                log.warning(f"Failed to capture screenshot: {ss_err}")

            # (We keep the cached state file so we can reuse the login session next time)
            raise

        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# HA REST API helper
# ---------------------------------------------------------------------------
async def push_token_to_ha(token: str) -> None:
    """Set input_text entity value via HA REST API."""
    url = f"{HA_URL}/api/services/input_text/set_value"
    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"entity_id": HA_ENTITY_ID, "value": token}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                log.info(f"Pushed new token to {HA_ENTITY_ID} ✓")
            else:
                body = await resp.text()
                raise RuntimeError(f"HA API returned {resp.status}: {body}")


async def run_keep_alive() -> bool:
    """Check if the saved session is still valid and refresh its activity timestamp."""
    async with async_playwright() as pw:
        use_headless = os.environ.get("USE_HEADED") != "true"
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if use_headless:
            launch_headless = False
            launch_args.append("--headless=new")
        else:
            launch_headless = False

        browser = await pw.chromium.launch(
            headless=launch_headless,
            args=launch_args
        )

        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "America/New_York",
            "extra_http_headers": {
                "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Linux"',
            }
        }
        if STATE_FILE.exists():
            log.info(f"Loading browser state for keep-alive from {STATE_FILE}")
            ctx_kwargs["storage_state"] = str(STATE_FILE)
        else:
            log.warning("No saved browser state file found. Cannot run keep-alive.")
            await browser.close()
            return False

        context = await browser.new_context(**ctx_kwargs)
        
        # Apply stealth overrides to bypass headless bot detection
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)

        page = await context.new_page()

        try:
            log.info(f"Navigating to {PAT_PAGE_URL} for keep-alive ping")
            await page.goto(PAT_PAGE_URL, wait_until="networkidle", timeout=30_000)
            
            # Check if we landed on the tokens page without being redirected to login
            if "account.smartthings.com/tokens" in page.url:
                # Save the fresh session state to update cookies
                await context.storage_state(path=str(STATE_FILE))
                log.info("Session is active. Successfully updated browser state.")
                return True
            else:
                log.warning(f"Session expired or redirected to: {page.url}")
                return False
        except Exception as e:
            log.error(f"Keep-alive failed: {e}")
            return False
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main() -> None:
    debug = "--debug" in sys.argv

    if "--clear-state" in sys.argv:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            log.info(f"Cleared saved browser state at {STATE_FILE}")
        else:
            log.info("No saved state to clear")
        return

    if "--keep-alive" in sys.argv:
        log.info("=== SmartThings Session Keep-Alive ===")
        success = await run_keep_alive()
        if success:
            log.info("=== Keep-alive complete: Session remains active ===")
        else:
            log.warning("=== Keep-alive complete: Session is EXPIRED ===")
            sys.exit(1)
        return

    log.info("=== SmartThings PAT Rotator starting ===")
    log.info(f"Target HA entity : {HA_ENTITY_ID}")
    log.info(f"Debug mode       : {debug}")

    token = await run_browser(debug=debug)
    await push_token_to_ha(token)

    log.info("=== PAT rotation complete ===")


if __name__ == "__main__":
    asyncio.run(main())
