#!/usr/bin/env python
import serial
import xively
import requests
import smbus
import schedule
import re
import datetime
import traceback
import time
import pickle
import math
import numpy
from flask import Flask, request
from flask.ext.restful import Resource, Api
import threading
import metoffer


# Scheduler setup
def bathwarm():
    if outside.value<18:
        bath.dutycycle = 1
    else:
        bath.dutycycle = 0
def bathcold():
    bath.dutycycle = 0
def diningwarm():
    dining.target = 20
def diningcold():
    dining.target = 18
def openallvalves():
    for r in iter(Room.rooms):
        r.dutycycle = 1
def submitday():
    for r in iter(Room.rooms):
        xiv.queue(r.name + '_dayenergy', round(r.dayenergy))
        r.dayenergy=0
    xiv.queue('outside_daytemp', round(outside.sum/outside.count,2))
    outside.sum=0
    outside.count=0


class State:
    def __init__(self):
        self.INTEGRALS = '/home/pi/integral.pckl'

    def load(self):
        try:
            print 'Reading saved integrals:'
            f = open(self.INTEGRALS, 'r')
            for room in iter(Room.rooms):
                room.integral = pickle.load(f)
                room.dutycycle=room.integral
                room.dayenergy= pickle.load(f)
                print '%-9s i=%6.3f de=%f' % (room.name, room.integral, room.dayenergy)
            outside.sum=pickle.load(f)
            outside.count=pickle.load(f)
            print outside.sum,outside.count
            f.close()

        except IOError:
            print "no integrals saved, using default 0"

    def save(self):
        try:
            f = open(self.INTEGRALS, 'w')
            for room in iter(Room.rooms):
                pickle.dump(room.integral, f)
                pickle.dump(room.dayenergy,f)
            pickle.dump(outside.sum,f)
            pickle.dump(outside.count,f)
            f.close()
        except IOError:
            print 'could not write integrals'

class Weather:
    def __init__(self):
        self.apikey="75e85110-6b81-4da3-9336-efd5e9dd4c05"
        self.M = metoffer.MetOffer(self.apikey)
        self.data=[]
        self.temps=[]
        self.times=[]
        self.types=[]
        self.futuretemp=[]
        self.futuregrad=[]
        self.futuretype=[]
    def getforecast(self):
        # 354077 is ID for Wantage
        try:
            self.data=metoffer.parse_val(self.M.loc_forecast('354077',metoffer.THREE_HOURLY)).data
        except:
            print datetime.datetime.now().ctime()
            traceback.print_exc()

        self.temps=[t['Temperature'][0] for t in self.data]
        self.times=[(t['timestamp'][0]-datetime.datetime.now()).total_seconds() for t in self.data]
        self.types=[t['Weather Type'][0] for t in self.data]
       # 0 clear night, 1 sunny day, 3 partially cloudy day, 7 cloudy
        f=numpy.interp([3600*5,3600*6],self.times,self.temps)
        self.futuretemp=f[0]
        self.futuregrad=f[1]-f[0]
        self.futuretype=self.types[(numpy.abs(numpy.array(self.times)-3600*5)).argmin()]
        xiv.queue('future_temp', round(float(self.futuretemp),2))
        xiv.queue('future_grad', round(float(self.futuregrad),2))
        xiv.queue('future_type', self.futuretype)



class Output:
    def __init__(self):
        # SMBus setup
        self.bus = smbus.SMBus(1)
        self.RELAYADDR = 0x20
        self.OUTLATCH = 0x14
        self.IODIR = 0x00
        # set all bits to off (inverted by the relays)
        self.bus.write_byte_data(self.RELAYADDR, self.OUTLATCH, 0xff)
        # set all bits to output (defaults to input)
        self.bus.write_byte_data(self.RELAYADDR, self.IODIR, 0x00)
        # fifo for delay of pump action. 9s per iteration, so 20 is 180s
        self.FIFOLEN = 20
        self.FIFO = [False] * self.FIFOLEN
        self.valves=''

    def pwm(self):
        outbyte = 0
        outstr = ''
        for r in iter(Room.rooms):
            r.control()
            outbyte += (2 ** (7 - r.ufhc)) * int(r.valve)
            if r.valve:
                outstr += r.name[0].upper()
            else:
                outstr += r.name[0].lower()
        self.FIFO.append(outbyte > 0)
        pump = self.FIFO.pop(0)
        outbyte += 3 * int(pump)  # 3 for the bottom two bits
        if pump:
            outstr += ' P'

        self.valves=outstr
        self.bus.write_byte_data(self.RELAYADDR, self.OUTLATCH, 0xff ^ outbyte)  # invert for relay card

