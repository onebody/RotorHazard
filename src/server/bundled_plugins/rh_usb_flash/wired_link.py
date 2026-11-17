'''有线模式数据面：C5+W5500 经 bridge.py 变成 RH 串口节点（纯逻辑，可单测）。

固件侧 W5500 是 TCP server（ETH_TCP_PORT=12345，见 QiYun-RMC58 eth_w5500.h），
与 USB 串口同一套命令通道；RH 侧只认 GENERAL.SERIAL_PORTS 里的死字符串。
桥（bridge.py，与插件同包自带）把 tcp://<C5_IP>:12345 泵进一条 --link 钉住
固定路径的 PTY——两端不变，换链路不动配置。

与 relay 的「插件绝不代跑进程」不同：有线没有常驻网关/守护，用户在面板点
「连接」就该通，所以本模块负责拉起/停止桥进程，并把 PTY 写进 RH 扫描清单
（单选语义与核心 _set_active_node 的 serial 路径一致：启用有线即让出其他口）。

无线中继（relay）已冻结：不再新增能力，仅保留只读展示。
'''

import os
import signal

LINK = '/tmp/rh-w5500-1'
DEFAULT_PORT = 12345
TRANSPORT = 'wired'
PID_KEY = 'WIRED_PID'
HOST_KEY = 'WIRED_HOST'
PORT_KEY = 'WIRED_PORT'

# wired_status 的四态：running=桥进程活着；link=PTY 就位；enabled=RH 在扫；
# online=RH 接口里真的出现了这条口（说明 C5 应答了节点协议）。
OFFLINE, BRIDGE_ONLY, LINK_READY, ONLINE = 'offline', 'bridge', 'link', 'online'


def parse_hostport(data, cfg_host, cfg_port):
    '''UI 传入优先，缺省回读配置；端口回退固件定死的 12345。返回 (host, port) 或 None。'''
    host = str((data or {}).get('host') or cfg_host or '').strip()
    raw = str((data or {}).get('port') or cfg_port or '').strip()
    if not host:
        return None
    try:
        port = int(raw) if raw else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    if not (0 < port < 65536):
        return None
    return host, port


def pid_alive(pid):
    '''进程探活：ESRCH=死，EPERM=活着（归别人所有，同样别再拉新的）。'''
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError):
        return False
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def stop_pid(pid):
    '''SIGTERM 优雅停桥；进程不存在就当停成功（幂等，重装/重启路径会用到）。'''
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return False


def bridge_argv(bridge_py, host, port):
    '''桥命令行：与本插件同进程的解释器跑同包自带的 bridge.py。'''
    return [bridge_py, '--to', 'tcp://%s:%d' % (host, port), '--link', LINK]


def state(running, link_ok, enabled, online):
    '''四态合成（短路序：进程 → PTY → 扫描 → 在线）。'''
    if not running:
        return OFFLINE
    if not link_ok:
        return BRIDGE_ONLY
    return ONLINE if online else (LINK_READY if enabled else BRIDGE_ONLY)


def status_payload(host, port, pid, running, link_ok, enabled, online):
    return {
        'host': host, 'port': port, 'pid': pid if running else None,
        'running': running, 'link': link_ok, 'enabled': enabled,
        'online': online, 'state': state(running, link_ok, enabled, online),
        'link_path': LINK, 'command': bridge_argv('<bridge.py>', host, port),
    }


def merge_tag(tags, label='C5 有线节点 (W5500)', host=None):
    '''PORT_TAGS 只改 LINK 一个 key：transport=wired，wired_host 留排查线索。'''
    merged = {key: dict(value or {}) for key, value in (tags or {}).items()}
    merged[LINK] = {'label': label, 'transport': TRANSPORT,
                    'wired_host': host or ''}
    return merged


def drop_tag(tags):
    merged = {key: dict(value or {}) for key, value in (tags or {}).items()}
    merged.pop(LINK, None)
    return merged
