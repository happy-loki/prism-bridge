"""Offline browser selection regressions; real launches are verified separately."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bridge as b


class LoginLaunchTests(unittest.TestCase):
    def launch(self, browser, channel="", platform="nt"):
        environment = {"PRISM_BROWSER_CHANNEL": channel}
        with patch.object(b, "os", SimpleNamespace(name=platform, environ=environment)):
            return b._launch_login_context(SimpleNamespace(chromium=browser))

    def test_explicit_missing_channel_does_not_silently_change_browser(self):
        browser = Mock()
        failure = b.PlaywrightError("Chromium distribution 'chrome' is not found")
        browser.launch_persistent_context.side_effect = failure
        with self.assertRaises(b.PlaywrightError) as raised:
            self.launch(browser, channel="chrome")
        self.assertIs(raised.exception, failure)
        self.assertEqual(browser.launch_persistent_context.call_count, 1)

    def test_profile_failure_is_preserved_without_retry(self):
        for message in ("profile is already in use", "Target page, context or browser has been closed"):
            with self.subTest(message=message):
                browser = Mock()
                failure = b.PlaywrightError(message)
                browser.launch_persistent_context.side_effect = failure
                with self.assertRaises(b.PlaywrightError) as raised:
                    self.launch(browser)
                self.assertIs(raised.exception, failure)
                self.assertEqual(browser.launch_persistent_context.call_count, 1)

    def test_missing_installation_skips_to_next_available_browser(self):
        for message in ("Chromium distribution 'chrome' is not found at path", "Executable doesn't exist at path"):
            with self.subTest(message=message):
                browser = Mock()
                context = object()
                browser.launch_persistent_context.side_effect = [b.PlaywrightError(message), context]
                self.assertIs(self.launch(browser), context)
                self.assertEqual(browser.launch_persistent_context.call_args.kwargs["channel"], "msedge")

    def test_all_missing_retains_bundled_install_error_for_gui(self):
        browser = Mock()
        failure = b.PlaywrightError("Executable doesn't exist at bundled browser path")
        browser.launch_persistent_context.side_effect = [
            b.PlaywrightError("Chromium distribution 'chrome' is not found"),
            b.PlaywrightError("Chromium distribution 'msedge' is not found"),
            failure,
        ]
        with self.assertRaises(b.PlaywrightError) as raised:
            self.launch(browser)
        self.assertIs(raised.exception, failure)
        self.assertNotIn("channel", browser.launch_persistent_context.call_args.kwargs)
        self.assertEqual(browser.launch_persistent_context.call_count, 3)

    def test_non_windows_missing_browser_does_not_try_windows_channels(self):
        browser = Mock()
        failure = b.PlaywrightError("Executable doesn't exist at bundled browser path")
        browser.launch_persistent_context.side_effect = failure
        with self.assertRaises(b.PlaywrightError) as raised:
            self.launch(browser, platform="posix")
        self.assertIs(raised.exception, failure)
        self.assertEqual(browser.launch_persistent_context.call_count, 1)

    def test_programming_error_is_not_hidden_as_installation_failure(self):
        browser = Mock()
        browser.launch_persistent_context.side_effect = TypeError("invalid launch option")
        with self.assertRaises(TypeError):
            self.launch(browser)
        self.assertEqual(browser.launch_persistent_context.call_count, 1)

    def test_windows_uses_chrome_when_launch_succeeds(self):
        browser = Mock()
        context = object()
        browser.launch_persistent_context.return_value = context
        self.assertIs(self.launch(browser), context)
        self.assertEqual(browser.launch_persistent_context.call_count, 1)
        self.assertEqual(browser.launch_persistent_context.call_args.kwargs["channel"], "chrome")
        self.assertFalse(browser.launch_persistent_context.call_args.kwargs["headless"])

    def test_explicit_channel_success_does_not_fall_back(self):
        browser = Mock()
        context = object()
        browser.launch_persistent_context.return_value = context
        self.assertIs(self.launch(browser, channel="msedge"), context)
        self.assertEqual(browser.launch_persistent_context.call_count, 1)
        self.assertEqual(browser.launch_persistent_context.call_args.kwargs["channel"], "msedge")
        self.assertFalse(browser.launch_persistent_context.call_args.kwargs["headless"])


if __name__ == "__main__":
    unittest.main()
