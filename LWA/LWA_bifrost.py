#!/usr/bin/env python

## Core Python Includes
from __future__ import print_function
import signal
import logging
import time
import json
import os
import sys
import threading
import argparse
import numpy
import time
from collections import deque
from scipy.fftpack import fft
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import datetime
import ctypes

## Profiling Includes
import cProfile
import pstats

## Bifrost Includes
from bifrost.address import Address as BF_Address
from bifrost.udp_socket import UDPSocket as BF_UDPSocket
from bifrost.udp_capture import UDPCapture as BF_UDPCapture
from bifrost.ring import Ring
from bifrost.unpack import unpack as Unpack
from bifrost.quantize import quantize as Quantize
from bifrost.reduce import reduce as Reduce
from bifrost.proclog import ProcLog
from bifrost.libbifrost import bf
from bifrost.fft import Fft
from bifrost.fft_shift import fft_shift_2d
from bifrost.romein import romein_float
import bifrost
import bifrost.affinity
from bifrost.libbifrost import bf
from bifrost.ndarray import memset_array, copy_array
from bifrost.device import set_device as BFSetGPU, get_device as BFGetGPU, set_devices_no_spin_cpu as BFNoSpinZone
BFNoSpinZone()

## LWA Software Library Includes
from lsl.common.constants import c as speedOfLight
from lsl.writer import fitsidi
from lsl.reader.ldp import TBNFile
from lsl.common.stations import lwasv, parseSSMIF

######################## Profiling ############################

def enable_thread_profiling():
    '''Monkey-patch Thread.run to enable global profiling.

    Each thread creates a local profiler; statistics are pooled
    to the global stats object on run completion.'''
    threading.Thread.stats = None
    thread_run = threading.Thread.run

    def profile_run(self):
        self._prof = cProfile.Profile()
        self._prof.enable()
        thread_run(self)
        self._prof.disable()

        if threading.Thread.stats is None:
            threading.Thread.stats = pstats.Stats(self._prof)
        else:
            threading.Thread.stats.add(self._prof)

    threading.Thread.run = profile_run


def get_thread_stats():
      stats = getattr(threading.Thread, 'stats', None)
      if stats is None:
          raise ValueError, 'Thread profiling was not enabled,'
      'or no threads finished running.'
      return stats

###############################################################


################ Frequency-Dependent Locations ################

def GenerateLocations(lsl_locs, frequencies, ntime, nchan, npol, grid_size=64, grid_resolution=20/60.):
    
    delta = (2*grid_size*numpy.sin(numpy.pi*grid_resolution/360))**-1
    chan_wavelengths = speedOfLight/frequencies
    sample_grid = chan_wavelengths*delta    
    sll = sample_grid[0] / chan_wavelengths[0]
    lsl_locs = lsl_locs.T
    lsl_locs = lsl_locs.copy()

    lsl_locsf = numpy.zeros(shape=(3,npol,nchan,lsl_locs.shape[1]))
    for l in numpy.arange(3):
        for i in numpy.arange(nchan):
            lsl_locsf[l,:,i,:] = lsl_locs[l,:]/sample_grid[i]

            # I'm sure there's a more numpy way of doing this.
            for p in numpy.arange(npol):
                lsl_locsf[l,p,i,:] -= numpy.min(lsl_locsf[l,p,i,:])


    
    #Calculate grid size needed
    range_u = numpy.max(lsl_locsf[0,...]) - numpy.min(lsl_locsf[0,...])
    range_v = numpy.max(lsl_locsf[0,...]) - numpy.min(lsl_locsf[0,...])
    
    # Centre locations slightly
    for l in numpy.arange(3):
        for i in numpy.arange(nchan):
            for p in numpy.arange(npol):
                lsl_locsf[l,p,i,:] += (grid_size - numpy.max(lsl_locsf[l,p,i,:]))/2

    # Tile them for ntime
    
    locx = numpy.tile(lsl_locsf[0,...],(ntime,1,1,1))
    locy = numpy.tile(lsl_locsf[1,...],(ntime,1,1,1))
    locz = numpy.tile(lsl_locsf[2,...],(ntime,1,1,1))

    return delta, locx, locy, locz, sll
    
    

    
    


###############################################################

######################### EPIC ################################


class OfflineCaptureOp(object):
    def __init__(self, log, oring, filename, chan_bw=25000, profile=False, core=-1):
        self.log = log
        self.oring = oring
        self.filename = filename
        self.core = core
        self.profile = profile
        self.chan_bw = 25000

        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.out_proclog  = ProcLog(type(self).__name__+"/out")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")
        self.out_proclog.update({'nring':1, 'ring0':self.oring.name})

        self.shutdown_event = threading.Event()

    def shutdown(self):
        self.shutdown_event.set()

    def main(self):
        if self.core != -1:
            bifrost.affinity.set_core(self.core)
        self.bind_proclog.update({'ncore': 1,
                                  'core0': bifrost.affinity.get_core(),})

        idf = TBNFile(self.filename)
        cfreq = idf.getInfo('freq1')
        srate = idf.getInfo('sampleRate')
        tInt, tStart, data = idf.read(0.1, timeInSamples=True)

        # Setup the ring metadata and gulp sizes
        ntime = data.shape[1]
        nstand, npol = data.shape[0]/2, 2
        oshape = (ntime,nstand,npol)
        ogulp_size = ntime*nstand*npol*8		# complex64
        self.oring.resize(ogulp_size, buffer_factor=10)

        self.size_proclog.update({'nseq_per_gulp': ntime})

        # Build the initial ring header
        ohdr = {}
        ohdr['time_tag'] = tStart
        ohdr['seq0']     = 0
        ohdr['chan0']    = int((cfreq - srate/2)/self.chan_bw)
        ohdr['nchan']    = 1
        ohdr['cfreq']    = cfreq
        ohdr['bw']       = srate
        ohdr['nstand']   = nstand
        ohdr['npol']     = npol
        ohdr['nbit']     = 8
        ohdr['complex']  = True
        ohdr['axes']     = 'time,stand,pol'
        ohdr_str = json.dumps(ohdr)

        ## Fill the ring using the same data over and over again
        with self.oring.begin_writing() as oring:
            with oring.begin_sequence(time_tag=tStart, header=ohdr_str) as oseq:
                prev_time = time.time()
                if self.profile:
                    spani = 0
                while not self.shutdown_event.is_set():
                    ## Get the current section to use
                    try:
                        _, _, next_data = idf.read(0.1, timeInSamples=True)
                    except Exception as e:
                        print("FillerOp: Error - '%s'" % str(e))
                        idf.close()
                        self.shutdown()
                        break

                    curr_time = time.time()
                    acquire_time = curr_time - prev_time
                    prev_time = curr_time

                    with oseq.reserve(ogulp_size) as ospan:
                        curr_time = time.time()
                        reserve_time = curr_time - prev_time
                        prev_time = curr_time

                        ## Setup and load
                        idata = data

                        odata = ospan.data_view(numpy.complex64).reshape(oshape)

                        ## Transpose and reshape to time by stand by pol
                        idata = idata.transpose((1,0))
                        idata = idata.reshape((ntime,nstand,npol))

                        ## Save
                        odata[...] = idata

                    data = next_data

                    curr_time = time.time()
                    process_time = curr_time - prev_time
                    prev_time = curr_time
                    self.perf_proclog.update({'acquire_time': acquire_time,
                                              'reserve_time': reserve_time,
                                              'process_time': process_time,})
                    if self.profile:
                        spani += 1
                        if spani >= 10:
                            sys.exit()
                            break
        print("FillerOp - Done")


