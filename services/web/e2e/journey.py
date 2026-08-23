"""The browser journey, end to end.

Written in Python and run inside the AI service's runtime image on purpose: that
image already ships Chromium for scraping Tier 2, so this needs no second
browser download and no second Playwright install. The alternative --
`@playwright/test` in services/web -- would add ~400MB of browser to a service
that does not otherwise need one, to drive the same three clicks.

    make e2e

It stops at the keyword gate. Everything past that spends Gemini quota, so the
paid half is opt-in:

    make e2e FULL=1
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

WEB = os.getenv("WEB_URL", "http://localhost:3000")
FULL = os.getenv("FULL") == "1"
#: Continue an existing session instead of starting one. A rewrite costs real
#: model quota, so when the frontend is at fault -- which is what happened the
#: first time this ran FULL -- re-running the whole journey would pay for the
#: same generation twice to test a fix that has nothing to do with it.
RESUME_SESSION = os.getenv("SESSION_URL", "")
SHOTS = Path(os.getenv("SHOT_DIR", "/out/e2e"))
JD = Path("/app/tests/fixtures/sample_jd.txt").read_text(encoding="utf-8")


def shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"  screenshot: {path}")


def review_and_compile(page) -> None:
    """The paid half: the review gate, approval, compilation, the PDF."""
    print("7. the review gate")
    expect(page.get_by_role("heading", name="Review the rewrite")).to_be_visible(timeout=180_000)
    expect(page.get_by_text("sections changed")).to_be_visible()

    # The first changed section is expanded on arrival -- that is the point of
    # the screen -- and an open diff must not drag the page into horizontal
    # scroll: a unified diff is far wider than the viewport, and the first
    # version let the table grow to fit it.
    expect(page.locator("pre.diff").first).to_be_visible()
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"the page scrolls horizontally by {overflow}px with a diff open"
    shot(page, "03-review")

    print("8. approving and compiling")
    page.get_by_test_id("accept").click()
    expect(page.get_by_role("heading", name="Your resume")).to_be_visible(timeout=120_000)
    expect(page.get_by_test_id("download")).to_be_visible()
    shot(page, "04-pdf")


def main() -> int:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        failures: list[str] = []
        # A console error in the browser is a real failure even when the DOM looks
        # right -- a hydration mismatch or a bad fetch shows up here first.
        page.on("console", lambda m: failures.append(m.text) if m.type == "error" else None)

        if RESUME_SESSION:
            print(f"Resuming {RESUME_SESSION} (no new generation is paid for)")
            page.goto(RESUME_SESSION, wait_until="networkidle")
            review_and_compile(page)
            real = [f for f in failures if "favicon" not in f.lower()]
            if real:
                print("\nBrowser console errors:")
                for message in real:
                    print(f"  {message}")
                return 1
            browser.close()
            print("\nReview to PDF complete, no console errors.")
            return 0

        print("1. the start screen")
        page.goto(WEB, wait_until="networkidle")
        expect(page.get_by_role("heading", name="Tailor your resume to a posting")).to_be_visible()
        # The dev token is minted by the page itself on first load; if that failed
        # the profile check below would show a red notice instead.
        expect(page.get_by_text("no LaTeX template")).to_have_count(0)
        shot(page, "01-start")

        print("2. paste the posting and extract")
        page.get_by_test_id("job-text").fill(JD)
        page.get_by_test_id("start").click()

        print("3. the keyword gate")
        page.wait_for_url("**/sessions/**", timeout=30_000)
        chips = page.get_by_test_id("keyword-chip")
        # Streaming progress, then the gate. 35 keywords for this fixture.
        expect(chips.first).to_be_visible(timeout=30_000)
        count = chips.count()
        print(f"   {count} keywords offered")
        assert count > 10, f"expected a real keyword set, got {count}"
        # .first: the label appears both as the progress heading and in the log.
        expect(page.get_by_text("Extracting keywords").first).to_be_visible()
        shot(page, "02-keywords")

        print("4. dropping a keyword updates the count")
        before = page.get_by_text("kept").inner_text()
        chips.nth(1).click()
        expect(page.get_by_text("kept")).not_to_have_text(before)
        # Put it back, so the run below is the full set.
        chips.nth(1).click()

        session_url = page.url
        print(f"   session: {session_url}")

        print("5. the profile page, and Monaco loading from this origin")
        page.goto(f"{WEB}/profile", wait_until="networkidle")
        expect(page.get_by_text("Your LaTeX template")).to_be_visible()
        # Monaco is served from /monaco/vs rather than a CDN, so this assertion is
        # really "the local copy was found": its absence would leave the
        # "Loading editor…" placeholder forever, and no test of the DOM alone
        # would notice.
        expect(page.locator(".monaco-editor").first).to_be_visible(timeout=30_000)
        expect(page.get_by_text("Loading editor")).to_have_count(0)
        shot(page, "05-profile")
        page.goto(session_url, wait_until="networkidle")

        if not FULL:
            print("\nStopping at the keyword gate: everything past it spends Gemini")
            print("quota. Re-run with FULL=1 to drive the rest of the journey.")
        else:
            print("6. generating (this spends model quota)")
            page.get_by_test_id("confirm-keywords").click()
            review_and_compile(page)

        # Hydration mismatches and failed fetches surface here even when the page
        # looks correct.
        real = [f for f in failures if "favicon" not in f.lower()]
        if real:
            print("\nBrowser console errors:")
            for message in real:
                print(f"  {message}")
            return 1

        browser.close()
        print("\nJourney complete, no console errors.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
