"""Match the request UA to the Chromium used for local challenge execution."""
from functools import lru_cache
from importlib import resources
import json
import os
import re
import subprocess
import sys

from utils.browser_turnstile import BrowserTurnstileError, _worker_environment


@lru_cache(maxsize=8)
def _chromium_major(executable: str) -> str:
    try:
        if executable:
            result = subprocess.run(
                [executable, "--version"], capture_output=True, text=True,
                timeout=5, check=True, env=_worker_environment(),
            )
            version = re.search(r"(?:Chromium|Chrome(?: for Testing)?)\s+(\d+)\.", result.stdout)
            if not version:
                raise ValueError("unrecognized browser version")
            return version.group(1)
        # Playwright installs this exact Chromium revision. No browser launch or
        # authenticated request is needed to inspect its packaged metadata.
        metadata = json.loads(resources.files("playwright").joinpath("driver/package/browsers.json").read_text())
        browser = next(item for item in metadata["browsers"] if item["name"] == "chromium")
        major = str(browser["browserVersion"]).split(".")[0]
        if not major.isdigit():
            raise ValueError("unrecognized packaged browser version")
        return major
    except ModuleNotFoundError:
        raise BrowserTurnstileError("playwright_missing") from None
    except Exception:
        raise BrowserTurnstileError("browser_start_failed") from None


def browser_fingerprint() -> dict[str, str]:
    major = _chromium_major(os.getenv("CHATGPT2API_BROWSER_EXECUTABLE", "").strip())
    if sys.platform == "win32":
        platform, ua_platform = "Windows", "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        platform, ua_platform = "macOS", "Macintosh; Intel Mac OS X 10_15_7"
    else:
        platform, ua_platform = "Linux", "X11; Linux x86_64"
    return {
        "user-agent": f"Mozilla/5.0 ({ua_platform}) AppleWebKit/537.36 "
                      f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36",
        "sec-ch-ua": f'"Chromium";v="{major}"',
        "sec-ch-ua-platform": f'"{platform}"',
    }
