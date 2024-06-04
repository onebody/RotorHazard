"""同步数据到云端"""

import logging
logger = logging.getLogger(__name__)
from eventmanager import Evt
from EventActions import ActionEffect
from RHUI import UIField, UIFieldType, UIFieldSelectOption
import simpleobsws
import asyncio
import asyncio_gevent
from threading import Thread, Lock

# work around for database not being setup at this point, not database variable are not used for connection
asyncio.set_event_loop_policy(asyncio_gevent.EventLoopPolicy())
loop = asyncio.get_event_loop()


async def UploadDataChange(rhapi):
    try:
        rhapi.db.options()
        logger.info("*** 同步数据到云端 ***")
            # elif recMode == '2':
            #     await ws.emit(simpleobsws.Request('StopRecord'))
    except:
        pass


class Upload_Data_Actions():
    _queue_lock = Lock()
    
    def __init__(self, rhapi):
        self._rhapi = rhapi

    def updMessageEffect(self, action, args):
        try:
            loop.run_until_complete(UploadDataChange(self._rhapi))
            logger.debug("upload data =====")
        except:
            logger.debug("Unable to change scene")

    # 比赛结束同步数据到云端
    def onPilotAlter(self, args):
        args['register_fn'](self)
        pilot_id = args['pilot_id']
        with self._queue_lock:

            if pilot_id in self._heat_data:
                pilot_settings = {}
                
                logger.info(f"Pilot {pilot_id}'s ID")
                self._heat_data[pilot_id] = pilot_settings
                logger.info(f"Pilot {pilot_id}'s UID set to {UID}")

def initialize(rhapi):
    UDA = Upload_Data_Actions(rhapi)
    rhapi.events.on(Evt.PILOT_ALTER, UDA.onPilotAlter)
