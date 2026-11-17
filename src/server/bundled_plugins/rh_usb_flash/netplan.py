'''RH-ENE 增量4：面向 W5500 模块的空闲 IP / 网关地址推导（纯函数，不碰网络）。

判据按子网、不按网卡（主 spec §2.1）：一块网卡能同时挂默认路由段与面向模块的
别名段，按"网卡"筛会把别名段排掉。真实格式坑在归一层消化：macOS ifconfig 的
netmask 是十六进制（0xffffff00），arp -a 的 MAC 省略前导零（46:90:c4:2:b5:c）。
ping/探针以 callable 注入（运行层给 ping 实现），纯函数层只决定候选与排除。
'''

import ipaddress
import re
import sys


class NoCandidateSubnet(Exception):
    '''没有可用候选段或段内无空闲位；调用方必须停手，绝不自造地址下发。'''


def normalize_netmask(value):
    ''''0xffffff00'（macOS 十六进制）或 '255.255.255.0' → 前缀长度。'''
    text = str(value).strip()
    if text.startswith('0x') or text.startswith('0X'):
        bits = ipaddress.IPv4Address(int(text, 16))
        return bin(int(bits)).count('1')
    return bin(int(ipaddress.IPv4Address(text))).count('1')


def normalize_mac(value):
    return ':'.join(part.zfill(2) for part in str(value).strip().split(':'))


def _interface_subnets(interfaces):
    for entry in interfaces:
        net = ipaddress.IPv4Network(
            '{}/{}'.format(entry['ipv4'], normalize_netmask(entry['netmask'])),
            strict=False)
        if net.prefixlen != 24:
            continue
        if not net.is_private:
            continue
        yield net, ipaddress.IPv4Address(entry['ipv4'])


def pick_free_ip(env, arp_entries, alive):
    '''从面向模块的私有 /24 里挑空闲 IP；无候选段/无空闲位抛 NoCandidateSubnet。

    env = {'interfaces': [{iface, ipv4, netmask}], 'default_gateway': ip}。
    排除顺序：默认路由段 → 本机自身地址 → .1/.255 → ARP 已知条目（含 W5500
    旧 MAC de:ad:be:ef:00:55，源 MAC 固定于固件 eth_w5500.c）→ 探针应答。
    从 .98 起（与固件编译宏一致）向上扫描。
    '''
    default_net = None
    gw = env.get('default_gateway')
    if gw:
        default_net = ipaddress.IPv4Network('{}/24'.format(gw), strict=False)
    w5500_mac = normalize_mac('de:ad:be:ef:00:55')
    arp_norm = {ip: normalize_mac(mac) for ip, mac in (arp_entries or {}).items()}

    candidates = {}
    for net, local_ip in _interface_subnets(env.get('interfaces') or []):
        if default_net is not None and net == default_net:
            continue
        candidates[net] = local_ip
    if not candidates:
        raise NoCandidateSubnet('no private /24 besides the default-route subnet')

    probed = []
    for net, local_ip in candidates.items():
        for offset in range(98, 255):
            ip = ipaddress.IPv4Address(int(net.network_address) + offset)
            if ip == local_ip or offset in (1, 255):
                continue
            text = str(ip)
            if text in arp_norm and arp_norm[text] == w5500_mac:
                continue
            if text in arp_norm:
                continue
            if ip in probed:
                continue
            probed.append(ip)
            if alive(text):
                continue
            return text
    raise NoCandidateSubnet('no free address in candidate subnets')


def pick_gateway_host(servers, relay_ip4, local_ip4):
    '''中继拨入腿地址：优先播报列表里与中继 ip4 同段的那个；否则退化为本机
    地址并标 guessed=True（界面显示"推测"）。返回 (host, guessed)。'''
    hosts = [h for h in (servers or []) if h]
    if relay_ip4:
        relay_net = ipaddress.IPv4Network('{}/24'.format(relay_ip4), strict=False)
        for host in hosts:
            if ipaddress.IPv4Network('{}/24'.format(host), strict=False) == relay_net:
                return host, False
    if local_ip4:
        return local_ip4, True
    return (hosts[0], True) if hosts else (None, True)


_INET_RE = re.compile(
    r'inet (\d+\.\d+\.\d+\.\d+)(?: --> \d+\.\d+\.\d+\.\d+)? netmask (\S+)')
_PREFIX_INET_RE = re.compile(r'inet (\d+\.\d+\.\d+\.\d+)/(\d+) ')
_IFACE_RE = re.compile(r'^\d*:?(\S+?): flags|^(\d+): (\S+):')
_LINUX_IFACE_RE = re.compile(r'^\d+: (\S+): ')
_INET_LINUX_RE = re.compile(r'inet (\d+\.\d+\.\d+\.\d+)/(\d+) ')


