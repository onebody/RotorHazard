'''RH-ENE 增量4（插件形态）：USB 设备下拉 + 固件刷写 + S3 中继直接配网。

后加装模块，不动 RH 原生功能：配置段、设置页面板、/usbflash 页面与五个
socket 事件全部在 initialize() 里向 RHAPI 注册，server.py / Config.py 零改动。
一次只允许一个串口任务（探测/刷写/配网共用 USB_LOCK）；刷写、配网、复验是
长任务，在 gevent 后台跑完再广播结果，不阻塞事件循环；赛中拒绝写入类动作。
'''

import logging
import os
import subprocess
import threading

import gevent
from flask import Response, render_template, request
from flask.blueprints import Blueprint

from . import firmware_flash, gateway_client, netplan, provision, relay_view, usb_ops

logger = logging.getLogger(__name__)

SECTION = 'USB_FLASH'
PANEL = 'usb_flash'
# 与 server.py 的 DATA_DIR 同层：插件包位于 <DATA_DIR>/plugins/<name>/。
DATA_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.realpath(__file__))))
DEFAULT_CACHE_DIR = os.path.join(DATA_DIR, 'usb_flash_cache')

USB_LOCK = threading.Lock()
BLUEPRINT = Blueprint('rh_usb_flash', __name__, template_folder='templates')
# register_markdown 的实参留档：注入面闸要对着「真正传进去的字符串」断言，
# 而不是对着模板或文档（Spec §7）。
REGISTERED_MARKDOWN = []
# add_url_rule 对同名端点二次注册会抛错，而 load_plugin 会把异常吞成"插件未
# 加载"。重复 initialize（reload/重装路径）据此跳过，不二次注册。
_registered = False


def _authenticate():
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})


