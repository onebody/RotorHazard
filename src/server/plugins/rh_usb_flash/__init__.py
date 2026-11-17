'''RH-ENE 增量4（插件形态）：USB 设备下拉 + 固件刷写 + S3 中继直接配网。

后加装模块，不动 RH 原生功能：配置段、设置页面板、/usbflash 页面与五个
socket 事件全部在 initialize() 里向 RHAPI 注册，server.py / Config.py 零改动。
一次只允许一个串口任务（探测/刷写/配网共用 USB_LOCK）；刷写、配网、复验是
长任务，在 gevent 后台跑完再广播结果，不阻塞事件循环；赛中拒绝写入类动作。
'''

import ipaddress
import logging
import os
import subprocess
import threading

import gevent
from flask import Response, render_template, request
from flask.blueprints import Blueprint

from . import (firmware_check, firmware_flash, gateway_client, netplan,
               provision, relay_view, usb_ops)

logger = logging.getLogger(__name__)

SECTION = 'USB_FLASH'
PANEL = 'usb_flash'
# 与 server.py 的 DATA_DIR 同层：插件包位于 <DATA_DIR>/plugins/<name>/。
DATA_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.realpath(__file__))))
DEFAULT_CACHE_DIR = os.path.join(DATA_DIR, 'usb_flash_cache')

