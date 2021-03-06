# Copyright (C) 2007  Matthew Neeley, Max Hofheinz
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
### BEGIN NODE INFO
[info]
name = Sampling Scope
version = 2.1
description = 

[startup]
cmdline = %PYTHON% %FILE%
timeout = 20

[shutdown]
message = 987654321
timeout = 5
### END NODE INFO
"""

import struct
import re

import numpy as np

from labrad import types as T, errors, util
from labrad.server import setting
from labrad.gpib import GPIBManagedServer, GPIBDeviceWrapper
from twisted.internet.defer import inlineCallbacks, returnValue

__QUERY__ = 'ENC WAV:BIN;BYT. LSB;OUT TRA%d;WAV?'

class NotConnectedError(errors.Error):
    """You need to connect"""
    code = 10

class InvalidChannelError(errors.Error):
    """Only channels 1 through 8 are valid"""
    code = 10

class MeasurementError(errors.Error):
    """Scope returned error"""
    code = 10

class OutofrangeError(errors.Error):
    """Signal is out of range"""
    code = 10

class SendTraceError(errors.Error):
    """StrList needs to have either 3 or 4 elements"""
    code = 11
    

TIMEOUT = 120


class SamplingScopeDevice(GPIBDeviceWrapper):
    @inlineCallbacks
    def initialize(self):
        yield self.timeout(TIMEOUT)


class SamplingScope(GPIBManagedServer):
    name = 'Sampling Scope'
    deviceName = 'Tektronix 11801C'
    deviceWrapper = SamplingScopeDevice
    deviceIdentFunc = 'identify_device'

    @setting(1000, server='s', address='s', idn='s')
    def identify_device(self, c, server, address, idn):
        if idn == '\xff':
            return self.deviceName

    @setting(10, 'Get Trace',
                 trace=[': Query TRACE1',
                        'w: Specify trace to query: 1, 2, or 3'],
                 returns=['*v: y-values', 'v: x-increment'])
    def get_trace(self, c, trace=1):
        """Returns the y-values of the current trace from the sampling scope.
        
        First element: offset time
        Second element: time step
        Third to last element: trace
        """
        dev = self.selectedDevice(c)
        if trace < 1 or trace > 3:
            raise NotConnectedError()
        yield dev.write('COND TYP:AVG')
        
        while True:
            if int((yield dev.query('COND? REMA'))[18:]) == 0:
                break
            yield util.wakeupCall(2)

        resp = yield dev.query(__QUERY__ % trace, bytes=20000L)
        ofs, incr, vals = _parseBinaryData(resp)
        returnValue([T.Value(v, 'V') for v in np.hstack(([ofs, incr], vals))])

    @setting(99, 'Multi Trace',
                 cstar='w', cend='w',
                 returns='v[s]{time offset} v[s]{time step} *2v[V]{channel}')
    def multi_trace(self, c, cstar=1, cend=2):
        """Returns the y-values of the current traces from the sampling scope in a tuple.
        (offset, timestep, 2-D array of traces)
        """
        dev = self.selectedDevice(c)
        if cstar < 1 or cstar > 8:
            raise Exception('cstar out of range')
        if cend < 1 or cend > 8:
            raise Exception('cend out of range')
        if cstar > cend:
            raise Exception('must have cend >= cstar')
        
        yield dev.write('COND TYP:AVG')
        
        while True:
            if int((yield dev.query('COND? REMA'))[18:]) == 0:
                break
            yield util.wakeupCall(2)

        resp = yield dev.query('ENC WAV:BIN;BYT. LSB;OUT TRA%dTOTRA%d;WAV?' % (cstar, cend), bytes=20000L)
        splits = resp.split(";WFMPRE")
        traces = []
        ofs = 0
        incr = 0
        for trace in splits:
            ofs, incr, vals = _parseBinaryData(trace)
            traces.append(vals)
        #ofs1, incr1, vals1 = _parseBinaryData(t1)
        #ofs2, incr2, vals2 = _parseBinaryData(t2)
        #traces = np.vstack((vals1, vals2))
        returnValue((ofs, incr, traces))
    
    @setting(241, 'Send Trace To Data Vault',
                  server=['s'], session=['*s'], dataset=['s'], trace=['w'],
                  returns=['*s s: Dataset Name'])
    def send_trace(self, c, server, session, dataset, trace=1):
        """Send the current trace to the data vault.
        """
        dev = self.selectedDevice(c)

        resp = yield dev.query(__QUERY__ % trace, bytes=20000L)
        vals = _parseBinaryData(resp)

        startx = vals[0]
        stepx = vals[1]
        vals = vals[2:]
        for _ in np.shape(vals)[:-1]:
            vals = vals[0]

        out = [[(startx + i*stepx)*1e9, d] for i, d in enumerate(vals)]
        p = self.client[server].packet()
        p.cd(session,True)
        p.new(dataset,[('time', 'ns')],[('amplitude','trace %d' % trace, 'V')])
        p.add(out)
        resp = yield p.send()
        name = resp.new
        returnValue(name)

 
    @setting(101, 'Record Length',
                  data=['w: Record Length 512, 1024, 2048 or 4096, 5120'],
                  returns=['w: Record Length'])
    def record_length(self, c, data):
        """Sets the start time of the trace."""
        dev = self.selectedDevice(c)
        yield dev.write('TBM LEN:%d' % data)
        returnValue(data)

    @setting(102, 'Mean',
                  channel=['w: Trace number', ': Trace 1'],
                  returns=['v[V]: Time average of the trace'])
    def mean(self, c, channel=1):
        dev = self.selectedDevice(c)
        s = yield dev.query('COND TYP:AVG;SEL TRA%d;COND WAIT;MEAN?' % channel)
        if s[-2:] in ['GT', 'LT', 'OR']:
            raise OutofrangeError()
        returnValue(T.Value(float(s[5:-3]), 'V'))

    @setting(103, 'Amplitude',
                  channel=['w: Trace number', ': Trace 1'],
                  returns=['v[V]: Time average of the trace'])
    def amplitude(self, c, channel=1):
        dev = self.selectedDevice(c)
        s = yield dev.query('SEL TRA%d;PP?' % channel) # 'COND TYP:AVG;SEL TRA%d;COND WAIT;AMP?'
        if s[-2:] in ['GT', 'LT', 'OR']:
            raise OutofrangeError()
        returnValue(T.Value(float(s[3:-3]), 'V'))
        
    @setting(11, 'Start Time',
                 data=['v[s]: Set Start Time',''],
                 returns=['v[s]: Start Time'])
    def start_time(self, c, data=None):
        """Sets the start time of the trace."""
        dev = self.selectedDevice(c)
        if data is not None:
            dataS = data['s']
            yield dev.write('MAINP %g' % dataS)
        resp = yield dev.query('MAINP?')
        print resp
        returnValue(data)

    @setting(12, 'Time Step',
                 data=['v[s]: Set Time Step'],
                 returns=['v[s]: Time Step'])
    def time_step(self, c, data):
        """Sets the time/div for of the trace."""
        dev = self.selectedDevice(c)
        dataS = data['s']
        yield dev.write('TBM TIM:%g' % dataS)
        returnValue(data)


    @setting(112, 'Offset',
                  data=['v[V]: Set offset (voltage at screen center)'],
                  returns=['v[V]: offset'])
    def offset(self, c, data):
        """Set offset, i.e. the voltage at the center of the screen."""
        dev = self.selectedDevice(c)
        dataV = data['V']
        yield dev.write('CHM%d OFFS:%g' % (self.getchannel(c), dataV))
        returnValue(data)


    @setting(13, 'Sensitivity',
                 data=['v[V]: Set V/div'],
                 returns=['v[V]: Sensitivity'])
    def sensitivity(self, c, data):
        """Set sensitivity (V/div)."""
        dev = self.selectedDevice(c)
        dataV = data['V']
        yield dev.write('CHM%d SENS:%g' % (self.getchannel(c), dataV))
        returnValue(data)

        

    def getchannel(self, c):
        return c.get('Channel', 1)
    

    @setting(113, 'Channel',
                  data=[': Select Channel 1',
                        'w: Channel (1 to 8)'],
                  returns=['w: Sensitivity'])
    def channel(self, c, data=1):
        """Select channel."""
        if data < 1 or data > 8:
            raise InvalidChannelError()
        c['Channel'] = data
        return data


    @setting(114, 'Average', averages=['w'], returns=['w'])
    def average(self, c, averages=1):
        """Set number of averages."""
        dev = self.selectedDevice(c)
        yield dev.write('AVG OFF')
        if averages > 1:
            yield dev.write('NAV %d' % averages)
            yield dev.write('AVG ON')
        returnValue(averages)
     
 
    @setting(14, 'trace',
                 trace=[': Attach selected channel to trace 1',
                        'w: Attach selected channel to a trace'],
                 returns=['w: Trace'])
    def trace(self, c, trace=1):
        """Define a trace."""
        dev = self.selectedDevice(c)
        yield dev.write("TRA%d DES:'M%d'" % (trace, self.getchannel(c)))
        yield dev.write('SEL TRA%d' % trace)
        returnValue(trace)

 
    @setting(15, 'Trigger Level',
                 data=['v[V]: Set trigger level'],
                 returns=['v[V]: Trigger level'])
    def trigger_level(self, c, data):
        """Set trigger level."""
        dev = self.selectedDevice(c)
        dataV = data['V']
        yield dev.write('TRI LEV:%g' % dataV)
        returnValue(data)
   
    @setting(16, 'Trigger positive', returns=[''])
    def trigger_positive(self, c):
        """Trigger on positive slope."""
        dev = self.selectedDevice(c)
        yield dev.write('TRI SLO:PLU')
          
    @setting(17, 'Trigger negative', returns=[''])
    def trigger_negative(self, c):
        """Trigger on negative slope."""
        dev = self.selectedDevice(c)
        yield dev.write('TRI SLO:NEG')

    @setting(18, 'Reset', returns=[''])
    def reset(self, c):
        """Reset to default state."""
        dev = self.selectedDevice(c)
        yield dev.write('INI')

        
_xzero = re.compile('XZERO:(-?\d*.?\d+E?-?\+?\d*),')
_xincr = re.compile('XINCR:(-?\d*.?\d+E?-?\+?\d*),')
_yzero = re.compile('YZERO:(-?\d*.?\d+E?-?\+?\d*),')
_ymult = re.compile('YMULT:(-?\d*.?\d+E?-?\+?\d*),')
    
def _parseBinaryData(data):
    """Parse the data coming back from the scope"""
    hdr, dat = data.split(';CURVE')
    dat = dat[dat.find('%')+3:-1]
    dat = np.array(struct.unpack('h'*(len(dat)/2), dat))
    xzero = float(_xzero.findall(hdr)[0])
    xincr = float(_xincr.findall(hdr)[0])
    yzero = float(_yzero.findall(hdr)[0])
    ymult = float(_ymult.findall(hdr)[0])

    return xzero, xincr, dat*ymult + yzero


__server__ = SamplingScope()

if __name__ == '__main__':
    from labrad import util
    util.runServer(__server__)