class Xiv:
    def __init__(self):
        
        # Xively setup
        self.CELSIUS = xively.Unit(label='Celsius', type='derivedSI', symbol=u'\xb0C')
        self.VOLT = xively.Unit(label='Volt', type='derivedSI', symbol='V')
        self.PERCENT = xively.Unit(label='Percent', type='derivedSI', symbol='%')
        self.JOULE = xively.Unit(label='Joule', type='derivedSI', symbol='J')
        self.GRAD = xively.Unit(label='Celsius/Hour', type='derivedSI', symbol=u'\xb0C/h')
        self.TYPE = xively.Unit(label='Type', type='', symbol='')


        # magic number from xively API setup
        self.api = xively.XivelyAPIClient("caU2NhYBH5DbP6iuugl2mLcPHAqLiu4WYj27uqrW0rZAiDS2")
        self.feed = self.api.feeds.get(990209021)
        self.data_list = []

    def queue(self, k, v):
        if v!=[]:
            now = datetime.datetime.utcnow()
            if 'temp' in k:
                u = self.CELSIUS
            elif 'batt' in k:
                u = self.VOLT
            elif 'dutycycle' in k:
                u = self.PERCENT
            elif 'energy' in k:
                u = self.JOULE
            elif 'grad' in k:
                u = self.GRAD
            elif 'type' in k:
                u = self.TYPE

            self.data_list.append(xively.Datastream(id=k, unit=u, current_value=v, at=now))

    def send(self):
        # send all updates to xively
        self.feed.datastreams = self.data_list
        try:
            self.feed.update()
        except:
            print datetime.datetime.now().ctime()
            traceback.print_exc()
        self.data_list = []

class W1:
    devices = []

    def __init__(self, addr, name):
        self.name = name
        self.addr = addr
        self.value = []
        self.devices.append(self)

    def read(self):
        try:
            w1f = open('/sys/bus/w1/devices/w1_bus_master1/' + self.addr + '/w1_slave')
            readback = w1f.read()
            w1f.close()
            self.value = float(readback.split()[-1][2:]) / 1000
            xiv.queue(self.name, self.value)
        except KeyboardInterrupt:
            raise
        except:
            print datetime.datetime.now().ctime(),'could not open',self.addr
            #traceback.print_exc()
            self.value=0
            time.sleep(1)
            pass

class XRF:
    def __init__(self):
        self.XRFMAP = {} 
        self.ser=serial.Serial('/dev/ttyAMA0', 9600)

    def add(self, place, xrfc):
        if xrfc is not None:
            self.XRFMAP['T%dTMPA' % xrfc] = place.temp
            self.XRFMAP['T%dBATT' % xrfc] = place.batt
    def receive(self):
        # check for and read message(s) from XRF devices, parse message,
        # chose action from XRFMAP
        n = self.ser.inWaiting()
        if n != 0:
            message = self.ser.read(n)
            for key, value in re.findall("a([0-9A-Z]{6})([-+]?\d+.\d+)", message):
                if key in self.XRFMAP:
                    self.XRFMAP[key](float(value))

class Outside:
    def __init__(self, xrfc):
        self.value = 0
        self.sum = 0
        self.count =0
        xrf.add(self, xrfc)

    def temp(self, v):
        self.value = v
        self.sum += v
        self.count += 1
        xiv.queue('outside_temp', v)

    def batt(self, v):
        xiv.queue('outside_batt', v)

