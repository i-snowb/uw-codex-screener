from __future__ import annotations

import unittest

from morning_edge.config import ConfigError, Settings


class SettingsTest(unittest.TestCase):
    def test_defaults_are_explicit_and_secret_is_hidden(self) -> None:
        settings = Settings.from_env({"MORNING_EDGE_PROVIDER_API_KEY": "secret-value"})
        self.assertEqual(settings.timezone, "America/New_York")
        self.assertEqual(settings.snapshot_time, "06:55")
        self.assertEqual(
            settings.watchlist,
            ("ARM", "QCOM", "INTC", "AAOI", "CBRS", "CSCO", "NOK", "SKHY",
             "NBIS", "BABA", "MRVL", "CRWV", "AMD", "NVDA"),
        )
        self.assertNotIn("provider_api_key", settings.public_dict())
        self.assertTrue(settings.public_dict()["provider_configured"])

    def test_rejects_placeholder_secret(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"MORNING_EDGE_PROVIDER_API_KEY": "your_api_key_here"})

    def test_rejects_invalid_schedule(self) -> None:
        with self.assertRaises(ConfigError):
            Settings.from_env({"MORNING_EDGE_SNAPSHOT_TIME": "25:61"})

    def test_normalizes_watchlist(self) -> None:
        settings = Settings.from_env({"MORNING_EDGE_WATCHLIST": "qcom, intc"})
        self.assertEqual(settings.watchlist, ("QCOM", "INTC"))

    def test_accepts_provider_specific_key_without_exposing_it(self) -> None:
        settings = Settings.from_env({"UNUSUAL_WHALES_API_KEY": "local-secret"})
        self.assertTrue(settings.public_dict()["provider_configured"])
        self.assertNotIn("local-secret", str(settings.public_dict()))

    def test_accepts_public_configuration_aliases(self) -> None:
        settings = Settings.from_env({
            "CODEX_SCREENER_PROVIDER": "unusual_whales",
            "CODEX_SCREENER_PROVIDER_API_KEY": "alias-secret",
            "CODEX_SCREENER_WATCHLIST": "amd,nvda",
            "CODEX_SCREENER_TIMEZONE": "America/Chicago",
            "CODEX_SCREENER_SNAPSHOT_TIME": "05:45",
        })
        self.assertEqual(("AMD", "NVDA"), settings.watchlist)
        self.assertEqual("America/Chicago", settings.timezone)
        self.assertEqual("05:45", settings.snapshot_time)
        self.assertEqual("unusual_whales", settings.provider_name)
        self.assertTrue(settings.public_dict()["provider_configured"])

    def test_legacy_setting_keeps_precedence_during_alias_migration(self) -> None:
        settings = Settings.from_env({
            "MORNING_EDGE_WATCHLIST": "qcom",
            "CODEX_SCREENER_WATCHLIST": "amd,nvda",
        })
        self.assertEqual(("QCOM",), settings.watchlist)

    def test_private_runtime_root_moves_only_unset_defaults(self) -> None:
        settings = Settings.from_env({"CODEX_SCREENER_HOME": "/private/codex-screener"})
        self.assertEqual(
            "/private/codex-screener/data/morning-edge.sqlite",
            str(settings.database_path),
        )
        self.assertEqual(
            "/private/codex-screener/data/provider-request-usage.sqlite",
            str(settings.provider_usage_path),
        )
        explicit = Settings.from_env({
            "CODEX_SCREENER_HOME": "/private/codex-screener",
            "CODEX_SCREENER_DATABASE": "/explicit/snapshots.sqlite",
        })
        self.assertEqual("/explicit/snapshots.sqlite", str(explicit.database_path))


if __name__ == "__main__":
    unittest.main()