class FDomainOp(object):
    def __init__(self, log, iring, oring, ntime_gulp=2500, nchan_out=1, profile=False, core=-1, gpu=-1):
        self.log = log
        self.iring = iring
        self.oring = oring
        self.ntime_gulp = ntime_gulp
        self.nchan_out = nchan_out
        self.core = core
        self.gpu = gpu
        self.profile = profile

        self.nchan_out = nchan_out

        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.out_proclog  = ProcLog(type(self).__name__+"/out")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")

        self.in_proclog.update( {'nring':1, 'ring0':self.iring.name})
        self.out_proclog.update({'nring':1, 'ring0':self.oring.name})
        self.size_proclog.update({'nseq_per_gulp': self.ntime_gulp})
        self.shutdown_event = threading.Event()

    def shutdown(self):
        self.shutdown_event.set()

    def main(self):
        if self.core != -1:
            bifrost.affinity.set_core(self.core)
        if self.gpu != -1:
            BFSetGPU(self.gpu)
        self.bind_proclog.update({'ncore': 1,
                                  'core0': bifrost.affinity.get_core(),
                                  'ngpu': 1,
                                  'gpu0': BFGetGPU(),})

        with self.oring.begin_writing() as oring:
            for iseq in self.iring.read(guarantee=True):
                ihdr = json.loads(iseq.header.tostring())

                self.sequence_proclog.update(ihdr)
                print('FDomainOp: Config - %s' % ihdr)

                # Setup the ring metadata and gulp sizes
                nchan  = self.nchan_out
                nstand = ihdr['nstand']
                npol   = ihdr['npol']

                igulp_size = self.ntime_gulp*1*nstand*npol * 8		# complex64
                ishape = (self.ntime_gulp/nchan,nchan,nstand,npol)
                ogulp_size = self.ntime_gulp*1*nstand*npol * 1		# ci4
                oshape = (self.ntime_gulp/nchan,nchan,nstand,npol)
                #self.iring.resize(igulp_size)
                self.oring.resize(ogulp_size,buffer_factor=5)

                # Set the output header
                ohdr = ihdr.copy()
                ohdr['nchan'] = nchan
                ohdr['nbit']  = 4
                ohdr['axes']  = 'time,chan,stand,pol'
                ohdr_str = json.dumps(ohdr)

                prev_time = time.time()
                with oring.begin_sequence(time_tag=iseq.time_tag, header=ohdr_str) as oseq:
                    print("FDomain Out Sequence!")
                    iseq_spans = iseq.read(igulp_size)
                    while not self.iring.writing_ended():
                        for ispan in iseq_spans:
                            if ispan.size < igulp_size:
                                continue # Ignore final gulp
                            curr_time = time.time()
                            acquire_time = curr_time - prev_time
                            prev_time = curr_time

                            if self.profile:
                                spani = 0

                            with oseq.reserve(ogulp_size) as ospan:
                                curr_time = time.time()
                                reserve_time = curr_time - prev_time
                                prev_time = curr_time

                                ## Setup and load
                                idata = ispan.data_view(numpy.complex64).reshape(ishape)

                                odata = ospan.data_view(numpy.int8).reshape(oshape)

                                ## FFT, shift, and phase
                                fdata = fft(idata, axis=1)
                                fdata = numpy.fft.fftshift(fdata, axes=1)
                                fdata = bifrost.ndarray(fdata, space='system')

                                ## Quantization
                                try:
                                    Quantize(fdata, qdata, scale=1./numpy.sqrt(nchan))
                                except NameError:
                                    qdata = bifrost.ndarray(shape=fdata.shape, native=False, dtype='ci4')
                                    Quantize(fdata, qdata, scale=1./numpy.sqrt(nchan))

                                ## Save
                                odata[...] = qdata.copy(space='cuda_host').view(numpy.int8).reshape(oshape)

                                if self.profile:
                                    spani += 1
                                    if spani >= 10:
                                        sys.exit()
                                        break

                            curr_time = time.time()
                            process_time = curr_time - prev_time
                            prev_time = curr_time
                            self.perf_proclog.update({'acquire_time': acquire_time,
                                                      'reserve_time': reserve_time,
                                                      'process_time': process_time,})
                    # Only do one pass through the loop
        print("FDomainOp - Done")



## For when we don't need to care about doing the F-Engine ourself.
## TODO: Implement this come implementation time...
FS = 196.0e6
CHAN_BW = 25.0e3
ADP_EPOCH = datetime.datetime(1970, 1, 1)