def initialize(rhapi):
    global _registered
    if _registered:
        logger.info('rh_usb_flash already registered; skipping re-registration')
        return

    # RH 核心要在服务器就绪（gevent 已 monkey-patch）后才能导入；纯模块测试
    # 只 import 本包的子模块，不该被这些依赖拖起来。
    from AdminAuth import check_auth
    from RHUI import UIField, UIFieldType
    from RHRace import RaceStatus
    from interface_mapper import InterfaceType

    # ctx 只读：HTTP 守卫要 racecontext，配网在线判据要 RH-ENE 接口对象。
    ctx = rhapi._racecontext

    def settings():
        cfg = rhapi.config
        return {
            'pio_path': cfg.get(SECTION, 'PIO_PATH') or firmware_flash.PPIO,
            'project': cfg.get(SECTION, 'PROJECT_DIR') or '',
            'cache_dir': cfg.get(SECTION, 'CACHE_DIR') or DEFAULT_CACHE_DIR,
        }

    def refuse_if_racing():
        if ctx.race.race_status != RaceStatus.READY:
            rhapi.ui.message_notify(rhapi.__(
                'USB changes prevented by active race: stop and save laps'))
            return True
        return False

    def spawn(event, job, prefix=None):
        '''锁已持有：后台跑 job，结束释放锁并广播结果（prefix 带回调用参数）。'''
        def run_job():
            try:
                payload = job()
            except Exception as exc:
                logger.exception("Error in USB job for '%s'", event)
                payload = {'ok': False, 'error': str(exc)}
            finally:
                USB_LOCK.release()
            rhapi.ui.socket_broadcast(
                event, dict(prefix or {}, **payload))
        gevent.spawn(run_job)

    def gateway_cfg():
        '''网关地址只从配置读；半配就算没配（配置优先于代码兜底，不猜端口）。'''
        host = (rhapi.config.get(SECTION, 'GATEWAY_HOST') or '').strip()
        raw = str(rhapi.config.get(SECTION, 'GATEWAY_PORT') or '').strip()
        return host, (int(raw) if raw.isdigit() and int(raw) > 0 else None)

    def local_ports():
        '''与核心 local_nodes 同源：mapper 的 RH 串口接口里出现过的物理口。'''
        mapper = getattr(ctx, 'interface', None)
        rh = mapper.get_rh_interface() if mapper is not None else None
        ports = []
        for node in (getattr(rh, 'nodes', None) or []):
            port = getattr(getattr(node, 'serial', None), 'port', None)
            if port and port not in ports:
                ports.append(port)
        return ports

    def tags_now():
        return rhapi.config.get(relay_view.SECTION, relay_view.TAG_KEY) or {}

    def write_tags(tags):
        return bool(rhapi.config.set(relay_view.SECTION, relay_view.TAG_KEY, tags))

    def relay_snapshot(devices=None):
        host, port = gateway_cfg()
        nodes = gateway_client.fetch_nodes(
            gateway_client.gateway_base(host, port))
        if devices is None:
            devices = usb_ops.enumerate_devices()
        return relay_view.relay_rows(devices, nodes, tags_now(), local_ports(),
                                     host, port)

    def gateway_watcher():
        '''配网后的 online 判据：本机 ETHNODE 接口出现任何在线会话。'''
        def watch(url):
            for meta in ctx.interface.mapped_interfaces:
                if meta.type != InterfaceType.ETHNODE:
                    continue
                sessions = getattr(meta.interface, 'sessions', None) or {}
                return any(not session.offline for session in sessions.values()
                           if not session.superseded)
            return False
        return watch

    def on_list(data):
        if not USB_LOCK.acquire(blocking=False):
            rhapi.ui.socket_send('usb_devices', {'busy': True, 'devices': []})
            return
        try:
            devices = usb_ops.enumerate_devices()
        finally:
            USB_LOCK.release()
        current = settings()
        rhapi.ui.socket_send('usb_devices', {
            'devices': devices,
            'flash': {
                'pio': usb_ops.pio_available(current['pio_path']),
                'project': bool(current['project']) and os.path.isfile(
                    os.path.join(current['project'], 'platformio.ini')),
            }})

    def on_probe(data):
        port = (data or {}).get('port')
        if not USB_LOCK.acquire(blocking=False):
            rhapi.ui.socket_send('usb_device_detail', {'port': port,
                                                       'kind': 'unknown',
                                                       'error': 'busy'})
            return
        try:
            device = usb_ops.probe_one(port)
        finally:
            USB_LOCK.release()
        device['cached_ip'] = usb_ops.cached_ip_for(settings()['cache_dir'])
        rhapi.ui.socket_send('usb_device_detail', device)

    def flash_job(port, env):
        if env not in firmware_flash.FLASH_ENVS:
            return {'ok': False, 'error': 'unknown flash env: {}'.format(env)}
        current = settings()
        if not usb_ops.pio_available(current['pio_path']):
            return {'ok': False,
                    'error': 'PIO not available at ' + current['pio_path']}
        if not current['project'] or not os.path.isfile(
                os.path.join(current['project'], 'platformio.ini')):
            return {'ok': False,
                    'error': 'firmware project not configured '
                             '(USB_FLASH/PROJECT_DIR)'}
        injected_ip = None
        if env == 'esp32c5-eth':
            try:
                injected_ip = netplan.pick_free_ip(
                    netplan.interface_env(), netplan.arp_entries(),
                    netplan.alive_probe())
            except netplan.NoCandidateSubnet as exc:
                return {'ok': False, 'error': 'ip pick failed: {}'.format(exc)}
        return usb_ops.flash_and_verify(port, env, current['project'],
                                        current['cache_dir'], subprocess.run,
                                        injected_ip=injected_ip, boot_s=3.0)

    def on_flash(data):
        data = data or {}
        port, env = data.get('port'), data.get('env')
        if refuse_if_racing():
            rhapi.ui.socket_send('usb_flash_result',
                                 {'ok': False, 'refused': True,
                                  'error': 'active race', 'port': port,
                                  'env': env})
            return
        if not USB_LOCK.acquire(blocking=False):
            rhapi.ui.socket_send('usb_flash_result', {'ok': False,
                                                      'error': 'busy',
                                                      'port': port, 'env': env})
            return
        spawn('usb_flash_result', lambda: flash_job(port, env),
              {'port': port, 'env': env})

    def on_provision(data):
        data = data or {}
        port = data.get('port')
        if refuse_if_racing():
            rhapi.ui.socket_send('usb_provision_result',
                                 {'ok': False, 'refused': True,
                                  'error': 'active race', 'port': port})
            return
        if not USB_LOCK.acquire(blocking=False):
            rhapi.ui.socket_send('usb_provision_result', {'ok': False,
                                                          'error': 'busy',
                                                          'port': port})
            return
        host = data.get('host')
        port_num = int(data.get('port_num') or 8011)
        spawn('usb_provision_result',
              lambda: provision.provision_relay(
                  port, host, port_num, gateway_nodes=gateway_watcher()),
              {'port': port, 'host': host})

    def on_test(data):
        port = (data or {}).get('port')
        if not USB_LOCK.acquire(blocking=False):
            rhapi.ui.socket_send('usb_test_result', {'ok': False,
                                                     'error': 'busy',
                                                     'port': port})
            return

        def job():
            device = usb_ops.probe_one(port)
            return {'ok': device.get('kind') != 'unknown'
                    and not device.get('error'), 'device': device}
        spawn('usb_test_result', job, {'port': port})

    def on_relay_refresh(data):
        if not USB_LOCK.acquire(blocking=False):
            rhapi.ui.socket_send('relay_table',
                                 {'busy': True, 'rows': [], 'others': []})
            return
        try:
            table = relay_snapshot()
        except Exception as exc:
            logger.exception('relay refresh failed')
            table = {'rows': [], 'others': [], 'error': str(exc)}
        finally:
            USB_LOCK.release()
        host, port = gateway_cfg()
        rhapi.ui.socket_send('relay_table', dict(
            table, gateway={'host': host, 'port': port}))

    def on_relay_tag(data):
        '''只改显示：不过 READY 门（赛中想改备注不该改不动）。'''
        data = data or {}
        usb_port, node_port = data.get('usb_port'), data.get('node_port')
        if not usb_port or not node_port:
            rhapi.ui.socket_send('relay_result', {
                'ok': False, 'action': 'tag',
                'error': 'usb_port and node_port are required'})
            return
        if not USB_LOCK.acquire(blocking=False):
            rhapi.ui.socket_send('relay_result', {'ok': False, 'action': 'tag',
                                                  'error': 'busy'})
            return
        try:
            device = usb_ops.probe_one(usb_port)
        finally:
            USB_LOCK.release()
        if device.get('kind') != 'relay' or not device.get('mac'):
            rhapi.ui.socket_send('relay_result', {
                'ok': False, 'action': 'tag',
                'error': 'not a relay (kind={})'.format(device.get('kind'))})
            return
        label = data.get('label') or usb_port
        tags = relay_view.merge_tag(tags_now(), node_port, label, device['mac'])
        ok = write_tags(tags)
        rhapi.ui.socket_send('relay_result', {
            'ok': ok, 'action': 'tag', 'node_port': node_port,
            'relay_mac': device['mac'],
            'error': None if ok else 'config write failed'})

    def on_relay_unbind(data):
        node_port = (data or {}).get('node_port')
        if not node_port:
            rhapi.ui.socket_send('relay_result', {
                'ok': False, 'action': 'unbind', 'error': 'node_port is required'})
            return
        ok = write_tags(relay_view.drop_tag(tags_now(), node_port))
        rhapi.ui.socket_send('relay_result', {
            'ok': ok, 'action': 'unbind', 'node_port': node_port,
            'error': None if ok else 'config write failed'})

    def on_relay_retarget(data):
        data = data or {}
        usb_port = data.get('usb_port')
        cfg_host, cfg_port = gateway_cfg()
        host = (data.get('host') or cfg_host or '').strip()
        raw = data.get('port_num')
        port_num = int(raw) if raw else (cfg_port or 0)
        if not host or not port_num:
            rhapi.ui.socket_send('relay_retarget_result', {
                'ok': False, 'usb_port': usb_port,
                'error': 'gateway not configured (USB_FLASH/GATEWAY_HOST, '
                         'GATEWAY_PORT)'})
            return
        if refuse_if_racing():
            rhapi.ui.socket_send('relay_retarget_result', {
                'ok': False, 'refused': True, 'usb_port': usb_port,
                'error': 'active race'})
            return
        if not USB_LOCK.acquire(blocking=False):
            rhapi.ui.socket_send('relay_retarget_result', {
                'ok': False, 'usb_port': usb_port, 'error': 'busy'})
            return
        spawn('relay_retarget_result',
              lambda: provision.provision_relay(
                  usb_port, host, port_num, gateway_nodes=gateway_watcher()),
              {'usb_port': usb_port, 'host': host, 'port': port_num})

    def page():
        if not check_auth(ctx, request.authorization):
            return _authenticate()
        return render_template('usbflash.html', **{
            'serverInfo': ctx.serverstate.template_info_dict,
            'getOption': ctx.rhdata.get_option,
            'get_optionInt': ctx.rhdata.get_optionInt,
            'get_optionNumeric': ctx.rhdata.get_optionNumeric,
            'getConfig': ctx.serverconfig.get_item,
            '__': ctx.language.__,
            'Debug': ctx.serverconfig.get_item('GENERAL', 'DEBUG'),
            'restart_flag': ctx.serverstate.restart_required,
            'usb_environments': sorted(firmware_flash.FLASH_ENVS),
            'relay_ports': local_ports(),
            'gateway': dict(zip(('host', 'port'), gateway_cfg())),
        })

    rhapi.config.register_section(SECTION)
    # 合并页：USB 设备与以太网节点现为同一页面（/usbflash），设置页只留这一个面板。
    panel_title = rhapi.__('USB devices / Ethernet Nodes')
    rhapi.ui.register_panel(PANEL, panel_title, 'settings', 0, False)
    link_md = panel_title + ': [/usbflash](/usbflash)'
    # 元组顺序与 InjectionGuards 的解包一致：(name, panel, text)
    REGISTERED_MARKDOWN.append(('usb_flash_link', PANEL, link_md))
    rhapi.ui.register_markdown(PANEL, 'usb_flash_link', link_md)
    for field in [
        UIField('PIO_PATH', rhapi.__('PlatformIO executable'),
                UIFieldType.TEXT, value=firmware_flash.PPIO,
                persistent_section=SECTION,
                desc=rhapi.__('Used to build and flash the firmware project.')),
        UIField('PROJECT_DIR', rhapi.__('Firmware project directory'),
                UIFieldType.TEXT, value='', persistent_section=SECTION,
                desc=rhapi.__('Directory holding platformio.ini (QiYun-RMC58).')),
        UIField('CACHE_DIR', rhapi.__('Firmware build cache'),
                UIFieldType.TEXT, value=DEFAULT_CACHE_DIR,
                persistent_section=SECTION,
                desc=rhapi.__('Cached .bin per injected IP; empty to disable.')
                ),
        UIField('GATEWAY_HOST', rhapi.__('Gateway host'),
                UIFieldType.TEXT, value='', persistent_section=SECTION,
                desc=rhapi.__('LAN address of the gateway (serve.mjs) this '
                              'relay should dial.')),
        UIField('GATEWAY_PORT', rhapi.__('Gateway port'),
                UIFieldType.TEXT, value='', persistent_section=SECTION,
                desc=rhapi.__('Port of the gateway, e.g. 8011.')),
    ]:
        rhapi.fields.register_option(field, PANEL)

    for name, handler in [('usb_list', on_list),
                          ('usb_probe_dev', on_probe),
                          ('usb_flash', on_flash),
                          ('usb_provision', on_provision),
                          ('usb_test', on_test),
                          ('relay_refresh', on_relay_refresh),
                          ('relay_tag', on_relay_tag),
                          ('relay_unbind', on_relay_unbind),
                          ('relay_retarget', on_relay_retarget)]:
        rhapi.ui.socket_listen(name, handler, requires_auth=True)

    BLUEPRINT.add_url_rule('/usbflash', 'usbflash', page)
    rhapi.ui.blueprint_add(BLUEPRINT)
    _registered = True


def _subprocess_run(cmd, **kwargs):
    '''烧录/构建的子进程入口（list-argv，不走 shell）。'''
    import subprocess
    return subprocess.run(cmd, **kwargs)
