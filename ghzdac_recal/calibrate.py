# Copyright (C) 2007  Max Hofheinz 
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

# This module contains the calibration scripts. They must not require any
# user interaction because they are used not only for the initial
# recalibration but also for recalibration. The user interface is provided
# by GHz_DAC_calibrate in the "scripts" package. Eventually async versions
# will be provide for use in a LABRAD server

from ghzdac_recal import SESSIONNAME, ZERONAME, PULSENAME, CHANNELNAMES, \
     IQNAME, SETUPTYPESTRINGS, IQcorrector
from numpy import exp, pi, arange, real, imag, min, max, log, transpose, alen
from labrad.types import Value
from datetime import datetime
from twisted.internet.defer import inlineCallbacks, returnValue
#trigger to be set:
#0x1: trigger S0
#0x2: trigger S1
#0x4: trigger S2
#0x8: trigger S3
#e.g. 0xA sets trigger S1 and S3
trigger = 0xFL << 28

DACMAX= 1 << 13 - 1
DACMIN= 1 << 13
PERIOD = 200
SBFREQUNIT = 1.0/PERIOD

@inlineCallbacks
def spectInit(spec):
    yield spec.gpib_write(':POW:RF:ATT 0dB\n:AVER:STAT OFF\n:BAND 300Hz\n:FREQ:SPAN 100Hz\n:INIT:CONT OFF\n')

@inlineCallbacks     
def spectFreq(spec,freq):
    yield spec.gpib_write(':FREQ:CENT %gGHz\n' % freq)

@inlineCallbacks     
def signalPower(spec):
    """returns the mean power in mW read by the spectrum analyzer"""
    dBs = yield spec.gpib_query('*TRG\n*OPC?\n:TRAC:MATH:MEAN? TRACE1\n')
    returnValue(10.0**(0.1*float(dBs[2:])))


def makeSample(a,b):
    """computes sram sample from dac A and B values"""
    if (max(a) > 0x1FFF) or (max(b) > 0x1FFF) or (min(a) < -0x2000) or (min(b) < -0x2000):
        print 'DAC overflow'
    return long(a & 0x3FFFL) | (long(b & 0x3FFFL) << 14)

@inlineCallbacks     
def measurePower(spec,fpga,a,b):
    """returns signal power from the spectrum analyzer"""
    dac=[makeSample(a,b)]*64
    dac[0] |= trigger
    yield fpga.run_sram(dac,True)
    returnValue((yield signalPower(spec)))

def datasetNumber(dataset):
    return int(dataset[1][:5])

def minPos(l,c,r):
    """Calculates minimum of a parabola to three equally spaced points.
    The return value is in units of the spacing relative to the center point.
    It is bounded by -1 and 1.
    """
    d=l+r-2.0*c
    if d <= 0:
        return 0
    d=0.5*(l-r)/d
    if d>1:
        d=1
    if d<-1:
        d=-1
    return d


####################################################################
# DAC zero calibration                                             #
####################################################################

  
@inlineCallbacks 
def zero(anr, spec, fpga, freq):
    """Calibrates the zeros for DAC A and B using the spectrum analyzer"""
   
    yield anr.frequency(Value(freq,'GHz'))
    yield spectFreq(spec,freq)
    a=0
    b=0
    precision=0x800
    print '    calibrating at %g GHz...' % freq
    while precision > 0:
        al = yield measurePower(spec,fpga,a-precision,b)
        ar = yield measurePower(spec,fpga,a+precision,b)
        ac = yield measurePower(spec,fpga,a,b)
        corra=long(round(precision*minPos(al,ac,ar)))
        a+=corra

        bl = yield measurePower(spec,fpga,a,b-precision)
        br = yield measurePower(spec,fpga,a,b+precision)
        bc = yield measurePower(spec,fpga,a,b)
        corrb=long(round(precision*minPos(bl,bc,br)))
        b+=corrb
        optprec=2*max([abs(corra),abs(corrb)]) 
        precision/=2
        if precision>optprec:
            precision=optprec
        print '        a = %4d  b = %4d uncertainty : %4d, power %6.1f dBm' % \
              (a, b, precision, 10 * log(bc) / log(10.0))
    returnValue([a,b])