class FEngineCaptureOp(object):
    '''
    Receives Fourier Spectra from LWA FPGA
    '''
    def __init__(self, log, *args, **kwargs):
        self.log = log
        self.args = args
        self.kwargs = kwargs
        self.utc_start = self.kwargs['utc_start']
        del self.kwargs['utc_start']
        self.shutdown_event = threading.Event()

    def shutdown(self):
        self.shutdown_event.set()

    def seq_callback(self, seq0, chan0, nchan, nsrc,
                    time_tag_ptr, hdr_ptr, hdr_size_ptr):
        timestamp0 = int((self.utc_start - ADP_EPOCH).total_seconds())
        time_tag0  = timestamp0 * int(FS)
        time_tag   = time_tag0 + seq0*(int(FS)//int(CHAN_BW))
        print("++++++++++++++++ seq0     =", seq0)
        print("                 time_tag =", time_tag)
        time_tag_ptr[0] = time_tag
        hdr = {
            'time_tag': time_tag,
            'seq0':     seq0,
            'chan0':    chan0,
            'nchan':    nchan,
            'cfreq':    (chan0 + 0.5*(nchan-1))*CHAN_BW,
            'bw':       nchan*CHAN_BW,
            'nstand':   nsrc*16,
            #'stand0':   src0*16, # TODO: Pass src0 to the callback too(?)
            'npol':     2,
            'complex':  True,
            'nbit':     4,
            'axes':     'time,chan,stand,pol'
        }
        print("******** CFREQ:", hdr['cfreq'])
        hdr_str = json.dumps(hdr)
        # TODO: Can't pad with NULL because returned as C-string
        #hdr_str = json.dumps(hdr).ljust(4096, '\0')
        #hdr_str = json.dumps(hdr).ljust(4096, ' ')
        self.header_buf = ctypes.create_string_buffer(hdr_str)
        hdr_ptr[0]      = ctypes.cast(self.header_buf, ctypes.c_void_p)
        hdr_size_ptr[0] = len(hdr_str)
        return 0

    def main(self):
        seq_callback = bf.BFudpcapture_sequence_callback(self.seq_callback)
        with BF_UDPCapture(*self.args,
                        sequence_callback=seq_callback,
                        **self.kwargs) as capture:
            while not self.shutdown_event.is_set():
                status = capture.recv()
        del capture

class DecimationOp(object):
    def __init__(self, log, iring, oring, ntime_gulp=2500, nchan_out=1, npol_out=2, guarantee=True, core=-1):
        self.log = log
        self.iring = iring
        self.oring = oring
        self.ntime_gulp = ntime_gulp
        self.nchan_out = nchan_out
        self.npol_out = npol_out
        self.guarantee = guarantee
        self.core = core

        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.out_proclog  = ProcLog(type(self).__name__+"/out")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")

        self.in_proclog.update(  {'nring':1, 'ring0':self.iring.name})
        self.out_proclog.update( {'nring':1, 'ring0':self.oring.name})
        self.size_proclog.update({'nseq_per_gulp': self.ntime_gulp})

    def main(self):
        if self.core != -1:
            bifrost.affinity.set_core(self.core)
        self.bind_proclog.update({'ncore': 1,
                                  'core0': bifrost.affinity.get_core(),})

        with self.oring.begin_writing() as oring:
            for iseq in self.iring.read(guarantee=self.guarantee):
                ihdr = json.loads(iseq.header.tostring())

                self.sequence_proclog.update(ihdr)

                self.log.info("Decimation: Start of new sequence: %s", str(ihdr))

                nchan  = ihdr['nchan']
                nstand = ihdr['nstand']
                npol   = ihdr['npol']
                chan0  = ihdr['chan0']

                igulp_size = self.ntime_gulp*nchan*nstand*npol*1                     # ci4
                ishape = (self.ntime_gulp,nchan,nstand,npol)
                ogulp_size = self.ntime_gulp*self.nchan_out*nstand*self.npol_out*1   # ci4
                oshape = (self.ntime_gulp,self.nchan_out,nstand,self.npol_out)
                self.iring.resize(igulp_size)
                self.oring.resize(ogulp_size)#, obuf_size)

                ohdr = ihdr.copy()
                ohdr['nchan'] = self.nchan_out
                ohdr['npol']  = self.npol_out
                ohdr['cfreq'] = (chan0 + 0.5*(self.nchan_out-1))*CHAN_BW
                ohdr['bw']    = self.nchan_out*CHAN_BW
                ohdr_str = json.dumps(ohdr)

                prev_time = time.time()
                with oring.begin_sequence(time_tag=iseq.time_tag, header=ohdr_str) as oseq:
                    for ispan in iseq.read(igulp_size):
                        if ispan.size < igulp_size:
                            continue # Ignore final gulp
                        curr_time = time.time()
                        acquire_time = curr_time - prev_time
                        prev_time = curr_time

                        with oseq.reserve(ogulp_size) as ospan:
                            curr_time = time.time()
                            reserve_time = curr_time - prev_time
                            prev_time = curr_time

                            idata = ispan.data_view(numpy.uint8).reshape(ishape)
                            odata = ospan.data_view(numpy.uint8).reshape(oshape)

                            sdata = idata[:,:self.nchan_out,:,:]
                            if self.npol_out != npol:
                                sdata = sdata[:,:,:,:self.npol_out]
                            odata[...] = sdata

                            curr_time = time.time()
                            process_time = curr_time - prev_time
                            prev_time = curr_time
                            self.perf_proclog.update({'acquire_time': acquire_time,
                                                      'reserve_time': reserve_time,
                                                      'process_time': process_time,})

class TransposeOp(object):
    def __init__(self, log, iring, oring, ntime_gulp=2500, guarantee=True, core=-1):
        self.log = log
        self.iring = iring
        self.oring = oring
        self.ntime_gulp = ntime_gulp
        self.guarantee = guarantee
        self.core = core

        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.out_proclog  = ProcLog(type(self).__name__+"/out")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")

        self.in_proclog.update(  {'nring':1, 'ring0':self.iring.name})
        self.out_proclog.update( {'nring':1, 'ring0':self.oring.name})
        self.size_proclog.update({'nseq_per_gulp': self.ntime_gulp})

    def main(self):
        if self.core != -1:
            bifrost.affinity.set_core(self.core)
        self.bind_proclog.update({'ncore': 1,
                                  'core0': bifrost.affinity.get_core(),})

        with self.oring.begin_writing() as oring:
            for iseq in self.iring.read(guarantee=self.guarantee):
                ihdr = json.loads(iseq.header.tostring())

                self.sequence_proclog.update(ihdr)

                self.log.info("Transpose: Start of new sequence: %s", str(ihdr))

                nchan  = ihdr['nchan']
                nstand = ihdr['nstand']
                npol   = ihdr['npol']
                chan0  = ihdr['chan0']

                igulp_size = self.ntime_gulp*nchan*nstand*npol*1        # ci4
                ishape = (self.ntime_gulp,nchan,nstand,npol)
                ogulp_size = self.ntime_gulp*nchan*npol*nstand*1        # ci4
                oshape = (self.ntime_gulp,nchan,npol,nstand)
                self.iring.resize(igulp_size)
                self.oring.resize(ogulp_size)#, obuf_size)

                ohdr = ihdr.copy()
                ohdr['axes'] = 'time,chan,pol,stand'
                ohdr_str = json.dumps(ohdr)

                prev_time = time.time()
                with oring.begin_sequence(time_tag=iseq.time_tag, header=ohdr_str) as oseq:
                    for ispan in iseq.read(igulp_size):
                        if ispan.size < igulp_size:
                            continue # Ignore final gulp
                        curr_time = time.time()
                        acquire_time = curr_time - prev_time
                        prev_time = curr_time

                        with oseq.reserve(ogulp_size) as ospan:
                            curr_time = time.time()
                            reserve_time = curr_time - prev_time
                            prev_time = curr_time

                            idata = ispan.data_view(numpy.uint8).reshape(ishape)
                            odata = ospan.data_view(numpy.uint8).reshape(oshape)

                            idata = idata.transpose(0,1,3,2)
                            odata[...] = idata.copy()

                            curr_time = time.time()
                            process_time = curr_time - prev_time
                            prev_time = curr_time
                            self.perf_proclog.update({'acquire_time': acquire_time,
                                                      'reserve_time': reserve_time,
                                                      'process_time': process_time,})

class CalibrationOp(object):
    def __init__(self, log, iring, oring, *args, **kwargs):
        pass

class MOFFCorrelatorOp(object):
    def __init__(self, log, iring, oring, antennas, grid_size, grid_resolution, 
                 ntime_gulp=2500, accumulation_time=10000, core=-1, gpu=-1, 
                 remove_autocorrs = False, benchmark=False, profile=False, 
                 *args, **kwargs):
        self.log = log
        self.iring = iring
        self.oring = oring
        self.ntime_gulp = ntime_gulp
        self.accumulation_time=accumulation_time

        self.antennas = antennas
        locations = numpy.empty(shape=(0,3))
        for ant in self.antennas:
            locations = numpy.vstack((locations,[ant.stand[0],ant.stand[1],ant.stand[2]]))
        locations = numpy.delete(locations, list(range(0,locations.shape[0],2)),axis=0)
        locations[255,:] = 0.0
        self.locations = locations
        
        self.grid_size = grid_size
        self.grid_resolution  = grid_resolution
        
        self.core = core
        self.gpu = gpu
        self.remove_autocorrs = remove_autocorrs
        self.benchmark = benchmark
        self.newflag = True
        self.profile = profile
        
        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.out_proclog  = ProcLog(type(self).__name__+"/out")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")

        self.in_proclog.update( {'nring':1, 'ring0':self.iring.name})
        self.out_proclog.update({'nring':1, 'ring0':self.oring.name})
        self.size_proclog.update({'nseq_per_gulp': self.ntime_gulp})

        if self.gpu != -1:
            BFSetGPU(self.gpu)
            
        self.ant_extent = 1
        self.antgridmap = bifrost.ndarray(numpy.ones(shape=(self.ant_extent,self.ant_extent),dtype=numpy.complex64),space='cuda')
        self.antgridmap = self.antgridmap.copy(space='cuda',order='C')

        self.shutdown_event = threading.Event()

    def shutdown(self):
        self.shutdown_event.set()


    def main(self):
        if self.core != -1:
            bifrost.affinity.set_core(self.core)
        if self.gpu != -1:
            BFSetGPU(self.gpu)
        self.bind_proclog.update({'ncore': 1,
                                  'core0': bifrost.affinity.get_core(),
                                  'ngpu': 1,
                                  'gpu0': BFGetGPU(),})

        runtime_history = deque([], 50)
        accum = 0
        with self.oring.begin_writing() as oring:
            for iseq in self.iring.read(guarantee=True):
                ihdr = json.loads(iseq.header.tostring())
                self.sequence_proclog.update(ihdr)
                self.log.info('MOFFCorrelatorOp: Config - %s' % ihdr)
                chan0 = ihdr['chan0']
                nchan = ihdr['nchan']
                nstand = ihdr['nstand']
                npol = ihdr['npol']
                self.newflag = True
                accum = 0
                
                igulp_size = self.ntime_gulp * nchan * nstand * npol * 1 # ci4
                itshape = (self.ntime_gulp,nchan,npol,nstand)
                
                
                freq = (chan0 + numpy.arange(nchan))*CHAN_BW
                sampling_length, lx, ly, lz, sll = GenerateLocations(self.locations, freq, 
                                                                     self.ntime_gulp, nchan, npol, 
                                                                     grid_size=self.grid_size,
                                                                     grid_resolution=self.grid_resolution)
                try:
                    copy_array(self.lx, bifrost.ndarray(lx.astype(numpy.int32)))
                    copy_array(self.ly, bifrost.ndarray(ly.astype(numpy.int32)))
                    copy_array(self.lz, bifrost.ndarray(lz.astype(numpy.int32)))
                except AttributeError:
                    self.lx = bifrost.ndarray(lx.astype(numpy.int32), space='cuda')
                    self.ly = bifrost.ndarray(ly.astype(numpy.int32), space='cuda')
                    self.lz = bifrost.ndarray(lz.astype(numpy.int32), space='cuda')
    

                ohdr = ihdr.copy()
                ohdr['nbit'] = 64


                ohdr['npol'] = npol**2 # Because of cross multiplying shenanigans
                ohdr['grid_size_x'] = self.grid_size
                ohdr['grid_size_y'] = self.grid_size
                ohdr['axes'] = 'time,chan,pol,gridy,gridx'
                ohdr['sampling_length_x'] = sampling_length
                ohdr['sampling_length_y'] = sampling_length
                ohdr['accumulation_time'] = self.accumulation_time
                ohdr['FS'] = FS
                ohdr['latitude'] = lwasv.lat * 180. / numpy.pi
                ohdr['longitude'] = lwasv.lon * 180. / numpy.pi
                ohdr['telescope'] = 'LWA-SV'
                ohdr['data_units'] = 'UNCALIB'
                if ohdr['npol'] == 1:
                    ohdr['pols'] = ['xx']
                elif ohdr['npol'] == 2:
                    ohdr['pols'] = ['xx', 'yy']
                elif ohdr['npol'] == 4:
                    ohdr['pols'] = ['xx', 'xy', 'yx', 'yy']
                else:
                    raise ValueError('Cannot write fits file without knowing polarization list')
                ohdr_str = json.dumps(ohdr)

                # Setup the phasing terms for zenith
                # Phases are Nchan x Npol x Nstand
                phases = numpy.zeros((itshape[1], itshape[2], itshape[3]), dtype=numpy.complex64)
                for i in xrange(nstand):
                    ## X
                    a = self.antennas[2*i + 0]
                    delay = a.cable.delay(freq) - a.stand.z / speedOfLight
                    phases[:,0,i] = numpy.exp(2j*numpy.pi*freq*delay)
                    phases[:,0,i] /= numpy.sqrt(a.cable.gain(freq))
                    if npol == 2:
                        ## Y
                        a = self.antennas[2*i + 1]
                        delay = a.cable.delay(freq) - a.stand.z / speedOfLight
                        phases[:,1,i] = numpy.exp(2j*numpy.pi*freq*delay)
                        phases[:,1,i] /= numpy.sqrt(a.cable.gain(freq))
                    ## Explicit outrigger masking - we probably want to do
                    ## away with this at some point
                    if a.stand.id == 256:
                        phases[:,:,i] = 0.0
                phases = bifrost.ndarray(phases)
                try:
                    copy_array(gphases, phases)
                except NameError:
                    gphases = phases.copy(space='cuda')
                    
                oshape = (1,nchan,npol**2,self.grid_size,self.grid_size)
                ogulp_size = nchan * npol**2 * self.grid_size * self.grid_size * 8
                self.iring.resize(igulp_size)
                self.oring.resize(ogulp_size,buffer_factor=5)
                prev_time = time.time()
                with oring.begin_sequence(time_tag=iseq.time_tag,header=ohdr_str) as oseq:
                    iseq_spans = iseq.read(igulp_size)
                    while not self.iring.writing_ended():
                        reset_sequence = False

                        if self.profile:
                            spani = 0

                        for ispan in iseq_spans:
                            if ispan.size < igulp_size:
                                continue # Ignore final gulp
                            curr_time = time.time()
                            acquire_time = curr_time - prev_time
                            prev_time = curr_time


                            if self.benchmark == True:
                                print(" ------------------ ")

                            ###### Correlator #######
                            ## Setup and load
                            idata = ispan.data_view(numpy.uint8).reshape(itshape)
                            ## Fix the type
                            tdata = bifrost.ndarray(shape=itshape, dtype='ci4', native=False, buffer=idata.ctypes.data)

                            if self.benchmark == True:
                                time1=time.time()
                            #tdata = tdata.transpose((0,1,3,2))

                            tdata = tdata.copy(space='cuda')
                            if self.benchmark == True:
                                time1a = time.time()
                                print("  Input copy time: %f" % (time1a-time1))

                            ## Unpack
                            try:
                                udata = udata.reshape(*tdata.shape)
                                Unpack(tdata, udata)
                            except NameError:
                                udata = bifrost.ndarray(shape=tdata.shape, dtype=numpy.complex64, space='cuda')
                                Unpack(tdata, udata)
                            if self.benchmark == True:
                                time1b = time.time()
                            ## Phase
                            bifrost.map('a(i,j,k,l) *= b(j,k,l)',
                                        {'a':udata, 'b':gphases}, axis_names=('i','j','k','l'), shape=udata.shape)
                            if self.benchmark == True:
                                time1c = time.time()
                                print("  Unpack and phase-up time: %f" % (time1c-time1a))

                            ## Make sure we have a place to put the gridded data
                            # Gridded Antennas
                            try:
                                gdata = gdata.reshape(self.ntime_gulp*nchan*npol,self.grid_size,self.grid_size)
                                memset_array(gdata,0)
                            except NameError:
                                gdata = bifrost.zeros(shape=(self.ntime_gulp*nchan*npol,self.grid_size,self.grid_size),dtype=numpy.complex64, space='cuda')

                            ## Grid the Antennas
                            if self.benchmark == True:
                                timeg1 = time.time()


                            gdata = romein_float(udata,gdata,self.antgridmap,self.lx,self.ly,self.lz,self.ant_extent,self.grid_size,nstand,self.ntime_gulp*npol*nchan)
                            #gdata = self.LinAlgObj.matmul(1.0, udata, bfantgridmap, 0.0, gdata)
                            if self.benchmark == True:
                                timeg2 = time.time()
                                print("  Romein time: %f"%(timeg2 - timeg1))

                            #Inverse transform

                            if self.benchmark == True:
                                timefft1 = time.time()
                            try:

                                bf_fft.execute(gdata,gdata,inverse=True)
                            except NameError:

                                bf_fft = Fft()
                                bf_fft.init(gdata,gdata,axes=(1,2))
                                bf_fft.execute(gdata,gdata,inverse=True)
                            gdata = gdata.reshape(1,self.ntime_gulp,nchan,npol,self.grid_size,self.grid_size)
                            if self.benchmark == True:
                                timefft2 = time.time()
                                print("  FFT time: %f"%(timefft2 - timefft1))

                            #print ("Accum: %d"%accum,end='\n')
                            if self.newflag is True:
                                try:
                                    crosspol = crosspol.reshape(self.ntime_gulp,nchan,npol**2,self.grid_size,self.grid_size)
                                    accumulated_image = accumulated_image.reshape(1,nchan,npol**2,self.grid_size,self.grid_size)
                                    memset_array(crosspol, 0)
                                    memset_array(accumulated_image, 0)

                                except NameError:
                                    crosspol = bifrost.zeros(shape=(self.ntime_gulp,nchan,npol**2,self.grid_size,self.grid_size),
                                                             dtype=numpy.complex64, space='cuda')
                                    accumulated_image = bifrost.zeros(shape=(1,nchan,npol**2,self.grid_size,self.grid_size),
                                                                      dtype=numpy.complex64, space='cuda')
                                self.newflag=False



                            if self.remove_autocorrs == True:

                                ##Setup everything for the autocorrelation calculation.
                                try:
                                    # If one isn't allocated, then none of them are.
                                    autocorrs = autocorrs.reshape(self.ntime_gulp,nchan,npol**2,nstand)
                                    autocorr_g = autocorr_g.reshape(nchan*npol**2,self.grid_size,self.grid_size)
                                except NameError:
                                    autocorrs = bifrost.ndarray(shape=(self.ntime_gulp,nchan,npol**2,nstand),dtype=numpy.complex64, space='cuda')
                                    autocorrs_av = bifrost.zeros(shape=(1,nchan,npol**2,nstand), dtype=numpy.complex64, space='cuda')
                                    autocorr_g = bifrost.zeros(shape=(nchan*npol**2,self.grid_size,self.grid_size), dtype=numpy.complex64, space='cuda')
                                    autocorr_lx = bifrost.ndarray(numpy.ones(shape=(nchan*npol**2*nstand),dtype=numpy.int32)*self.grid_size/2,space='cuda')
                                    autocorr_ly = bifrost.ndarray(numpy.ones(shape=(nchan*npol**2*nstand),dtype=numpy.int32)*self.grid_size/2,space='cuda')
                                    autocorr_lz = bifrost.zeros(shape=(nchan*npol**2*nstand),dtype=numpy.int32,space='cuda')
                                    autocorr_il = bifrost.ndarray(numpy.ones(shape=(self.ant_extent,self.ant_extent),dtype=numpy.complex64),space='cuda')


                                # Cross multiply to calculate autocorrs
                                bifrost.map('a(i,j,k,l) += (b(i,j,k/2,l) * b(i,j,k%2,l).conj())',
                                            {'a':autocorrs, 'b':udata,'t':self.ntime_gulp},
                                            axis_names=('i','j','k','l'),
                                            shape=(self.ntime_gulp,nchan,npol**2,nstand))

                            bifrost.map('a(i,j,p,k,l) += b(0,i,j,p/2,k,l)*b(0,i,j,p%2,k,l).conj()',
                                        {'a':crosspol, 'b':gdata},
                                        axis_names=('i','j', 'p', 'k', 'l'),
                                        shape=(self.ntime_gulp, nchan, npol**2, self.grid_size, self.grid_size))


                            # Increment
                            accum += 1e3 * self.ntime_gulp / CHAN_BW
                            
                            if accum >= self.accumulation_time:

                                bifrost.reduce(crosspol, accumulated_image, op='sum')
                                if self.remove_autocorrs == True:
                                    # Reduce along time axis.
                                    bifrost.reduce(autocorrs, autocorrs_av, op='sum')
                                    # Grid the autocorrelations.
                                    autocorr_g = romein_float(autocorrs_av,autocorr_g,autocorr_il,autocorr_lx,autocorr_ly,autocorr_lz,self.ant_extent,self.grid_size,nstand,nchan*npol**2)

                                    #Inverse FFT
                                    try:
                                        autocorr_g = fft_shift_2d(autocorr_g, self.grid_size, nchan*npol**2)
                                        ac_fft.execute(autocorr_g,autocorr_g,inverse=True)
                                    except NameError:
                                         ac_fft = Fft()
                                         ac_fft.init(autocorr_g,autocorr_g,axes=(1,2))
                                         autocorr_g = fft_shift_2d(autocorr_g, self.grid_size, nchan*npol**2)
                                         ac_fft.execute(autocorr_g,autocorr_g,inverse=True)

                                    accumulated_image = accumulated_image.reshape(nchan,npol**2,self.grid_size, self.grid_size)
                                    autocorr_g = autocorr_g.reshape(nchan,npol**2,self.grid_size, self.grid_size)
                                    bifrost.map('a(i,j,k,l) -= b(i,j,k,l)',
                                                {'a':accumulated_image, 'b':autocorr_g},
                                                axis_names=('i','j','k','l'),
                                                shape=(nchan,npol**2,self.grid_size, self.grid_size))





                                curr_time = time.time()
                                process_time = curr_time - prev_time
                                prev_time = curr_time
                                
                                with oseq.reserve(ogulp_size) as ospan:
                                    odata = ospan.data_view(numpy.complex64).reshape(oshape)
                                    accumulated_image = accumulated_image.reshape(oshape)
                                    odata[...] = accumulated_image
                                    
                                curr_time = time.time()
                                reserve_time = curr_time - prev_time
                                prev_time = curr_time
                                
                                self.newflag = True
                                accum = 0

                                if self.remove_autocorrs == True:
                                    autocorr_g = autocorr_g.reshape(oshape)
                                    memset_array(autocorr_g,0)
                                    memset_array(autocorrs,0)
                                    memset_array(autocorrs_av,0)
                                    
                            else:
                                process_time = 0.0
                                reserve_time = 0.0

                            curr_time = time.time()
                            process_time += curr_time - prev_time
                            prev_time = curr_time
                            
                            #TODO: Autocorrs using Romein??
                            ## Output for gridded electric fields.
                            if self.benchmark == True:
                                time1h = time.time()
                            #gdata = gdata.reshape(self.ntime_gulp,nchan,2,self.grid_size,self.grid_size)
                            # image/autos, time, chan, pol, gridx, grid.
                            #accumulated_image = accumulated_image.reshape(oshape)

                            if self.benchmark == True:
                                time2=time.time()
                                print("-> GPU Time Taken: %f"%(time2-time1))

                                runtime_history.append(time2-time1)
                                print("-> Average GPU Time Taken: %f (%i samples)" % (1.0*sum(runtime_history)/len(runtime_history), len(runtime_history)))
                            if self.profile:
                                spani += 1
                                if spani >= 10:
                                    sys.exit()
                                    break

                            self.perf_proclog.update({'acquire_time': acquire_time,
                                                      'reserve_time': reserve_time,
                                                      'process_time': process_time,})

                        # Reset to move on to the next input sequence?
                        if not reset_sequence:
                            break


class SaveOp(object):
    def __init__(self, log, iring, filename, grid_size, core=-1, gpu=-1, cpu=False,
                 profile=False, ints_per_file=1, out_dir='', *args, **kwargs):
        self.log = log
        self.iring = iring
        self.filename = filename
        self.grid_size = grid_size
        self.ints_per_file = ints_per_file
        self.out_dir = out_dir

        # TODO: Validate ntime_gulp vs accumulation_time
        self.core = core
        self.gpu = gpu
        self.cpu = cpu
        self.profile = profile

        self.bind_proclog = ProcLog(type(self).__name__+"/bind")
        self.in_proclog   = ProcLog(type(self).__name__+"/in")
        self.size_proclog = ProcLog(type(self).__name__+"/size")
        self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
        self.perf_proclog = ProcLog(type(self).__name__+"/perf")

        self.in_proclog.update( {'nring':1, 'ring0':self.iring.name})
        self.size_proclog.update({'nseq_per_gulp': 1})

        self.shutdown_event = threading.Event()

    def shutdown(self):
        self.shutdown_event.set()


    def main(self):
        if self.core != -1:
            bifrost.affinity.set_core(self.core)
        if self.gpu != -1:
            BFSetGPU(self.gpu)
        self.bind_proclog.update({'ncore': 1,
                                  'core0': bifrost.affinity.get_core(),
                                  'ngpu': 1,
                                  'gpu0': BFGetGPU(),})


        fileid = 0
        for iseq in self.iring.read(guarantee=True):
            ihdr = json.loads(iseq.header.tostring())

            self.sequence_proclog.update(ihdr)
            self.log.info('SaveOp: Config - %s' % ihdr)

            cfreq = ihdr['cfreq']
            nchan = ihdr['nchan']
            npol = ihdr['npol']
            print("Channel no: %d, Polarisation no: %d"%(nchan,npol))

            igulp_size = nchan * npol * self.grid_size * self.grid_size * 8
            ishape = (nchan,npol,self.grid_size,self.grid_size)
            image = []

            prev_time = time.time()
            iseq_spans = iseq.read(igulp_size)
            nints = 0

            if self.profile:
                spani = 0

            for ispan in iseq_spans:
                if ispan.size < igulp_size:
                    continue # Ignore final gulp
                curr_time = time.time()
                acquire_time = curr_time - prev_time
                prev_time = curr_time

                idata = ispan.data_view(numpy.complex64).reshape(ishape)
                itemp = idata.copy(space='cuda_host')
                image.append(itemp)
                nints += 1
                if nints >= self.ints_per_file:
                    image = numpy.fft.fftshift(image, axes=(3, 4))
                    image = image[:, :, :, ::-1, :]
                    unix_time = (ihdr['time_tag'] / FS + ihdr['accumulation_time']
                                 * 1e-3 * fileid * self.ints_per_file)
                    image_nums = numpy.arange(fileid * self.ints_per_file, (fileid + 1) * self.ints_per_file)
                    filename = os.path.join(self.out_dir, 'EPIC_{0:3f}_{1:0.3f}MHz.npz'.format(unix_time, cfreq/1e6))
                    numpy.savez(filename, image=image, hdr=ihdr, image_nums=image_nums)
                    image = []
                    nints = 0
                    fileid += 1
                    print("SaveOp - Image Saved")

                curr_time = time.time()
                process_time = curr_time - prev_time
                prev_time = curr_time
                self.perf_proclog.update({'acquire_time': acquire_time,
                                          'reserve_time': -1,
                                          'process_time': process_time,})
                if self.profile:
                    spani += 1
                    if spani >= 10:
                        sys.exit()
                        break

class SaveFFTOp(object):
    def __init__(self, log, iring, filename, ntime_gulp=2500, core=-1,*args, **kwargs):
        self.log = log
        self.iring = iring
        self.filename = filename
        self.core = core
        self.ntime_gulp = ntime_gulp

    def main(self):
        #bifrost.affinity.set_core(self.core)

        for iseq in self.iring.read(guarantee=True):

            ihdr = json.loads(iseq.header.tostring())
            nchan = ihdr['nchan']
            nstand = ihdr['nstand']
            npol = ihdr['npol']


            igulp_size = self.ntime_gulp*1*nstand*npol * 2         # ci8
            ishape = (self.ntime_gulp/nchan,nchan,nstand,npol,2)

            iseq_spans = iseq.read(igulp_size)

            while not self.iring.writing_ended():

                for ispan in iseq_spans:
                    if ispan.size < igulp_size:
                        continue

                    idata = ispan.data_view(numpy.int8)

                    idata = idata.reshape(ishape)
                    idata = bifrost.ndarray(shape=ishape, dtype='ci4', native=False, buffer=idata.ctypes.data)
                    print(numpy.shape(idata))
                    numpy.savez(self.filename + "asdasd.npy",data=idata)
                    print("Wrote to disk")
            break
        print("Save F-Engine Spectra.. done")


def main():

    # Main Input: UDP Broadcast RX from F-Engine?

    parser = argparse.ArgumentParser(description='EPIC Correlator')
    parser.add_argument('--addr', type=str, default = "p5p1", help= 'F-Engine UDP Stream Address')
    parser.add_argument('--port', type=int, default = 4015, help= 'F-Engine UDP Stream Port')
    parser.add_argument('--utcstart', type=str, default = '1970_1_1T0_0_0', help= 'F-Engine UDP Stream Start Time')
    parser.add_argument('--imagesize', type=int, default = 64, help = '1-D Image Size')
    parser.add_argument('--imageres', type=float, default = 1.79057, help = 'Image pixel size in degrees')
    parser.add_argument('--offline', action='store_true', help = 'Load TBN data from Disk')
    parser.add_argument('--tbnfile', type=str, help = 'TBN Data Path')
    parser.add_argument('--nts',type=int, default = 1000, help= 'Number of timestamps per span')
    parser.add_argument('--accumulate',type=int, default = 1000, help='How many milliseconds to accumulate an image over')
    parser.add_argument('--channels',type=int, default=1, help='How many channels to produce')
    parser.add_argument('--singlepol', action='store_true', help = 'Process only X pol. in online mode')
    parser.add_argument('--removeautocorrs', action='store_true', help = 'Removes Autocorrelations')
    parser.add_argument('--benchmark', action='store_true',help = 'benchmark gridder')
    parser.add_argument('--profile', action='store_true', help = 'Run cProfile on ALL threads. Produces trace for each individual thread')
    parser.add_argument('--ints_per_file', type=int, default=1, help='Number of integrations per output FITS file. Default is 1.')
    parser.add_argument('--out_dir', type=str, default='', help='Directory for output files. Default is current directory.')

    args = parser.parse_args()
    # Logging Setup
    # TODO: Set this up properly
    if args.profile:
        enable_thread_profiling()

    if not os.path.isdir(args.out_dir):
        print('Output directory does not exist. Defaulting to current directory.')
        args.out_dir = ''


    log = logging.getLogger(__name__)
    logFormat = logging.Formatter('%(asctime)s [%(levelname)-8s] %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    logFormat.converter = time.gmtime
    logHandler = logging.StreamHandler(sys.stdout)
    logHandler.setFormatter(logFormat)
    log.addHandler(logHandler)
    log.setLevel(logging.DEBUG)

    # Setup the cores and GPUs to use
    cores = [0, 2, 3, 4, 5, 6, 7]
    gpus  = [0, 0, 0, 0, 0, 0, 0]

    # Setup the signal handling
    ops = []
    shutdown_event = threading.Event()
    def handle_signal_terminate(signum, frame):
        SIGNAL_NAMES = dict((k, v) for v, k in \
                            reversed(sorted(signal.__dict__.items()))
                            if v.startswith('SIG') and \
                            not v.startswith('SIG_'))
        log.warning("Received signal %i %s", signum, SIGNAL_NAMES[signum])
        try:
            ops[0].shutdown()
        except IndexError:
            pass
        shutdown_event.set()
    for sig in [signal.SIGHUP,
            signal.SIGINT,
            signal.SIGQUIT,
            signal.SIGTERM,
            signal.SIGTSTP]:
        signal.signal(sig, handle_signal_terminate)


    # Setup Rings

    fcapture_ring = Ring(name="capture",space="cuda_host")
    fdomain_ring = Ring(name="fengine", space="cuda_host")
    transpose_ring = Ring(name="transpose", space="cuda_host")
    gridandfft_ring = Ring(name="gridandfft", space="cuda")
    image_ring = Ring(name="image", space="system")


    # Setup Antennas
    ## TODO: Some sort of switch for other stations?

    lwasv_antennas = lwasv.getAntennas()
    lwasv_stands = lwasv.getStands()
    
    # Setup threads

    if args.offline:
        ops.append(OfflineCaptureOp(log, fcapture_ring, args.tbnfile,
                                    core=cores.pop(0), profile=args.profile))
        ops.append(FDomainOp(log, fcapture_ring, fdomain_ring, ntime_gulp=args.nts,
                             nchan_out=args.channels, core=cores.pop(0), gpu=gpus.pop(0),
                             profile=args.profile))
    else:
        # It would be great is we could pull this from ADP MCS...
        utc_start_dt = datetime.datetime.strptime(args.utcstart, "%Y_%m_%dT%H_%M_%S")

        # Note: Capture uses Bifrost address+socket objects, while output uses
        #         plain Python address+socket objects.
        iaddr = BF_Address(args.addr, args.port)
        isock = BF_UDPSocket()
        isock.bind(iaddr)
        isock.timeout = 0.5

        ops.append(FEngineCaptureOp(log, fmt="chips", sock=isock, ring=fcapture_ring,
                                    nsrc=16, src0=0, max_payload_size=9000,
                                    buffer_ntime=args.nts, slot_ntime=25000, core=cores.pop(0),
                                    utc_start=utc_start_dt))
        ops.append(DecimationOp(log, fcapture_ring, fdomain_ring, ntime_gulp=args.nts,
                                nchan_out=args.channels, npol_out=1 if args.singlepol else 2,
                                core=cores.pop(0)))

    ops.append(TransposeOp(log, fdomain_ring, transpose_ring, ntime_gulp=args.nts,
                                core=cores.pop(0)))
    ops.append(MOFFCorrelatorOp(log, transpose_ring, gridandfft_ring, lwasv_antennas,
                                args.imagesize, args.imageres, ntime_gulp=args.nts, 
                                accumulation_time=args.accumulate, 
                                remove_autocorrs=args.removeautocorrs,
                                core=cores.pop(0), gpu=gpus.pop(0),benchmark=args.benchmark,
                                profile=args.profile))
    ops.append(SaveOp(log, gridandfft_ring, "EPIC_", args.imagesize, out_dir=args.out_dir,
                         core=cores.pop(0), gpu=gpus.pop(0), cpu=False,
                         ints_per_file=args.ints_per_file, profile=args.profile))

    threads= [threading.Thread(target=op.main) for op in ops]

    # Go!

    for thread in threads:
        thread.daemon = False
        thread.start()

    while not shutdown_event.is_set():
        # Keep threads alive -- if reader is still alive, prevent timeout signal from executing
        if threads[0].is_alive():
            signal.pause()
        else:
            break

    # Wait for threads to finish

    for thread in threads:
        thread.join()

    if args.profile:
        stats = get_thread_stats()
        stats.print_stats()
        stats.dump_stats("EPIC_stats.prof")

    log.info("Done")


if __name__ == "__main__":
    main()
