"""骑云 数据同步 插件"""

import logging
logger = logging.getLogger(__name__)
# import RHUtils
import json
from sqlalchemy.ext.declarative import DeclarativeMeta
from sqlalchemy import inspect
from data_export import DataExporter
from eventmanager import Evt

from RHUI import UIField, UIFieldType, UIFieldSelectOption

import requests
from flask import templating
from flask.blueprints import Blueprint

def register_handlers(args):
    if 'register_fn' in args:
        for exporter in discover():
            args['register_fn'](exporter)

def initialize(rhapi):
    # 初始数据
    rhapi.fields.register_pilot_attribute( UIField('safetycheck', "Safety Checked", UIFieldType.CHECKBOX) )
    rhapi.fields.register_pilot_attribute( UIField('fpvs_uuid', "FPVS Pilot UUID", UIFieldType.TEXT) )

    rhapi.ui.register_panel("fpvscores_run", "FPV Scores", "run")

    rhapi.ui.register_quickbutton(
        "fpvscores_run", "fpvscores_clear", "清除数据", runClearBtn, {"rhapi": rhapi}
    )

    rhapi.ui.register_quickbutton("fpvscores_run", "fpvscores_upload_run", "上传数据", runUploadBtn, {'rhapi': rhapi})

    rhapi.events.on(Evt.DATA_EXPORT_INITIALIZE, register_handlers)

def write_json(data):
    payload = json.dumps(data, indent='\t', cls=AlchemyEncoder)

    return {
        'data': payload,
        'encoding': 'application/json',
        'ext': 'json'
    }

def runUploadBtn(args):
    logger.info("---开始上传比赛数据.")
    args['rhapi'].ui.message_notify(args['rhapi'].__('开始上传比赛数据...'))
    data = args['rhapi'].io.run_export('JSON_FPVScores_Upload')
    logger.info(data)
    uploadToFPVS_frombtn(args, data)
    logger.info("开始上传比赛数据.------")

## 清空数据
def runClearBtn(args):
    logger.info('---开始清除数据')
    args['rhapi'].ui.message_notify(args['rhapi'].__('Clear event data request has been send.'))
    url = "http://localhost/rh/?action=rh_clear"
    json_data = '{"event_uuid":"' + args['rhapi'].db.option('event_uuid') + '"}'
    headers = {'Authorization' : 'rhconnect', 'Accept' : 'application/json', 'Content-Type' : 'application/json'}
    r = requests.post(url, data=json_data, headers=headers)
    if r.status_code == 200:
        if r.text == 'Data Cleared':
            args['rhapi'].ui.message_notify(args['rhapi'].__('赛程数据已清除.'))
        else:
            args['rhapi'].ui.message_notify(r.text)
    logger.info("开始清除数据-------")


## 数据上传
def uploadToFPVS_frombtn(args, input_data):
    logger.info('----数据上传')   
    json_data =  input_data['data']
    url = 'http://localhost/rh/?action=mgp_push'
    headers = {'Authorization' : 'rhconnect', 'Accept' : 'application/json', 'Content-Type' : 'application/json'}
    r = requests.post(url, data=json_data, headers=headers)
    logger.info(r.status_code)
    logger.info(r.text)
    if r.status_code == 200:
        if r.text == 'import succesfull':
            args['rhapi'].ui.message_notify(args['rhapi'].__('数据上传成功！'))
        else:
            args['rhapi'].ui.message_notify(r.text)       
    logger.info("数据上传-----")

def assemble_fpvscoresUpload(rhapi):
    payload = {}
    payload['import_settings'] = 'upload_FPVScores'
    payload['Pilot'] = assemble_pilots_complete(rhapi)
    payload['Heat'] = assemble_heats_complete(rhapi)
    payload['HeatNode'] = assemble_heatnodes_complete(rhapi)
    payload['RaceClass'] = assemble_classes_complete(rhapi)
    payload['GlobalSettings'] = assemble_settings_complete(rhapi)
    payload['FPVScores_results'] = rhapi.eventresults.results

    return payload

def discover(*args, **kwargs):
    # returns array of exporters with default arguments
    return [
        DataExporter(
            'JSON FPVScores Upload',
            write_json,
            assemble_fpvscoresUpload
        )
    ]

def assemble_results_raw(RaceContext):
    payload = RaceContext.pagecache.get_cache()
    return payload


def assemble_pilots_complete(rhapi):
    payload = rhapi.db.pilots
    for pilot in payload:
        pilot.fpvsuuid = rhapi.db.pilot_attribute_value(pilot.id, 'fpvs_uuid')
        # pilot.country = rhapi.db.pilot_attribute_value(pilot.id, 'country')
    return payload


def assemble_heats_complete(rhapi):
    payload = rhapi.db.heats
    return payload

def assemble_heatnodes_complete(rhapi):
    payload = rhapi.db.slots
    
    freqs = json.loads(rhapi.race.frequencyset.frequencies)
    
    for slot in payload:
        if slot.node_index is not None and isinstance(slot.node_index, int):
            slot.node_frequency_band = freqs['b'][slot.node_index] if len(freqs['b']) > slot.node_index else ' '
            slot.node_frequency_c = freqs['c'][slot.node_index] if len(freqs['c']) > slot.node_index else ' '
            slot.node_frequency_f = freqs['f'][slot.node_index] if len(freqs['f']) > slot.node_index else ' '
        else:
            # Als slot.node_index None is of geen integer, gebruik dan een lege string als de waarde
            slot.node_frequency_band = ' '
            slot.node_frequency_c = ' '
            slot.node_frequency_f = ' '
        
    return payload

def assemble_classes_complete(rhapi):
    payload = rhapi.db.raceclasses
    return payload

def assemble_formats_complete(rhapi):
    payload = rhapi.db.raceformats
    return payload

def assemble_racemeta_complete(rhapi):
    payload = rhapi.db.races
    return payload

def assemble_pilotrace_complete(rhapi):
    payload = rhapi.db.pilotruns
    return payload

def assemble_racelap_complete(rhapi):
    payload = rhapi.db.laps
    return payload

def assemble_profiles_complete(rhapi):
    payload = rhapi.db.frequencysets
    return payload

def assemble_settings_complete(rhapi):
    payload = rhapi.db.options
    return payload

class AlchemyEncoder(json.JSONEncoder):
    def default(self, obj):  #pylint: disable=arguments-differ
        custom_vars = ['fpvsuuid','country','node_frequency_band','node_frequency_c','node_frequency_f']
        if isinstance(obj.__class__, DeclarativeMeta):
            # an SQLAlchemy class
            mapped_instance = inspect(obj)
            fields = {}
            for field in dir(obj): 
                if field in [*mapped_instance.attrs.keys(), *custom_vars]:
                    data = obj.__getattribute__(field)
                    if field != 'query' \
                        and field != 'query_class':
                        try:
                            json.dumps(data) # this will fail on non-encodable values, like other classes
                            if field == 'frequencies':
                                fields[field] = json.loads(data)
                            elif field == 'enter_ats' or field == 'exit_ats':
                                fields[field] = json.loads(data)
                            else:
                                fields[field] = data
                        except TypeError:
                            fields[field] = None

            # a json-encodable dict
            return fields

        return json.JSONEncoder.default(self, obj)
