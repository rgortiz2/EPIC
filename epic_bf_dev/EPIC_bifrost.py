#!/usr/bin/env python

import signal
import logging
import time
import json
import os
import sys
import threading
import argparse
import numpy
from scipy.fftpack import fft

import matplotlib.pyplot as plt

from bifrost.address import Address as BF_Address
from bifrost.udp_socket import UDPSocket as BF_UDPSocket
from bifrost.udp_capture import UDPCapture as BF_UDPCapture
from bifrost.udp_transmit import UDPTransmit as BF_UDPTransmit
from bifrost.ring import Ring
from bifrost.quantize import quantize as Quantize
from bifrost.proclog import ProcLog
from bifrost.libbifrost import bf

import bifrost
import bifrost.affinity


from lsl.common.constants import c as speedOfLight
from lsl.writer import fitsidi
from lsl.reader.ldp import TBNFile
from lsl.common.stations import lwasv, parseSSMIF

## TODO: Move this to argparse.

CHAN_BW = 25000	# Hz
ANTENNAS = lwasv.getAntennas()
DUMP_TIME = 5	# s

# Build map of where the LWA-SV Stations are.
## TODO: Make this non-SV specific
LOCATIONS = lwasv.getStands()

locations = numpy.empty(shape=(0,3))

for stand in LOCATIONS:

    locations = numpy.vstack((locations,[stand[0],stand[1],stand[2]]))
    print 'X: %f' % stand[0] #x
    print 'Y: %f' % stand[1] #y
    print 'Z: %f' % stand[2] #z

print locations
locations = numpy.delete(locations, list(range(0,locations.shape[0],2)),axis=0)
#locations = locations[0:255,:]
print ("Max X: %f, Max Y: %f" % (numpy.max(locations[:,0]), numpy.max(locations[:,1])))
print ("Min X: %f, Min Y: %f" % (numpy.min(locations[:,0]), numpy.min(locations[:,1])))
print numpy.shape(locations)

#for location in locations:
#    print location
RESOLUTION = 1.0
GRID_SIZE = int(numpy.power(2,numpy.ceil(numpy.log(numpy.max(numpy.abs(locations))/RESOLUTION)/numpy.log(2))))
print GRID_SIZE


#plt.plot(locations[:,0],locations[:,1],'x')
#plt.show()

## Antenna Illumination Pattern params:
# For now just use a square top-hat function..
## TODO: Do this properly...
Xmin= -1.5
Xmax= 1.5
Ymin= -1.5
Ymax= 1.5




class OfflineCaptureOp(object):
    def __init__(self, log, oring, filename, core=-1):
        self.log = log
        self.oring = oring
        self.filename = filename
        self.core = core

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
        bifrost.affinity.set_core(self.core)

        idf = TBNFile(self.filename)
	cfreq = idf.getInfo('freq1')
	srate = idf.getInfo('sampleRate')
	tInt, tStart, data = idf.read(0.1, timeInSamples=True)
	idf.reset()
	
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
	ohdr['chan0']    = int((cfreq - srate/2)/CHAN_BW)
	ohdr['nchan']    = 1
	ohdr['cfreq']    = cfreq
	ohdr['bw']       = srate
	ohdr['nstand']   = nstand
	ohdr['npol']     = npol
	ohdr['nbit']     = 8
	ohdr['complex']  = True
	ohdr_str = json.dumps(ohdr)
		
	## Fill the ring using the same data over and over again
	with self.oring.begin_writing() as oring:
	    with oring.begin_sequence(time_tag=tStart, header=ohdr_str) as oseq:
		prev_time = time.time()
		while not self.shutdown_event.is_set():
		    ## Get the current section to use
		    try:
			tInt, tStart, data = idf.read(0.1, timeInSamples=True)
		    except Exception as e:
			print "FillerOp: Error - '%s'" % str(e)
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
			
			curr_time = time.time()
			process_time = curr_time - prev_time
			prev_time = curr_time
			self.perf_proclog.update({'acquire_time': acquire_time, 
					          'reserve_time': reserve_time, 
					          'process_time': process_time,})
	print "FillerOp - Done"


