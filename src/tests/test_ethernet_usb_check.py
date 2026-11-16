'''RH-ENE 增量5：firmware_check 纯判定单测（不开串口、不跑 pio）。

判据口径见模块 docstring：适合 = kind → 推荐 env；最新 = 设备 INFO 的
commit 与固件工程当前 HEAD 相同。git 输出用注入 runner 造，不碰真实仓库。
'''

import os
import sys
import unittest
from types import SimpleNamespace

_TESTS_DIR = os.path.dirname(os.path.realpath(__file__))
for _p in (os.path.join(os.path.dirname(_TESTS_DIR), 'server'),
           os.path.join(os.path.dirname(_TESTS_DIR), 'server', 'util'),
           os.path.join(os.path.dirname(_TESTS_DIR), 'server', 'plugins')):
    if _p not in sys.path:
        sys.path.append(_p)

from plugins.rh_usb_flash import firmware_check  # noqa: E402


class FakeRunner():
    '''按 `git -C <project> <args...>` 的 args 查表回放。'''

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, cmd, **kwargs):
        key = tuple(cmd[3:])
        self.calls.append(key)
        returncode, stdout = self.mapping.get(key, (1, ''))
        return SimpleNamespace(returncode=returncode, stdout=stdout)


def runner(head='abc1234', describe='v1.2-3-gabc1234', dirty=''):
    return FakeRunner({
        ('rev-parse', '--short=7', 'HEAD'): (0, head + '\n'),
        ('describe', '--tags', '--always', '--dirty'): (0, describe + '\n'),
        ('status', '--porcelain'): (0, dirty),
    })


class Mapping(unittest.TestCase):
    def test_recommended_env_by_kind(self):
        self.assertEqual('esp32s3-relay',
                         firmware_check.recommended_env('relay'))
        self.assertEqual('esp32s3-relay', firmware_check.recommended_env('s3'))
        self.assertEqual('esp32c5-eth', firmware_check.recommended_env('eth'))
        self.assertEqual('esp32c5-eth', firmware_check.recommended_env('c5'))
        self.assertIsNone(firmware_check.recommended_env('unknown'))

    def test_env_target(self):
        self.assertEqual('esp32s3', firmware_check.env_target('esp32s3-relay'))
        self.assertEqual('esp32c5', firmware_check.env_target('esp32c5-head'))
        self.assertIsNone(firmware_check.env_target('nope'))

    def test_version_commit_from_git_describe(self):
        self.assertEqual('4a7278a', firmware_check.version_commit(
            'Web-v0.11-2-g4a7278a-dirty'))
        self.assertEqual('4a7278a', firmware_check.version_commit(
            'Web-v0.11-2-g4a7278a'))
        self.assertIsNone(firmware_check.version_commit('Web-v0.11'))
        self.assertIsNone(firmware_check.version_commit(None))

    def test_version_dirty(self):
        self.assertTrue(firmware_check.version_dirty('Web-v0.11-2-g4a7278a-dirty'))
        self.assertFalse(firmware_check.version_dirty('Web-v0.11-2-g4a7278a'))


class ProjectVersion(unittest.TestCase):
    def test_empty_project_is_none(self):
        self.assertIsNone(firmware_check.project_version(''))

    def test_git_failure_is_none(self):
        fake = FakeRunner({})
        self.assertIsNone(firmware_check.project_version('/tmp/x', runner=fake))

    def test_reports_commit_describe_and_dirty(self):
        fake = runner(head='abc1234', describe='v1.2-3-gabc1234-dirty',
                      dirty=' M src/main.c\n')
        info = firmware_check.project_version('/tmp/proj', runner=fake)
        self.assertEqual('abc1234', info['commit'])
        self.assertEqual('v1.2-3-gabc1234-dirty', info['describe'])
        self.assertTrue(info['dirty'])


class Check(unittest.TestCase):
    def device(self, **over):
        dev = {'kind': 'relay', 'target': 'esp32s3',
               'version': 'Web-v0.11-2-gabc1234'}
        dev.update(over)
        return dev

    def test_unrecognized_device(self):
        result = firmware_check.check({'kind': 'unknown'}, '/tmp/proj',
                                      runner=runner())
        self.assertEqual('unknown', result['verdict'])
        self.assertIsNone(result['expected_env'])

    def test_missing_pio_short_circuits(self):
        result = firmware_check.check(self.device(), '/tmp/proj',
                                      runner=runner(), pio=False)
        self.assertEqual('no-pio', result['verdict'])
        self.assertEqual('esp32s3-relay', result['expected_env'])

    def test_unconfigured_project(self):
        result = firmware_check.check(self.device(), '',
                                      runner=runner())
        self.assertEqual('no-project', result['verdict'])

    def test_current_when_commits_match(self):
        result = firmware_check.check(self.device(), '/tmp/proj',
                                      runner=runner(head='abc1234'))
        self.assertEqual('current', result['verdict'])
        self.assertEqual('abc1234', result['project']['commit'])

    def test_outdated_when_commit_differs(self):
        result = firmware_check.check(self.device(), '/tmp/proj',
                                      runner=runner(head='def5678'))
        self.assertEqual('outdated', result['verdict'])
        self.assertEqual('abc1234', result['device_commit'])

    def test_version_without_git_hash_is_unknown(self):
        result = firmware_check.check(self.device(version='Web-v0.11'),
                                      '/tmp/proj', runner=runner())
        self.assertEqual('unknown', result['verdict'])
        self.assertEqual('device version has no git hash', result['error'])

    def test_c5_device_recommends_wired_env(self):
        result = firmware_check.check(
            self.device(kind='c5', target='esp32c5',
                        version='Web-v0.11-2-gdef5678'),
            '/tmp/proj', runner=runner(head='abc1234'))
        self.assertEqual('esp32c5-eth', result['expected_env'])
        self.assertEqual('outdated', result['verdict'])


if __name__ == '__main__':
    unittest.main()