@inlineCallbacks
def zeroScanCarrier(cxn, scanparams, boardname):
    """Measures the DAC zeros in function of the carrier frequency."""
    fpga = cxn.ghz_dacs
    anr = cxn.anritsu_server
    spec = cxn.spectrum_analyzer_server
    scope = cxn.sampling_scope

    yield anr.amplitude(Value(scanparams['anritsu dBm'],'dBm'))
    yield anr.output(True)

    print 'Zero calibration from %g GHz to %g GHz in steps of %g GHz...' % \
        (scanparams['carrierMin'],scanparams['carrierMax'],scanparams['carrierStep'])

    ds = cxn.data_vault
    yield ds.cd(['',SESSIONNAME,boardname],True)
    dataset = yield ds.new(ZERONAME,
                           [('Frequency','GHz')],
                           [('DAC zero', 'A', 'clics'),
                            ('DAC zero', 'B', 'clics')])
    yield ds.add_parameter('Anritsu amplitude',
                     Value(scanparams['anritsu dBm'],'dBm'))

    freq=scanparams['carrierMin']
    while freq<scanparams['carrierMax']:
        yield ds.add([freq]+(yield zero(anr,spec,fpga,freq)))
        freq+=scanparams['carrierStep']
    returnValue(int(dataset[1][:5]))
                
####################################################################
# Pulse calibration                                                #
####################################################################

@inlineCallbacks
def measureImpulseResponse(fpga, scope, baseline, pulse, dacoffsettime=6):
    """Measure the response to a DAC pulse
    fpga: connected fpga server
    scope: connected scope server
    dac: 'a' or 'b'
    returns: list
    list[0] : start time (s)
    list[1] : time step (s)
    list[2:]: actual data (V)
    """
    #units clock cycles

    triggerdelay=30
    looplength=256
    pulseindex=(triggerdelay-dacoffsettime) % looplength
    yield scope.start_time(Value(triggerdelay,'ns'))
    #calculate the baseline voltage by capturing a trace without a pulse

    data = looplength * [baseline]
    data[0] |= trigger
    yield fpga.run_sram(data,True)

    data[pulseindex] = pulse | (trigger * (pulseindex == 0))
    yield fpga.run_sram(data,True)
    data = (yield scope.get_trace(1)).asarray
    data[0]-=triggerdelay*1e-9
    returnValue(data)

@inlineCallbacks
def calibrateACPulse(cxn, scanparams, boardname, setupType, baselineA, baselineB):
    """Measures the impulse response of the DACs after the IQ mixer"""
    pulseheight=0x1800

    anr = yield cxn.anritsu_server
    yield anr.frequency(Value(scanparams['carrier'],'GHz'))
    yield anr.amplitude(Value(scanparams['anritsu dBm'],'dBm'))
    yield anr.output(True)
    
    fpga = cxn.ghz_dacs
 
    #Set up the scope
    scope = cxn.sampling_scope
    p = scope.packet().\
    reset().\
    channel(1).\
    trace(1).\
    record_length(5120).\
    average(128).\
    sensitivity(Value(10.0,'mV')).\
    offset(Value(0,'mV')).\
    time_step(Value(2,'ns')).\
    trigger_level(Value(0.18,'V')).\
    trigger_positive()
    yield p.send()

    baseline = makeSample(baselineA,baselineB)
    print "Measuring offset voltage..."
    offset = (yield measureImpulseResponse(fpga, scope, baseline, baseline))[2:]
    offset = sum(offset) / len(offset)

    print "Measuring pulse response DAC A..."
    traceA = yield measureImpulseResponse(fpga, scope, baseline,
        makeSample(baselineA+pulseheight,baselineB),
        dacoffsettime=scanparams['dacOffsetTimeIQ'])

    print "Measuring pulse response DAC B..."
    traceB = yield measureImpulseResponse(fpga, scope, baseline,
        makeSample(baselineA,baselineB+pulseheight),
        dacoffsettime=scanparams['dacOffsetTimeIQ'])

    starttime = traceA[0]
    timestep = traceA[1]
    if (starttime != traceB[0]) or (timestep != traceB[1]) :
        print """Time scales are different for measurement of DAC A and B.
        Did you change settings on the scope during the measurement?"""
        exit
    #set output to zero    
    yield fpga.run_sram([baseline]*4)
    ds = cxn.data_vault
    yield ds.cd(['',SESSIONNAME,boardname],True)
    dataset = yield ds.new(PULSENAME,[('Time','ns')],
                           [('Voltage','A','V'),('Voltage','B','V')])
    yield ds.add_parameter('Setup type', SETUPTYPESTRINGS[setupType])
    yield ds.add_parameter('Anritsu frequency',
                     Value(scanparams['carrier'],'GHz'))
    yield ds.add_parameter('Anritsu amplitude',
                     Value(scanparams['anritsu dBm'],'dBm'))
    yield ds.add_parameter('DAC offset time',
                           Value(scanparams['dacOffsetTimeIQ'],'ns'))
    yield ds.add(transpose([1e9*(starttime+timestep*arange(alen(traceA)-2)),
        traceA[2:]-offset,
        traceB[2:]-offset]))
    returnValue(datasetNumber(dataset))