class FDomainOp(object):
	def __init__(self, log, iring, oring, ntime_gulp=2500, nchan_out=4, core=-1):
		self.log = log
		self.iring = iring
		self.oring = oring
		self.ntime_gulp = ntime_gulp
		self.nchan_out = nchan_out
		self.core = core
		
		self.nchan_out = 4
		
		self.bind_proclog = ProcLog(type(self).__name__+"/bind")
		self.in_proclog   = ProcLog(type(self).__name__+"/in")
		self.out_proclog  = ProcLog(type(self).__name__+"/out")
		self.size_proclog = ProcLog(type(self).__name__+"/size")
		self.sequence_proclog = ProcLog(type(self).__name__+"/sequence0")
		self.perf_proclog = ProcLog(type(self).__name__+"/perf")
		
		self.in_proclog.update( {'nring':1, 'ring0':self.iring.name})
		self.out_proclog.update({'nring':1, 'ring0':self.oring.name})
		self.size_proclog.update({'nseq_per_gulp': self.ntime_gulp})
		
	def main(self):
		bifrost.affinity.set_core(self.core)
		self.bind_proclog.update({'ncore': 1, 
		                          'core0': bifrost.affinity.get_core(),})
		
		with self.oring.begin_writing() as oring:
			for iseq in self.iring.read(guarantee=True):
				ihdr = json.loads(iseq.header.tostring())
				
				self.sequence_proclog.update(ihdr)
				print 'FDomainOp: Config - %s' % ihdr
				
				# Setup the ring metadata and gulp sizes
				nchan  = self.nchan_out
				nstand = ihdr['nstand']
				npol   = ihdr['npol']
				
				igulp_size = self.ntime_gulp*1*nstand*npol * 8		# complex64
				ishape = (self.ntime_gulp/nchan,nchan,nstand,npol)
				ogulp_size = self.ntime_gulp*1*nstand*npol * 2		# ci8
				oshape = (self.ntime_gulp/nchan,nchan,nstand,npol,2)
				#self.iring.resize(igulp_size)
				self.oring.resize(ogulp_size)
				
				# Set the output header
				ohdr = ihdr.copy()
				ohdr['nchan'] = nchan
				ohdr['nbit']  = 8
				ohdr_str = json.dumps(ohdr)
				
				# Setup the phasing terms for zenith
				phases = numpy.zeros(ishape, dtype=numpy.complex64)
				freq = numpy.fft.fftfreq(nchan, d=1.0/ihdr['bw']) + ihdr['cfreq']
				for i in xrange(nstand):
					## X
					a = ANTENNAS[2*i + 0]  
					delay = a.cable.delay(freq) - a.stand.z / speedOfLight
					phases[:,:,i,0] = numpy.exp(2j*numpy.pi*freq*delay)
					## Y
					a = ANTENNAS[2*i + 1]
					delay = a.cable.delay(freq) - a.stand.z / speedOfLight
					phases[:,:,i,1] = numpy.exp(2j*numpy.pi*freq*delay)
					
				prev_time = time.time()
				with oring.begin_sequence(time_tag=iseq.time_tag, header=ohdr_str) as oseq:
					iseq_spans = iseq.read(igulp_size)
					while not self.iring.writing_ended():
						for ispan in iseq_spans:
							if ispan.size < igulp_size:
								continue # Ignore final gulp
							curr_time = time.time()
							acquire_time = curr_time - prev_time
							prev_time = curr_time
							
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
								fdata *= phases
								
								## Quantization
								try:
									Quantize(fdata, qdata, scale=1./numpy.sqrt(nchan))
								except NameError:
									qdata = bifrost.ndarray(shape=fdata.shape, native=False, dtype='ci8', space='system')
									Quantize(fdata, qdata, scale=1./numpy.sqrt(nchan))
									
								## Save
								rdata = qdata.view(numpy.int8).reshape(oshape)
								rdata = rdata.copy(space='cuda')
								odata[...] = rdata
								
							curr_time = time.time()
							process_time = curr_time - prev_time
							prev_time = curr_time
							self.perf_proclog.update({'acquire_time': acquire_time, 
							                          'reserve_time': reserve_time, 
							                          'process_time': process_time,})
				break	# Only do one pass through the loop
		print "FDomainOp - Done"

                

