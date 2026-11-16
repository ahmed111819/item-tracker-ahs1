"""Catch invalid job-level contexts before GitHub rejects a workflow startup."""
import re
import unittest
from pathlib import Path


ALLOWED_JOB_ENV_CONTEXTS = {'github','needs','strategy','matrix','vars','secrets','inputs'}


def invalid_job_env_contexts(source):
    # This repository uses ordinary 2-space YAML indentation. Only inspect
    # direct job env blocks; runner is valid in steps and must stay allowed there.
    errors = []
    in_job_env = False
    for line in source.splitlines():
        if re.fullmatch(r'    env:\s*',line):
            in_job_env = True
            continue
        if in_job_env and line.strip() and not line.startswith('      '):
            in_job_env = False
        if in_job_env:
            for expr in re.findall(r'\$\{\{(.*?)\}\}',line):
                for root in re.findall(r'\b([A-Za-z_][A-Za-z_0-9]*)\s*\.',expr):
                    if root not in ALLOWED_JOB_ENV_CONTEXTS:
                        errors.append(root)
    return errors


class WorkflowConfigurationTests(unittest.TestCase):
    def test_scanner_job_environment_uses_available_contexts(self):
        source = Path('.github/workflows/amazon-sa-bot3.yml').read_text(encoding='utf-8')
        self.assertEqual(invalid_job_env_contexts(source),[])

    def test_original_startup_failure_is_detected(self):
        bad = 'jobs:\n  scan:\n    env:\n      REPORT: ${{ runner.temp }}/samples\n    steps:\n      - run: echo ok\n'
        self.assertEqual(invalid_job_env_contexts(bad),['runner'])
        good = bad.replace('runner.temp','github.workspace')
        self.assertEqual(invalid_job_env_contexts(good),[])

    def test_runner_remains_valid_inside_a_step(self):
        valid = 'jobs:\n  scan:\n    env:\n      TOKEN: ${{ secrets.TOKEN }}\n    steps:\n      - run: echo ok\n        env:\n          REPORT: ${{ runner.temp }}\n'
        self.assertEqual(invalid_job_env_contexts(valid),[])

    def test_diagnostic_writer_and_artifact_use_same_directory(self):
        source = Path('.github/workflows/amazon-sa-bot3.yml').read_text(encoding='utf-8')
        writer = re.search(r'^\s+UNKNOWN_OFFERS_DIR:\s*(.+)$',source,re.M).group(1).strip()
        reader = re.search(r'^\s+path:\s*(.+)/\*\.jsonl\s*$',source,re.M).group(1).strip()
        self.assertEqual(writer,reader)