@inlineCallbacks
def calibrateDCPulse(cxn,scanparams,boardname,channel):
    fpga = cxn.ghz_dacs

    dac_baseline = -0x2000
    dac_pulse=0x1FFF
    dac_neutral = 0x0000
    if channel:
        pulse = makeSample(dac_neutral,dac_pulse)
        baseline = makeSample(dac_neutral, dac_baseline)
    else:
        pulse = makeSample(dac_pulse, dac_neutral)
        baseline = makeSample(dac_baseline, dac_neutral)
    #Set up the scope
    scope = cxn.sampling_scope
    p = scope.packet().\
    reset().\
    channel(1).\
    trace(1).\
    record_length(5120).\
    average(128).\
    sensitivity(Value(100.0,'mV')).\
    offset(Value(0,'mV')).\
    time_step(Value(2,'ns')).\
    trigger_level(Value(0.18,'V')).\
    trigger_positive()
    yield p.send()
    
    print 'Measuring offset voltage...'
    offset = (yield measureImpulseResponse(fpga, scope, baseline, baseline,
        dacoffsettime=scanparams['dacOffsetTimeNoIQ']))[2:]
    offset = sum(offset) / len(offset)

    print 'Measuring pulse response...'
    trace = yield measureImpulseResponse(fpga, scope, baseline, pulse,
        dacoffsettime=scanparams['dacOffsetTimeNoIQ'])
    yield fpga.run_sram([makeSample(neutral, neutral)]*4,False)
    ds = cxn.data_vault
    yield ds.cd(['',SESSIONNAME,boardname],True)
    dataset = yield ds.new(CHANNELNAMES[channel],[('Time','ns')],
                           [('Voltage','','V')])
    yield ds.add_parameter('DAC offset time',
                  Value(scanparams['dacOffsetTimeNoIQ'],'ns'))
    yield ds.add(transpose([1e9*(trace[0]+trace[1]*arange(alen(trace)-2)),
        trace[2:]-offset]))
    returnValue(datasetNumber(dataset))


####################################################################
# Sideband calibration                                             #
####################################################################

@inlineCallbacks 
def measureOppositeSideband(spec, fpga, corrector,
                            carrierfreq, sidebandfreq, compensation):
    """Put out a signal at carrierfreq+sidebandfreq and return the power at
    carrierfreq-sidebandfreq"""

    arg=-2.0j*pi*sidebandfreq*arange(PERIOD)
    signal=corrector.DACify(carrierfreq,
                            0.5 * exp(arg) + 0.5 * compensation * exp(-arg), \
                            loop=True, iqcor=False, rescale=True)
    signal[0] = signal[0] | trigger
    yield fpga.run_sram(signal,True)
    returnValue((yield signalPower(spec)) / corrector.last_rescale_factor)