## For when we don't need to care about doing the F-Engine ourself.
## TODO: Implement this come implementation time...
class FEngineCaptureOp(object):
    '''
    Receives Fourier Spectra from LWA FPGA
    '''
    def __init__(self, log, *args, **kwargs):
        self.log = log
        self.args = args
        self.kwargs = kwargs

        self.shutdown_event = threading.Event()

    def shutdown(self):
        self.shutdown_event.set()

    def seq_callback(self):
        #What do I need to do to the data?
        pass

    def main(self):
        seq_callback = bf.BFudpcapture_sequence_callback(self.seq_callback)
        with UDPCapture(*self.args,
                        sequence_callback=seq_callback,
                        **self.kwargs) as capture:
            while not self.shutdown_event.is_set():
                status = capture.recv()
        del capture

class CalibrationOp(object):
    def __init__(self, log, iring, oring, *args, **kwargs):
        pass

class MOFFCorrelatorOp(object):
    def __init__(self, log, iring, oring, ntime_gulp=2500, core=-1, cpu=False, *args, **kwargs):
        self.log = log
        self.iring = iring
        self.oring = oring
        self.ntime_gulp = ntime_gulp

        self.core = core
        self.cpu = cpu
        
        self.grid = None
        self.image = None

    def main(self):
        bifrost.affinity.set_core(self.core)
        with self.oring.begin_writing() as oring:
            for iseq in self.iring.read(guarantee=True):
                
                ihdr = json.loads(iseq.header.tostring())
                nchan = ihdr['nchan']
                nstand = ihdr['nstand']
                npol = ihdr['npol']
                
                igulp_size = self.ntime_gulp * 1 * nstand * npol * 2
                ishape = (self.ntime_gulp/nchan,nchan,nstand,npol,2)

                ohdr = ihdr.copy()
                ohdr['nbit'] = 64
                ohdr['npol'] = npol
                ohdr['nchan'] = nchan
                ohdr_str = json.dumps(ohdr)
                
                ##TODO: Setup output gulp and shape.
                oshape = (nchan,npol,GRID_SIZE,GRID_SIZE)
                ogulp_size = nchan * npol * GRID_SIZE * GRID_SIZE * 8
                self.oring.resize(ogulp_size)
                self.grid = numpy.zeros(shape=(nchan,npol,GRID_SIZE,GRID_SIZE),dtype=numpy.complex64)
                self.grid = bifrost.ndarray(self.grid)
                self.image = numpy.zeros(shape=(nchan,npol,GRID_SIZE,GRID_SIZE),dtype=numpy.complex64)
                base_time_tag = iseq.time_tag
                while not self.iring.writing_ended():
                    iseq_spans = iseq.read(igulp_size)
                    for ispan in iseq_spans:

                        if ispan.size < igulp_size:
                            continue

                        for time in numpy.arange(100,101):

                            with oring.begin_sequence(time_tag=base_time_tag, header=ohdr_str) as oseq:
                                base_time_tag = base_time_tag + 1                                
                        
                                ###### Correlator #######
                                ## Check the casting of ingulp to bfidata ci8
                                idata = ispan.data_view(numpy.int8).reshape(ishape)
                                bfidata = bifrost.ndarray(shape=idata.shape, dtype='ci8', native=False, buffer=idata.ctypes.data)

                                if(self.cpu): #CPU

                                    # I am pretty sure I couldn't make this any less efficient if I tried. 
                                    for i in numpy.arange(nchan): 
                                        for j in numpy.arange(npol):
                                            for k in numpy.arange(nstand):

                                                #Get co-ordinates on the grid. Switch origin to centre
                                                x = int(numpy.round(locations[k,0] + GRID_SIZE/2))
                                                y = int(numpy.round(locations[k,1] + GRID_SIZE/2))
                                                # y and x should now correspond to the i/j co-ordinates of the grid.
                                                num = bfidata[time,i,k,0]
                                                if i == 2:
                                                    if j==0:
                                                        print num
                                                    
                                                for yi in numpy.arange(y+int(Ymin),y+int(Ymax)):
                                                    for xi in numpy.arange(x+int(Xmin),x+int(Xmax)):

                                                        #num = 1 + 1j
                                                        #print numpy.shape(num)
                                                        #print num[0][0]
                                                        #self.grid[i,j,xi,yi] += numpy.complex(1.0,1.0)
                                                        self.grid[i,j,xi,yi] += numpy.complex(float(num[0][0]),float(num[0][1]))
                                                        
                                                    
                                                
                                    #Now that unsightly business is concluded, let us FFT.
                                    for i in numpy.arange(nchan):
                                        for j in numpy.arange(npol):
                                            self.image[i,j,:,:] = numpy.fft.fftshift(self.grid[i,j,:,:])
                                            self.image[i,j,:,:] = numpy.fft.ifft2(self.image[i,j,:,:])
                                            self.image[i,j,:,:] = numpy.fft.fftshift(self.image[i,j,:,:])

                                    plt.figure(1)
                                    #print(numpy.shape(self.image[2,0,:,:]))
                                    #print(self.image[2,0,:,:])
                                    plt.imshow(numpy.abs(numpy.real(self.image[2,0,:,:])))
                                    plt.savefig("blahblah.png")
                                    print "Saved Image"
                                                
                                else: #GPU
                                    pass


                            
                        


                                with oseq.reserve(ogulp_size) as ospan:

                                    odata = ospan.data_view(numpy.complex64).reshape(oshape)
                                    odata[...] = self.image
                        

