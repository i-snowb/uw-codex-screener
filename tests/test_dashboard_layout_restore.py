from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_enriched_morning_dashboard import build_fragment
from signal_research_panel import SIGNAL_RESEARCH_JS


class DashboardLayoutTests(unittest.TestCase):
    def test_signal_research_drawer_starts_closed(self):
        self.assertIn('<details><summary>Which signals help predict returns?</summary>',
                      SIGNAL_RESEARCH_JS)
        self.assertNotIn('<details open', SIGNAL_RESEARCH_JS)

    def test_research_default_preserves_trial_without_compact_redesign(self):
        run = {'run_id': 'synthetic', 'cutoff_at': '2026-09-10T12:00:00Z',
               'generated_at': '2026-09-10T12:00:00Z', 'mode': 'SHADOW', 'watchlist': []}
        html = build_fragment(run)
        self.assertIn("searchParams.get('mode')==='focus'?'FOCUS':'RESEARCH'", html)
        self.assertIn('priceTrialMarkup', html)
        self.assertNotIn('function decisionSummary', html)
        self.assertNotIn('me-global-detail', html)
        script = html.split('<script>', 1)[1].split('</script>', 1)[0]
        result = subprocess.run(['node', '--check'], input=script,
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
