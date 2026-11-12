'''以太网节点注册表：D-2「发现 ≠ 接入」里"接入"那一半的持久化。

座位规则由 C-1/C-4 定死：确认即追加一块连续座位；移除只标记不接入、座位留作
占位（永不压缩、永不回收），所以重载一次也不会让已绑定飞行员的 seat 指向另一
台接收机。白名单只含已确认的 uid（D-14 默认拒绝，且永不交出 None）。
'''

import ene_protocol as enp

SECTION = 'ETHERNET_NODES'


class NodeRegistry:
    def __init__(self, config):
        self._config = config

    @property
    def enabled(self):
        return bool(self._config.get_item(SECTION, 'ENABLED'))

    @property
    def entries(self):
        return self._config.get_item(SECTION, 'NODES') or []

    @property
    def whitelist(self):
        return tuple(entry['uid'] for entry in self.entries if entry['admitted'])

    @property
    def seat_plan(self):
        return {entry['uid']: entry['first_seat'] for entry in self.entries}

    @property
    def capacity(self):
        '''占位也算容量：C-4 的占位不是空位。'''
        return max((entry['first_seat'] + entry['channels'] for entry in self.entries),
                   default=0)

    def confirm(self, uid, channels):
        if not isinstance(uid, str) or not uid:
            return False
        if not isinstance(channels, int) or not 1 <= channels <= enp.MAX_CHANNELS:
            return False
        entries = self.entries
        for entry in entries:
            if entry['uid'] == uid:
                if entry['channels'] != channels:
                    return False  # C-1：块不能就地改大小，装不下就明拒
                entry['admitted'] = True
                break
        else:
            entries.append({'uid': uid, 'channels': channels,
                            'first_seat': self.capacity, 'admitted': True})
        return self._save(entries)

    def remove(self, uid):
        entries = self.entries
        for entry in entries:
            if entry['uid'] == uid:
                entry['admitted'] = False  # C-4：保留 seat 占位
                return self._save(entries)
        return False

    def _save(self, entries):
        return bool(self._config.set_item(SECTION, 'NODES', entries))