class ImagingOp(object):
    def __init__(self, log, iring, filename, nimage_gulp=100, core=-1,*args, **kwargs):
        self.log = log
        self.iring = iring
        self.filename
        self.nimage_gulp=100
        self.core = core

    def main(self):
        bifrost.affinity.set_core(self.core)

        for iseq in self.iring.read(guarantee=True):
            ihdr = json.loads(iseq.header.tostring())

            nchan = ihdr['nchan']
            npol = ihdr['npol']

            igulp_size = nimage_gulp * nchan * npol * GRID_SIZE * GRID_SIZE * 8
            ishape = nimage_gulp * nchan * npol * GRID_SIZE * GRID_SIZE 
            self.iring.resize(igulp_size)

            iseq_spans = iseq.read(igulp_size)
            while not self.iring.writing_ended():
                for ispan in iseq_spans:

                    idata = ispan.data_view(numpy.complex64).reshape(ishape)
                    #Square
                    idata = numpy.square(idata)
                    #Accumulate
                    ## TODO: Implement this.


                    #Save and output
                    
                    
                                
class SaveFFTOp(object):
    def __init__(self, log, iring, filename, ntime_gulp=2500, core=-1,*args, **kwargs):
        self.log = log
        self.iring = iring
        self.filename = filename
        self.core = core
        self.ntime_gulp = ntime_gulp

    def main(self):
        bifrost.affinity.set_core(self.core)

        for iseq in self.iring.read(guarantee=True):
            
            ihdr = json.loads(iseq.header.tostring())
            nchan = ihdr['nchan']
            nstand = ihdr['nstand']
            npol = ihdr['npol']
            
            
            igulp_size = self.ntime_gulp*1*nstand*npol * 2		# ci8
	    ishape = (self.ntime_gulp/nchan,nchan,nstand,npol,2)

            iseq_spans = iseq.read(igulp_size)

            while not self.iring.writing_ended():

                for ispan in iseq_spans:
                    if ispan.size < igulp_size:
                        continue

                    idata = ispan.data_view(numpy.int8)

                    idata = idata.reshape(ishape)
                    print(numpy.shape(idata))
                    numpy.savez(self.filename + "asdasd.npy",data=idata)
                    print("Wrote to disk")
            break
    print("Save F-Engine Spectra.. done")
                

