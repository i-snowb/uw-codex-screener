import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from morning_edge.experiments import ExperimentPlan, run_experiment, signal_contribution_report
from test_research_experiments import examples


class SignalAblationTests(unittest.TestCase):
    def test_shared_cohorts_train_only_scaling_and_holdout_are_preserved(self):
        rows = examples()
        result = run_experiment(rows, ExperimentPlan(), source_digest='fixed', include_ablation=True)
        scores = [r for r in result['scores'] if 'ablation_group' in r]
        self.assertEqual(18, len(scores))
        for horizon in (1, 5, 20):
            for target in ('absolute_return', 'excess_return'):
                cohort = [r for r in scores if r['horizon'] == horizon and r['target'] == target]
                self.assertEqual(1, len({r['cohort_sha256'] for r in cohort}))
                self.assertGreater(cohort[0]['matched_rows'], 0)
        self.assertEqual(['return_5', 'return_20'], result['folds'][0]['models']['absolute_return']['ridge_trend']['features'])
        held = copy.deepcopy(rows)
        for row in held:
            if row['target'] >= result['holdout']['start']:
                row['absolute_return'] += 100
                row['excess_return'] -= 100
        self.assertEqual(result, run_experiment(held, ExperimentPlan(), source_digest='fixed', include_ablation=True))
        report = signal_contribution_report(result, capture_cutoff='2026-09-10T12:00:00Z')
        self.assertEqual(18, len(report['comparisons']))
        self.assertFalse(report['promotion_eligible'])
        self.assertEqual(['Flow', 'Events/news'], [r['signal'] for r in report['untested']])
        self.assertNotIn('predictions', report)

    def test_ablation_requires_explicit_development_mode(self):
        script = Path(__file__).resolve().parents[1]/'scripts/run_research_experiments.py'
        result = subprocess.run([sys.executable, str(script), '--input', 'absent', '--database', 'absent',
                                 '--output', 'absent', '--run-signal-ablation'], capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn('requires --run-development', result.stderr)

    def test_panel_escapes_data_and_labels_units_and_untested_signals(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
        from signal_research_panel import SIGNAL_RESEARCH_JS
        helper = SIGNAL_RESEARCH_JS.split('async function installSignalResearch')[0]
        harness = r'''const assert=require('node:assert/strict');
const n=v=>typeof v==='number'&&Number.isFinite(v),pp=v=>v.toFixed(1)+' pp';
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
''' + helper + r'''
const report={comparisons:[{target:'absolute_return',ablation_group:'<img>',horizon:1,mae_improvement:.0001,accuracy_lift:0,matched_rows:12,origin_dates:3}],untested:[{signal:'Flow',reason:'<script>'}]};
const html=signalResearchMarkup(report,'absolute_return');
assert.ok(html.includes('+1.000'));assert.ok(html.includes('Basis points of return'));
assert.ok(html.includes('&lt;img&gt;'));assert.ok(!html.includes('<img>'));
assert.ok(html.includes('&lt;script&gt;'));assert.ok(html.includes('Not tested'));
assert.ok(!signalResearchMarkup(report,'excess_return').includes('+1.000'));
'''
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_publisher_only_writes_research_sidecars_and_rejects_promotion(self):
        script = Path(__file__).resolve().parents[1]/'scripts/publish_signal_research.py'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root/'app/data'
            data.mkdir(parents=True)
            (data/'latest.json').write_text('{"frozen":true}')
            report = {'schema_version': 'signal-contribution-v1', 'promotion_eligible': False,
                      'holdout': {'status': 'SEALED_NOT_SCORED'}, 'comparisons': [{'horizon': 1}],
                      'experiment_id': 'test'}
            source = root/'research.json'
            source.write_text(json.dumps(report))
            command = [sys.executable, str(script), '--input', str(source), '--app-root', str(root/'app')]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('{"frozen":true}', (data/'latest.json').read_text())
            self.assertEqual({'latest.json', 'signal-research.json', 'signal-research-manifest.json'},
                             {path.name for path in data.iterdir()})
            body = (data/'signal-research.json').read_bytes()
            manifest = (data/'signal-research-manifest.json').read_bytes()
            self.assertEqual(hashlib.sha256(body).hexdigest(), json.loads(manifest)['sha256'])
            report['promotion_eligible'] = True
            source.write_text(json.dumps(report))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(body, (data/'signal-research.json').read_bytes())
            self.assertEqual(manifest, (data/'signal-research-manifest.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