@inlineCallbacks 
def sideband(anr,spect,fpga,corrector,carrierfreq,sidebandfreq):
    """When the IQ mixer is used for sideband mixing, imperfections in the
    IQ mixer and the DACs give rise to a signal not only at
    carrierfreq+sidebandfreq but also at carrierfreq-sidebandfreq.
    This routine determines amplitude and phase of the sideband signal
    for carrierfreq-sidebandfreq that cancels the undesired sideband at
    carrierfreq-sidebandfreq.""" 
    if abs(sidebandfreq) < 3e-5:
        returnValue(0.0j)
    yield anr.frequency(Value(carrierfreq,'GHz'))
    comp=0.0j
    precision=1.0
    yield spectFreq(spect,carrierfreq-sidebandfreq)
    while precision > 2.0**-14:
        lR = yield measureOppositeSideband(spect, fpga, corrector, carrierfreq,
                                           sidebandfreq, comp - precision)
        rR = yield measureOppositeSideband(spect, fpga, corrector, carrierfreq,
                                           sidebandfreq, comp + precision)
        cR = yield measureOppositeSideband(spect, fpga, corrector, carrierfreq,
                                           sidebandfreq, comp)
        
        corrR = precision * minPos(lR,cR,rR)
        comp += corrR
        lI = yield measureOppositeSideband(spect, fpga, corrector, carrierfreq,
                                           sidebandfreq, comp - 1.0j * precision)
        rI = yield measureOppositeSideband(spect, fpga, corrector, carrierfreq,
                                           sidebandfreq, comp + 1.0j * precision)
        cI = yield measureOppositeSideband(spect, fpga, corrector, carrierfreq,
                                           sidebandfreq, comp)
        
        corrI = precision * minPos(lI,cI,rI)
        comp += 1.0j * corrI
        precision=min([2.0 * max([abs(corrR),abs(corrI)]), precision / 2.0])
        print '      compensation: %.4f%+.4fj +- %.4f, opposite sb: %6.1f dBm' % \
            (real(comp), imag(comp), precision, 10.0 * log(cI) / log(10.0))
    returnValue(comp)

@inlineCallbacks
def sidebandScanCarrier(cxn, scanparams, boardname, corrector):
    """Determines relative I and Q amplitudes by canceling the undesired
       sideband at different sideband frequencies."""

    fpga=cxn.ghz_dacs
    anr=cxn.anritsu_server
    spec=cxn.spectrum_analyzer_server
    scope=cxn.sampling_scope
    ds=cxn.data_vault

    yield anr.amplitude(Value(scanparams['anritsu dBm'],'dBm'))
    yield anr.output(True)

    print 'Sideband calibration from %g GHz to %g GHz in steps of %g GHz...' \
       %  (scanparams['carrierMin'],scanparams['carrierMax'],
           scanparams['sidebandCarrierStep'])
    
    sidebandfreqs = (arange(scanparams['sidebandFreqCount']) \
                         - (scanparams['sidebandFreqCount']-1) * 0.5) \
                     * scanparams['sidebandFreqStep']
    dependents = []
    for sidebandfreq in sidebandfreqs:
        dependents += [('relative compensation', 'Q at f_SB = %g MHz' % \
                            (sidebandfreq*1e3),''),
                       ('relative compensation', 'I at f_SB = %g MHz' % \
                            (sidebandfreq*1e3),'')]    
    yield ds.cd(['',SESSIONNAME,boardname],True)
    dataset = yield ds.new(IQNAME,[('Antritsu Frequency','GHz')],dependents)
    yield ds.add_parameter('Anritsu amplitude',
                      Value(scanparams['anritsu dBm'],'dBm'))
    yield ds.add_parameter('Sideband frequency step',
                     Value(scanparams['sidebandFreqStep']*1e3,'MHz'))
    yield ds.add_parameter('Number of sideband frequencies',
                     scanparams['sidebandFreqCount'])
    freq=scanparams['carrierMin']
    while freq<scanparams['carrierMax']:
        print '  carrier frequency: %g GHz' % freq
        datapoint=[freq]
        for sidebandfreq in sidebandfreqs:
            print '    sideband frequency: %g GHz' % sidebandfreq
            comp = yield sideband(anr,spec,fpga,corrector,freq,sidebandfreq)
            datapoint += [real(comp), imag(comp)]
        yield ds.add(datapoint)
        freq+=scanparams['sidebandCarrierStep']
    returnValue(datasetNumber(dataset))