class Room:
    rooms = []
    names = []

    def __str__(self):
        return 'Room %s' % self.name
    def __repr__(self):
        return '<room %s>' % self.name

    def __init__(self, name, ufhc, xrfc, target, rettemp, kp=.5, ki=15e-3, kd=0, ko=0, flow=1):
        self.name = name
        self.ufhc = ufhc
        self.target = target
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.ko = ko
        self.thistory = []
        self.temperature = 0
        self.vfifo = [False] * (output.FIFOLEN + 1)
        self.lasttime = time.time()
        self.integral = 0
        self.dutycycle = 0
        self.control_frac = 0
        self.rettemp = rettemp
        self.valve = False
        self.rooms.append(self)
        self.names.append(name)
        self.energy = 0
        self.dayenergy = 0
        self.flow = flow
        xrf.add(self, xrfc)

    def control(self):
        CYCLE = 900
        MINDUTY = .025
        mult = math.ceil(.2 / (self.dutycycle + 1e-6))
        dutyfrac = time.time() / (CYCLE * mult) % 1
        self.valve = ((self.dutycycle >= dutyfrac) and (self.dutycycle > MINDUTY))
        self.vfifo.append(self.valve)
        cv=self.vfifo[1]
        pv=self.vfifo.pop(0)
        if cv:# current valve state
            if pv:# previous valve state
                # valve open
                # 70W at 1 l/min
                self.energy += self.flow * (hot_in.value - self.rettemp.value) * (time.time()-self.lasttime) * 70
            else:
                # valve opening
                self.energy = 0


        frac = time.time() / CYCLE % 1
        if frac <= self.control_frac:
            xiv.queue(self.name + '_energy', round(self.energy))
            self.dayenergy += self.energy
            self.energy = 0
            if len(self.thistory) > 1:

                p = numpy.polyfit(range(1 - len(self.thistory), 1), self.thistory, 1)
                diff = p[0]
                error = self.target - p[1]
                self.thistory = []
                self.integral += error * self.ki
                self.dutycycle = self.kp * error + self.integral + self.kd * diff
                if self.dutycycle > 1:
                    self.dutycycle = 1
                    self.integral -= error * self.ki
                elif self.dutycycle < MINDUTY:
                    self.dutycycle = 0
                    self.integral -= error * self.ki

                print '%-9s t=%5.2f e=%5.2f p=%5.2f i=%4.3f d=%7.4f pid=%4.2f' % (
                self.name, self.target, error, self.kp * error, self.integral, self.kd * diff, self.dutycycle)
                xiv.queue(self.name + '_dutycycle', round(self.dutycycle * 100, 2))
                xiv.queue(self.name + '_dutycycle_I', round(self.integral * 100, 2))


        self.control_frac = frac
        self.lasttime = time.time()

    def temp(self, v):
        self.thistory.append(v)
        self.temperature=v
        xiv.queue(self.name + '_temp', v)

    def batt(self, v):
        xiv.queue(self.name + '_batt', v)

class WebClass(Resource):
    def get(self, classname):
        d=dir(globals()[classname])
        return  [s for s in d if s[:2] != '__']

class WebClassVar(Resource):
    def get(self, classname, varname):
        result = getattr(globals()[classname], varname)
        if callable(result):
            result = result()
        return {varname: result}

    def put(self, classname, varname):
        setattr(globals()[classname], varname, float(request.form['data']))
        return {varname: getattr(globals()[classname], varname)}

class WebClassVarChange(Resource):

    def put(self, classname, varname, action):
        if action=="add":
            setattr(globals()[classname], varname, getattr(globals()[classname], varname)+float(request.form['data']))
        return {varname: getattr(globals()[classname], varname)}

def ufhloop():
    while True:
        schedule.run_pending()
        xrf.receive()
        for d in iter(W1.devices):
            d.read()
        output.pwm()
        xiv.send()
        state.save()

weather=Weather()

schedule.every().minute.do(weather.getforecast)
schedule.every().day.at("17:00").do(bathwarm)
schedule.every().day.at("18:00").do(bathcold)
schedule.every().day.at("16:57").do(openallvalves)
schedule.every().day.at("10:59").do(diningwarm)
schedule.every().day.at("17:59").do(diningcold)
schedule.every().day.at("00:01").do(submitday)


state=State()
output=Output()
xiv=Xiv()
xrf=XRF()


living_ret = W1('28-000006099047', 'living_return_temp')
dining_ret = W1('28-0000060ad896', 'dining_return_temp')
bath_ret = W1('28-00000609076d', 'bath_return_temp')
hall_ret = W1('28-0000060aea96', 'hall_return_temp')
kitchen_ret = W1('28-000006095636', 'kitchen_return_temp')
snug_ret = W1('28-0000060afb69', 'snug_return_temp')
hot_in = W1('28-0000060879e4', 'hot_in_temp')
ret = W1('28-00000607f562', 'return_temp')
boiler = W1('28-000006087f7b', 'boiler_temp')
# spare=('28-0000060ae520', 'spare', None)

outside = Outside(1)

#         Room(name,ufhc,xrf,target,rettemp,kp=.5,ki=15e-3,kd=0,ko=0)
living =  Room('living', 0, 2, 20.5, living_ret, flow=3)
dining =  Room('dining', 1, 4, 18, dining_ret)
bath =    Room('bath', 2, None, 0, bath_ret)
hall =    Room('hall', 3, 3, 19.75, hall_ret)
kitchen = Room('kitchen', 4, 6, 20, kitchen_ret, flow=3)
snug =    Room('snug', 5, 5, 19.75, snug_ret, ki=7.5e-3)

state.load()
# start the UFH feedback in a thread
th = threading.Thread(target = ufhloop)
th.daemon = True
th.start()

# instantiate a Flask RESTful and run
app = Flask(__name__)
api = Api(app)
api.add_resource(WebClass, '/<string:classname>')
api.add_resource(WebClassVar, '/<string:classname>/<string:varname>')
api.add_resource(WebClassVarChange, '/<string:classname>/<string:varname>/<string:action>')
app.run(host='0.0.0.0')