def parse_ifconfig(text, platform='darwin'):
    '''ifconfig -a（macOS）或 ip -4 addr show（Linux）→ 接口地址表。

    输出 netmask 用原格式（macOS 十六进制 / Linux 点分），归一由
    normalize_netmask/pick_free_ip 完成。point-to-point 行（utun 的 -->）
    与 IPv6 行跳过。
    '''
    entries = []
    iface = None
    if platform == 'linux':
        for line in (text or '').splitlines():
            match = _LINUX_IFACE_RE.match(line.strip())
            if match:
                iface = match.group(1)
                continue
            inet = _INET_LINUX_RE.search(line)
            if inet and iface and not inet.group(1).startswith('127.'):
                bits = int(inet.group(2))
                entries.append({'iface': iface, 'ipv4': inet.group(1),
                                'netmask': str(ipaddress.IPv4Address(
                                    (0xffffffff << (32 - bits)) & 0xffffffff))})
        return entries
    for line in (text or '').splitlines():
        stripped = line.strip()
        if not line.startswith('\t') and not line.startswith('    '):
            name = line.split(':')[0].strip()
            iface = name if name else None
            continue
        inet = _INET_RE.search(stripped)
        if inet and iface and not inet.group(1).startswith('127.'):
            entries.append({'iface': iface, 'ipv4': inet.group(1),
                            'netmask': inet.group(2)})
    return entries


_GATEWAY_RE = re.compile(r'^default\s+(?:via\s+)?(\d+\.\d+\.\d+\.\d+)',
                         re.MULTILINE)


def parse_default_gateway(text):
    '''netstat -rn（macOS）或 ip route show（Linux）→ 默认网关 IP。'''
    match = _GATEWAY_RE.search(text or '')
    return match.group(1) if match else None


_MAC_ARP_RE = re.compile(r'\((\d+\.\d+\.\d+\.\d+)\) at (\S+)')
_LINUX_ARP_RE = re.compile(r'^(\d+\.\d+\.\d+\.\d+)\s+ether\s+(\S+)')


def parse_arp(text):
    '''arp -n（Linux）或 arp -a（macOS）→ {ip: mac}（MAC 归一补前导零）。

    incomplete 条目（Mac 无应答占位）与无 ether 字样的行跳过。
    '''
    found = {}
    for line in (text or '').splitlines():
        match = _LINUX_ARP_RE.match(line.strip()) or _MAC_ARP_RE.search(line)
        if not match:
            continue
        if match.group(2).lower() == 'incomplete':
            continue
        found[match.group(1)] = normalize_mac(match.group(2))
    return found


def arp_entries(runner=None, platform=None):
    '''采集真实 ARP 表（shape 与 pick_free_ip 的 arp_entries 参数一致）。'''
    if runner is None:
        def runner(cmd):
            import subprocess
            proc = subprocess.run(cmd, capture_output=True, text=True)
            return proc.stdout
    if platform is None:
        platform = sys.platform
    cmd = ['arp', '-n'] if platform.startswith('linux') else ['arp', '-a']
    return parse_arp(runner(cmd))


def alive_probe(runner=None, platform=None):
    '''ping 探针 callable：ip → bool。runner 注入返回 ping 退出码。'''
    if runner is None:
        def runner(cmd):
            import subprocess
            return subprocess.run(cmd, capture_output=True).returncode
    if platform is None:
        platform = sys.platform
    wait = ['1'] if platform.startswith('linux') else ['1000']
    base = ['ping', '-c', '1', '-W'] + wait

    def alive(ip):
        try:
            return runner(base + [ip]) == 0
        except Exception:
            return False
    return alive


def interface_env(runner=None, platform=None):
    '''采集真实接口表 + 默认网关，形状与 pick_free_ip 的 env 参数一致。

    runner(cmd) -> 输出文本（测试注入）；默认 subprocess。平台决定命令：
    darwin=ifconfig -a + netstat -rn -f inet；linux=ip -4 addr show +
    ip route show。
    '''
    if runner is None:
        _real_cmds = {'ifconfig -a': ['ifconfig', '-a'],
                      'netstat -rn -f inet': ['netstat', '-rn', '-f', 'inet'],
                      'ip -4 addr show': ['ip', '-4', 'addr', 'show'],
                      'ip route show': ['ip', 'route', 'show']}

        def runner(cmd):
            import subprocess
            proc = subprocess.run(_real_cmds[cmd], capture_output=True,
                                  text=True)
            return proc.stdout
    if platform is None:
        platform = sys.platform
    if platform.startswith('linux'):
        addr_cmd, route_cmd = 'ip -4 addr show', 'ip route show'
    else:
        addr_cmd, route_cmd = 'ifconfig -a', 'netstat -rn -f inet'
    return {'interfaces': parse_ifconfig(runner(addr_cmd), platform=platform),
            'default_gateway': parse_default_gateway(runner(route_cmd))}
