#!/usr/bin/python
# -*- coding:utf-8 -*-

import sys
import os
picdir = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'sysinfo/pic')
libdir = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'sysinfo/plib')
if os.path.exists(libdir):
    sys.path.append(libdir)

import logging    
import time
import traceback
from waveshare_OLED import OLED_1in3_c
from PIL import Image,ImageDraw,ImageFont

import logging    
import socket
import fcntl  
import struct
import json

logging.basicConfig(level=logging.INFO)

RH_CONFIG_FILE = "/home/pi/RotorHazard/src/server/config.json"  # RotorHazard 配置文件地址

host_name = "None"
wifi_ip = "None"
eth_ip = "None"
userId = "None"
userPwd = "None"

def get_ip_address(ifname):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # SIOCGIFADDR
        byte_data = bytes(ifname, encoding="utf-8")
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(),0x8915,struct.pack('256s', byte_data[:15]))[20:24])
    except:
        logging.info("获取IP信息错误")
        return "None"

def initDisp():
 
    disp = OLED_1in3_c.OLED_1in3_c()
    logging.info("\r 1.3inch OLED Module (C) ")
    # Initialize library.
    disp.Init()
    # Clear display.
    logging.info("clear display")
    disp.clear()

    return disp

def  displayInfo(disp=None):
    host_name = "None"
    wifi_ip = "None"
    eth_ip = "None"
    userId = "None"
    userPwd = "None"

    # 读取.json 文件
    with open(RH_CONFIG_FILE, "r") as file:
        json_data = json.load(file)

    try:
        # 调用函数读取 ADMINUSERNAME 和 ADMIN_PASSWORD 的值

        userId = json_data['GENERAL']['ADMIN_USERNAME']
        logging.info("userId: " + userId)
        userPwd = json_data['GENERAL']['ADMIN_PASSWORD']
        logging.info("userPwd: " + userPwd)

    except json.JSONDecodeError as e:
        # 处理解析错误
        logging.info("解析 JSON 数据时出错:"+e)

    # Create blank image for drawing.
    image1 = Image.new('1', (disp.width, disp.height), "WHITE")
    draw = ImageDraw.Draw(image1)
    logging.info("***-------------")
    logging.info("*** 字体文件： " + os.path.join(picdir, "Font.ttc"))
    font1 = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), 12)
    logging.info ("***画边框***")
    draw.line([(0,0),(127,0)], fill = 0)
    draw.line([(0,0),(0,63)], fill = 0)
    draw.line([(0,63),(127,63)], fill = 0)
    draw.line([(127,0),(127,63)], fill = 0)


    logging.info ("***显示信息--开始***")
    draw.text((5, 0), "User:" + userId, font=font1, fill=0)
    draw.text((5,12), u'PWD:' + userPwd, font = font1, fill = 0)

    wifi_ip = get_ip_address("wlan0")
    logging.info("wifi_ip: " + wifi_ip)

    eth_ip = get_ip_address("eth0")
    logging.info("eth_ip: " + eth_ip)

    draw.text((5, 24), "无线:" + wifi_ip, font=font1, fill=0)
    draw.text((5, 36), "有线:" + eth_ip, font=font1, fill=0)


    logging.info ("***显示信息--结束***")
    image1=image1.rotate(180) 
    disp.ShowImage(disp.getbuffer(image1))

try:
    disp=initDisp()
    
    while True:
        logging.info("当前时间： " + str(time.time()))
        # while True:
        displayInfo(disp)
            # 等待 2 分钟
        time.sleep(120)
        disp.clear()
        
except IOError as e:
    logging.info(e)
except KeyboardInterrupt:    
    logging.info("ctrl + c:")
    disp.module_exit()
    exit()