USB_LOCK = threading.Lock()
BLUEPRINT = Blueprint('rh_usb_flash', __name__, template_folder='templates')
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
    from RHRace import RaceStatus

    # ctx 只读：HTTP 守卫要 racecontext，relay 表要 mapper 的串口口列表。
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

    def settings_payload():
        '''页面「高级设置」表单与保存回执共用的一份当前配置。'''
        host, port = gateway_cfg()
        return dict(settings(), gateway={'host': host, 'port': port})

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

    def gateway_watcher(mac=None):
        '''配网后的 online 判据：网关 /api/nodes 里有这台中继的在线会话。

        中继连的是网关（serve.mjs），不是本机 RH 的 ETHNODE 接口，所以判据
        只能读网关节点表（spec §2/§3.5 红线：不发 WS attach）。
        '''
        def watch(url):
            host, port = gateway_cfg()
            reply = gateway_client.fetch_nodes(
                gateway_client.gateway_base(host, port))
            return reply['ok'] and gateway_client.relay_online(reply['nodes'],
                                                               mac)
        return watch

    def send_relay_table(devices=None):
        '''广播中继表；给了 devices 就复用（usb_list 刚枚举过，别再排队 5 秒）。'''
        try:
            table = relay_snapshot(devices)
        except Exception as exc:
            logger.exception('relay refresh failed')
            table = {'rows': [], 'others': [], 'error': str(exc)}
        host, port = gateway_cfg()
        rhapi.ui.socket_send('relay_table', dict(
            table, gateway={'host': host, 'port': port}))

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
            'flash': flash_state(current),
            'settings': settings_payload()})
        # 中继表随枚举一起回：首屏只需一次串口扫描，不会再出现
        # 「并发 relay_refresh 撞锁 → 表空 + A USB job is running」。
        send_relay_table(devices)

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
        current = settings()
        device['cached_ip'] = usb_ops.cached_ip_for(current['cache_dir'])
        # 「固件是否最新 / 适合」随详情一起回：选设备即完成检查，无需另发事件。
        device['check'] = firmware_check.check(
            device, current['project'],
            pio=usb_ops.pio_available(current['pio_path']))
        device['flash'] = flash_state(current)
        rhapi.ui.socket_send('usb_device_detail', device)

    def flash_state(current):
        return {'pio': usb_ops.pio_available(current['pio_path']),
                'project': bool(current['project']) and os.path.isfile(
                    os.path.join(current['project'], 'platformio.ini'))}

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

    def provision_job(port, host, port_num, wired_ip):
        '''按设备类别分派：中继走无线 NET 配网；C5 走有线（构建注入 IP 再烧）。'''
        device = usb_ops.probe_one(port)
        kind = device.get('kind')
        if kind == 'relay':
            if not host or not port_num:
                return {'ok': False, 'mode': 'wireless',
                        'error': 'gateway not configured '
                                 '(USB_FLASH/GATEWAY_HOST, GATEWAY_PORT)'}
            return dict(provision.provision_relay(
                port, host, port_num,
                gateway_nodes=gateway_watcher(device.get('mac'))),
                mode='wireless')
        if kind in ('eth', 'c5'):
            current = settings()
            state = flash_state(current)
            if not state['pio']:
                return {'ok': False, 'mode': 'wired',
                        'error': 'PIO not available at ' + current['pio_path']}
            if not state['project']:
                return {'ok': False, 'mode': 'wired',
                        'error': 'firmware project not configured '
                                 '(USB_FLASH/PROJECT_DIR)'}
            ip = (wired_ip or '').strip()
            if ip:
                try:
                    addr = ipaddress.IPv4Address(ip)
                except ValueError:
                    return {'ok': False, 'mode': 'wired',
                            'error': 'invalid IP: ' + ip}
                if not addr.is_private:
                    return {'ok': False, 'mode': 'wired',
                            'error': 'IP must be private: ' + ip}
            else:
                try:
                    ip = netplan.pick_free_ip(
                        netplan.interface_env(), netplan.arp_entries(),
                        netplan.alive_probe())
                except netplan.NoCandidateSubnet as exc:
                    return {'ok': False, 'mode': 'wired',
                            'error': 'ip pick failed: {}'.format(exc)}
            # 有线固件无运行期 ETHIP 命令：配网 IP 只在构建期生效，故必烧一次。
            result = usb_ops.flash_and_verify(
                port, 'esp32c5-eth', current['project'], current['cache_dir'],
                subprocess.run, injected_ip=ip, boot_s=3.0)
            result.update({'mode': 'wired', 'injected_ip': ip})
            return result
        return {'ok': False, 'mode': None,
                'error': 'not provisionable (kind={})'.format(kind)}

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
        cfg_host, cfg_port = gateway_cfg()
        host = (data.get('host') or cfg_host or '').strip()
        raw = str(data.get('port_num') or '').strip()
        port_num = int(raw) if raw.isdigit() else cfg_port
        wired_ip = data.get('wired_ip')
        spawn('usb_provision_result',
              lambda: provision_job(port, host, port_num, wired_ip),
              {'port': port, 'host': host})

    def on_save_settings(data):
        '''合并页接管原设置面板的配置项（面板已删）：保存到 USB_FLASH 段。'''
        data = data or {}
        values = {
            'PIO_PATH': str(data.get('pio_path') or '').strip(),
            'PROJECT_DIR': str(data.get('project') or '').strip(),
            'CACHE_DIR': str(data.get('cache_dir') or '').strip(),
            'GATEWAY_HOST': str(data.get('gateway_host') or '').strip(),
            'GATEWAY_PORT': str(data.get('gateway_port') or '').strip(),
        }
        port_text = values['GATEWAY_PORT']
        if port_text and not port_text.isdigit():
            rhapi.ui.socket_send('usb_settings_result', {
                'ok': False, 'error': 'gateway port must be numeric',
                'settings': settings_payload()})
            return
        ok = True
        for key, value in values.items():
            ok = bool(rhapi.config.set(SECTION, key, value)) and ok
        rhapi.ui.socket_send('usb_settings_result', {
            'ok': ok, 'settings': settings_payload(),
            'error': None if ok else 'config write failed'})

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
            devices = usb_ops.enumerate_devices()
        finally:
            USB_LOCK.release()
        send_relay_table(devices)

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

    def on_relay_reconnect(data):
        '''重连 WiFi；给了 ssid 就先改 SSID/密码再 NET SAVE。赛中拒绝——会断掉跑数据的那条链路。'''
        data = data or {}
        usb_port = data.get('usb_port')
        ssid = (data.get('ssid') or '').strip() or None
        password = data.get('password') or None
        if not usb_port:
            rhapi.ui.socket_send('relay_reconnect_result', {
                'ok': False, 'usb_port': usb_port, 'error': 'usb_port is required'})
            return
        if refuse_if_racing():
            rhapi.ui.socket_send('relay_reconnect_result', {
                'ok': False, 'refused': True, 'usb_port': usb_port,
                'error': 'active race'})
            return
        if not USB_LOCK.acquire(blocking=False):
            rhapi.ui.socket_send('relay_reconnect_result', {
                'ok': False, 'usb_port': usb_port, 'error': 'busy'})
            return

        def job():
            device = usb_ops.probe_one(usb_port)
            if device.get('kind') != 'relay':
                return {'ok': False,
                        'error': 'not a relay (kind={})'.format(device.get('kind'))}
            return provision.reconnect_relay(
                usb_port, gateway_nodes=gateway_watcher(device.get('mac')),
                ssid=ssid, password=password)
        spawn('relay_reconnect_result', job, {'usb_port': usb_port})

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
            'usb_environments': [
                {'name': env, 'target': firmware_check.env_target(env),
                 'desc': desc}
                for env, desc in sorted(firmware_flash.FLASH_ENVS.items())],
            'usb_settings': settings_payload(),
            'relay_ports': local_ports(),
        })

    rhapi.config.register_section(SECTION)
    # 设置页不再有本插件面板（用户裁定：唯一入口是 /usbflash 合并页）：
    # 原面板里的 5 个配置项搬到合并页「高级设置」，经 usb_save_settings 写回。
    for name, handler in [('usb_list', on_list),
                          ('usb_probe_dev', on_probe),
                          ('usb_flash', on_flash),
                          ('usb_provision', on_provision),
                          ('usb_test', on_test),
                          ('usb_save_settings', on_save_settings),
                          ('relay_refresh', on_relay_refresh),
                          ('relay_reconnect', on_relay_reconnect),
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