def main():

    # Main Input: UDP Broadcast RX from F-Engine??

    parser = argparse.ArgumentParser(description='EPIC Correlator')
    parser.add_argument('-a', '--addr', type=str, help= 'F-Engine UDP Stream Address')
    parser.add_argument('-p', '--port', type=int, help= 'F-Engine UDP Stream Port')
    parser.add_argument('-o', '--offline', action='store_true', help = 'Load TBN data from Disk')
    parser.add_argument('-f', '--tbnfile', type=str, help = 'TBN Data Path')
    parser.add_argument('-c', '--cpuonly', action='store_true', help = 'Runs EPIC Correlator on CPU Only.')

    args = parser.parse_args()
    # Logging Setup
    # TODO: Set this up properly
    log = logging.getLogger(__name__)
    logFormat = logging.Formatter('%(asctime)s [%(levelname)-8s] %(message)s',
	                          datefmt='%Y-%m-%d %H:%M:%S')
    logFormat.converter = time.gmtime
    logHandler = logging.StreamHandler(sys.stdout)
    logHandler.setFormatter(logFormat)
    log.addHandler(logHandler)
    log.setLevel(logging.DEBUG)
    
    # Setup the cores and GPUs to use
    cores = [0, 1, 2, 3, 4, 5, 6, 7]
    gpus  = [0, 0, 0, 0, 0, 0, 0, 0]
        
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

#    utc_start_dt = get_utc_start(shutdown_event)
    fcapture_ring = Ring(name="capture")
    if args.offline:
        fdomain_ring = Ring(name="fengine")
    #Think flagging/gains should be done on CPU?
    #calibration_ring = Ring(name="gains", space="cuda")
    if args.cpuonly:
        gridandfft_ring = Ring(name="gridandfft")
    else:
        gridandfft_ring = Ring(name="gridandfft", space="cuda")
    #output_ring = Ring(name="output", space="cuda")
    
    ##TODO: Setup configuration file for sockets etc.

    
    #fengineaddr = Address(args.iaddr, args.iport)
    #fenginesocket = BF_UDPSocket()
    #fenginesocket.bind(fengineaddr)
    #fenginesocket.timeout=0.5


    ops.append(OfflineCaptureOp(log, fcapture_ring,
	                args.tbnfile, core=cores.pop(0)))
    ops.append(FDomainOp(log, fcapture_ring, fdomain_ring, 
	                 ntime_gulp=2500, core=cores.pop(0)))
    ops.append(MOFFCorrelatorOp(log, fdomain_ring, gridandfft_ring, ntime_gulp=2500, core=cores.pop(0), cpu=True))
#    ops.append(SaveFFTOp(log, fdomain_ring,"EPIC_test",ntime_gulp=2500,core=cores.pop(0)))

    #ops.append(CaptureOp(log, fmt="chips", sock=fenginesocket, ring=fcapture_ring,
    #                     nsrc=1, src0=0, max_payload_size=9000, buffer_ntime=500,
    #                     slot_ntime=25000, core=cores.pop(0), utc_start=utc_start_dt))
    #ops.append(CalibrationOp(log, iring = fcapture_ring, oring = calibration_ring))
    #ops.append(GridandFFTOp(log, iring = calibration_ring, oring=gridandfft_ring))
    #ops.append(CopyOp(log))

    threads= [threading.Thread(target=op.main) for op in ops]

    for thread in threads:
        thread.daemon = False
        thread.start()

    while threads[0].is_alive() and not shutdown_event.is_set():
        time.sleep(0.5)
    for thread in threads:
        thread.join()

if __name__ == "__main__":
    main()
    
    