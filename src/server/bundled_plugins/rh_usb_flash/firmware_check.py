'''RH-ENE 增量5：设备固件「最新 / 适合」判定。

适合 = 设备类别（kind）对应的 PlatformIO env 就是该烧的那个；最新 = 设备
INFO 的 version（固件 `git describe --tags --dirty`，形如
`Web-v0.11-2-g4a7278a-dirty`）里嵌的 commit 与固件工程当前 HEAD 相同。
工程未配置、不是 git 仓库、PIO 缺失、version 不含 git 哈希时给明确 verdict，
不猜版本高低。runner 注入（默认 subprocess.run），纯判定不开串口不编译。
'''

import re

# env → 固件 target（与 INFO 第 2 字段同口径），用于「选了别家的固件」提示。
ENV_TARGET = {
    'esp32c5': 'esp32c5',
    'esp32c5-head': 'esp32c5',
    'esp32c5-eth': 'esp32c5',
    'esp32s3-relay': 'esp32s3',
}

# 设备类别 → 该烧的 env（与 usb_devices._FLASH_DEFAULT 同源含义）。
RECOMMENDED_ENV = {'relay': 'esp32s3-relay', 's3': 'esp32s3-relay',
                   'eth': 'esp32c5-eth', 'c5': 'esp32c5-eth'}


def env_target(env):
    return ENV_TARGET.get(env)


# INFO 的 commit 字段是构建时间戳（固件格式如此），git 哈希只嵌在 version 里。
_GIT_DESC = re.compile(r'-g([0-9a-f]{7,40})(-dirty)?$')


def version_commit(version):
    '''从 `git describe` 风格版本串里抽 commit；不是该格式返回 None。'''
    match = _GIT_DESC.search((version or '').strip())
    return match.group(1) if match else None


def version_dirty(version):
    return bool((version or '').strip().endswith('-dirty'))


def recommended_env(kind):
    return RECOMMENDED_ENV.get(kind)


def project_version(project, runner=None):
    '''固件工程当前版本：HEAD 短哈希 / describe / 是否脏；不是 git 仓库返回 None。'''
    if not project:
        return None
    if runner is None:
        import subprocess
        runner = subprocess.run

    def git(*args):
        proc = runner(['git', '-C', project] + list(args),
                      capture_output=True, text=True)
        if getattr(proc, 'returncode', 1) != 0:
            return None
        return (getattr(proc, 'stdout', '') or '').strip()

    commit = git('rev-parse', '--short=7', 'HEAD')
    if not commit:
        return None
    return {'commit': commit,
            'describe': git('describe', '--tags', '--always', '--dirty') or None,
            'dirty': bool(git('status', '--porcelain'))}


def check(device, project, runner=None, pio=True):
    '''设备 vs 工程 → {'verdict', 'expected_env', 'env_target',
    'device_target', 'device_commit', 'project', 'error'}。

    verdict ∈ current / outdated / no-pio / no-project / unknown。
    '''
    device = device or {}
    expected = recommended_env(device.get('kind'))
    device_commit = version_commit(device.get('version'))
    result = {'verdict': 'unknown', 'expected_env': expected,
              'env_target': env_target(expected),
              'device_target': device.get('target'),
              'device_commit': device_commit,
              'device_dirty': version_dirty(device.get('version')),
              'project': None, 'error': None}
    if not expected:
        result['error'] = 'unrecognized device'
        return result
    if not pio:
        result['verdict'] = 'no-pio'
        return result
    version = project_version(project, runner=runner)
    if version is None:
        result['verdict'] = 'no-project'
        return result
    result['project'] = version
    if not device_commit:
        result['error'] = 'device version has no git hash'
        return result
    if device_commit[:7] == version['commit'][:7]:
        result['verdict'] = 'current'
    else:
        result['verdict'] = 'outdated'
    return result
