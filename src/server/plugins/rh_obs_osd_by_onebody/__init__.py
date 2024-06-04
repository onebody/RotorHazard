"""骑云 OBS OSD 输出显示 插件"""

import logging
logger = logging.getLogger(__name__)
from eventmanager import Evt
from EventActions import ActionEffect
from RHUI import UIField, UIFieldType, UIFieldSelectOption
import simpleobsws
import asyncio
import asyncio_gevent

OBSSettingFile = "./plugins/rh_obs_osd_by_onebody/settings.txt"

# work around for database not being setup at this point, not database variable are not used for connection
try:
    file = open(OBSSettingFile) 
    SettingsFileContents = file.readlines() 
    file.close()
    OBS_IP = str(SettingsFileContents[0][:-1])
    OBS_Port = str(SettingsFileContents[1][:-1])
    OBS_Password = str(SettingsFileContents[2][:-1])
    OBS_Enabled = str(SettingsFileContents[3][:-1])
    logger.info("*** OBS Server info: *** IP:"+OBS_IP+" \n Port:"+OBS_Port+" \n Pass:"+OBS_Password)
except:
    OBS_IP = "localhost"
    OBS_Port = '4462'
    OBS_Password = '123456'
    OBS_Enabled = '1'

asyncio.set_event_loop_policy(asyncio_gevent.EventLoopPolicy())
loop = asyncio.get_event_loop()
ws = simpleobsws.WebSocketClient(url = 'ws://' + OBS_IP + ':' + str(OBS_Port), password = OBS_Password)

async def OBSConnect():
    try:
        logger.info("*** Connect  OBS Server info: *** IP:"+OBS_IP+" \n Port:"+OBS_Port+" \n Pass:"+OBS_Password)
        await ws.connect()
        await ws.wait_until_identified()
        if simpleobsws.WebSocketClient.is_identified(ws):
            logger.debug("OBS Connected")
        else:
            logger.info("*** OBS failed to connect ***")
    except:
        logger.info("*** OBS Server is not active ***")

async def OBSDisconnect():
    try:
        if simpleobsws.WebSocketClient.is_identified(ws):
            await ws.disconnect()
            logger.info("OBS Disconnected")
    except:
        pass

async def OBSChange(Scene_Select, recMode):  
    is_identified = simpleobsws.WebSocketClient.is_identified(ws)

    if not is_identified:
        try:
            await ws.connect()
            await ws.wait_until_identified()
        except:
            pass
    is_identified = simpleobsws.WebSocketClient.is_identified(ws)

    try:
        if is_identified:
            if Scene_Select != '':
                await ws.emit(
                    simpleobsws.Request(
                        "SetCurrentProgramScene", {"sceneName": "4路场景"}
                    )
                )
                # if recMode == '1':
                # 设置文本源的内容
                await ws.emit(
                    simpleobsws.Request(
                        "SetInputSettings",
                        {
                            "inputName": "成绩1",
                            "inputSettings": {"text": "Lap:1  "+" 44.08"},
                        },
                    )
                )
                logger.info("*** OBS Server info: test")
            # elif recMode == '2':
            #     await ws.emit(simpleobsws.Request('StopRecord'))

    except:
        pass

class OBS_OSD_Actions():
    def __init__(self, rhapi):
        self._rhapi = rhapi

    def setSettings(self, args):
        OBS_IP = self._rhapi.db.option("obs_osd_IP")
        OBS_Port = str(self._rhapi.db.option("obs_osd_port"))
        OBS_Password = self._rhapi.db.option("obs_osd_password")
        OBS_Enabled = str(self._rhapi.db.option("obs_osd_enabled"))
        f = open(OBSSettingFile, "w")
        file_Contents = OBS_IP + "\n" + OBS_Port + "\n" + OBS_Password + "\n" + OBS_Enabled + "\n"
        f.write(file_Contents)
        f.close()
        logger.info("OBS Websocks 配置文件已更新")

    def connectToOBS(self, args):
        try:
            loop.run_until_complete(OBSConnect())
        except:
            pass

    def disconnectFromOBS(self, args):
        try:
            loop.run_until_complete(OBSDisconnect())
        except:
            pass

    def obsMessageEffect(self, action, args):
        try:
            OBS_scene = action['scene']
        except:
            OBS_scene = ''
        try:
            OBS_record = 1
        except:
            OBS_record = 0

        OBS_Enabled = str(self._rhapi.db.option("obs_osd_enabled"))

        if OBS_Enabled == '1':
            try:
                loop.run_until_complete(OBSChange(OBS_scene, OBS_record))
                logger.debug("OBS scene changed to: {}". format(OBS_scene))
            except:
                logger.debug("Unable to change scene")

    def register_handlers(self, args):
        if 'register_fn' in args:
            for effect in [
                ActionEffect(
                    "OBS OSD配置",
                    self.obsMessageEffect,
                    [
                        UIField("scene", "OBS场景", UIFieldType.TEXT),
                    ],
                )
            ]:
                args['register_fn'](effect)
                
    # 单圏结束同步数据到OSD
    def onPilotAlter(self, args):
        args["register_fn"](self)
        pilot_id = args["pilot_id"]

        if pilot_id in self._heat_data:
            pilot_settings = {}

            logger.info(f"Pilot {pilot_id}'s ID")
            self._heat_data[pilot_id] = pilot_settings


def initialize(rhapi):
    obs = OBS_OSD_Actions(rhapi)
    rhapi.events.on(Evt.STARTUP, obs.connectToOBS)
    rhapi.events.on(Evt.SHUTDOWN, obs.disconnectFromOBS)
    rhapi.events.on(Evt.ACTIONS_INITIALIZE, obs.register_handlers)

    rhapi.ui.register_panel('obs_osd_options', 'OBS OSD配置', 'settings', order=0)
    rhapi.fields.register_option(
        UIField("obs_osd_IP", "OBS IP地址", UIFieldType.TEXT), "obs_osd_options"
    )
    rhapi.fields.register_option(
        UIField("obs_osd_port", "端口", UIFieldType.BASIC_INT), "obs_osd_options"
    )
    rhapi.fields.register_option(
        UIField("obs_osd_password", "密码", UIFieldType.PASSWORD), "obs_osd_options"
    )
    rhapi.fields.register_option(
        UIField("obs_osd_enabled", "启用 OBS 功能", UIFieldType.CHECKBOX),
        "obs_osd_options",
    )
    rhapi.ui.register_quickbutton(
        "obs_osd_options",
        "osd_generate_connectin_file",
        "保存设置",
        obs.setSettings,
    )
    rhapi.ui.register_quickbutton(
        "obs_osd_options",
        "osd_connect_to_obs",
        "连接到OBS服务器",
        obs.connectToOBS,
    )
    rhapi.ui.register_quickbutton(
        "obs_osd_options",
        "osd_disconnect_from_obs",
        "断开OBS服务器",
        obs.disconnectFromOBS,
    )
