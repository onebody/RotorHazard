'''RH-ENE 增量4：固件现场构建 + 产物缓存 + 烧录命令编排。

注入路径只有一条可行（2026-11-15 实测，spec §5）：PLATFORMIO_BUILD_FLAGS 追加
IP 宏会与 env 已有定义重复定义、-Werror 拒编；因此注入=在工程 .pio 下生成完整
platformio.ini 副本（只改目标 env 段的 IP 宏）→ `pio run -c <副本>`，原工程文件
零改动。探针/台架等 extends 自 esp32c5-eth 的 env 不受影响（只改
[env:esp32c5-eth] 段内的行）。覆盖副本在每次 runner 调用（构建/烧录）时重建、
用后即删——build 与 flash 各自独立闭环，不传递文件状态。

runner 参数注入（测试传假对象记录 argv）；缓存键=env+注入 IP，同参数二刷零重编。
烧录走 PIO 的 upload target（USB-Serial/JTAG 硬件复位进下载模式，日常烧录同路），
与「运行期探测 no-reset 不拉 RTS」不冲突——烧录必须复位。
'''

import hashlib
import json
import os
import re
import shutil

FLASH_ENVS = {
    'esp32c5': '常规扫频（USB 直连 RH @115200）',
    'esp32c5-head': '无线头（UART 接 S3 中继 @921600）',
    'esp32c5-eth': '有线节点（W5500，构建时注入配网 IP）',
    'esp32s3-relay': 'S3 Wi-Fi 中继',
}

PPIO = '/Users/fcj/.venvs/pio313/bin/pio'
# 只改 ETH_IP_ADDR：GATEWAY/NETMASK 保留 env 编译值（面向 W5500 的段无网关、/24）。
IP_ADDR_RE = re.compile(r"(-DETH_IP_ADDR=)'\"[^\"]*\"'")
OVERRIDE_NAME = 'platformio.provision.flash.ini'


class FlashError(Exception):
    pass


def override_ini_ip(text, env, ip):
    '''只改 [env:<env>] 段内的 IP 宏为注入值；段不存在抛 FlashError。'''
    header = '[env:{}]'.format(env)
    lines = text.split('\n')
    in_target = False
    seen_header = False
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[env:') and stripped.endswith(']'):
            in_target = stripped == header
            seen_header = seen_header or in_target
        if in_target:
            line = IP_ADDR_RE.sub(r"\1'\"" + ip + "\"'", line)
        out.append(line)
    if not seen_header:
        raise FlashError('env {} not found in platformio.ini'.format(env))
    return '\n'.join(out)


def _cache_path(cache_dir, env, injected_ip):
    key = hashlib.sha256(
        '{}|{}'.format(env, injected_ip or '').encode()).hexdigest()[:16]
    return os.path.join(cache_dir, key)


def _write_override(project, env, injected_ip):
    ini = os.path.join(project, 'platformio.ini')
    with open(ini) as f:
        text = f.read()
    path = os.path.join(project, '.pio', OVERRIDE_NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(override_ini_ip(text, env, injected_ip))
    os.chmod(path, 0o600)
    return path


def _run(runner, cmd, project, what):
    proc = runner(cmd, shell=True, capture_output=True, text=True, cwd=project)
    if proc.returncode != 0:
        tail = (proc.stdout or '')[-400:] + (proc.stderr or '')[-400:]
        raise FlashError('{} failed: {}'.format(what, tail))
    return proc


def build_firmware(env, project, cache_dir, runner, injected_ip=None):
    '''构建固件；缓存命中（同 env+同注入 IP 且 bin 在）则零 runner 调用。

    返回 {'env', 'bin', 'injected_ip', 'cache_hit', 'manifest'}。
    '''
    if env not in FLASH_ENVS:
        raise FlashError('unknown flash env: {}'.format(env))
    if not os.path.isfile(os.path.join(project, 'platformio.ini')):
        raise FlashError('firmware project not found: {}'.format(project))
    cache = _cache_path(cache_dir, env, injected_ip)
    manifest_path = cache + '.json'
    if os.path.isfile(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        if os.path.isfile(manifest['bin']):
            return {'env': env, 'bin': manifest['bin'],
                    'injected_ip': injected_ip, 'cache_hit': True,
                    'manifest': manifest}

    override_path = None
    cmd = PPIO + ' run -e ' + env
    if injected_ip:
        override_path = _write_override(project, env, injected_ip)
        cmd += ' -c ' + override_path
    try:
        _run(runner, cmd, project, 'build')
        bin_path = os.path.join(project, '.pio', 'build', env, 'firmware.bin')
        if not os.path.isfile(bin_path):
            raise FlashError('build reported success but firmware.bin missing')
    finally:
        if override_path:
            try:
                os.remove(override_path)
            except OSError:
                pass

    os.makedirs(cache_dir, exist_ok=True)
    cached_bin = cache + '.bin'
    shutil.copyfile(bin_path, cached_bin)
    with open(cached_bin, 'rb') as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    manifest = {'env': env, 'injected_ip': injected_ip, 'bin': cached_bin,
                'sha256': digest}
    with open(manifest_path, 'w') as f:
        json.dump(manifest, f)
    return {'env': env, 'bin': cached_bin, 'injected_ip': injected_ip,
            'cache_hit': False, 'manifest': manifest}


def flash_firmware(env, port, project, cache_dir, runner, injected_ip=None):
    '''构建（或命中缓存）后经 PIO upload target 烧录到指定串口。'''
    result = build_firmware(env, project, cache_dir, runner,
                            injected_ip=injected_ip)
    cmd = PPIO + ' run -e ' + env + ' -t upload --upload-port ' + port
    override_path = None
    if result['injected_ip']:
        override_path = _write_override(project, env, result['injected_ip'])
        cmd += ' -c ' + override_path
    try:
        _run(runner, cmd, project, 'flash')
    finally:
        if override_path:
            try:
                os.remove(override_path)
            except OSError:
                pass
    result['port'] = port
    return result
