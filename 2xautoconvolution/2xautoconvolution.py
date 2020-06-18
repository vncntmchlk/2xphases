#!/usr/bin/python
#
# Autoconvolution for long audio files
#
# The autoconvolution can produce very interesting effects on audio (especially if the overall spectrum envelope is preserved)
# For loading non-wav files (like mp3, ogg, etc) or changing the sample_rate it requires "avconv"
# This software requires a lot of temporary hard drive space for processing 
#
# You can try this for a whole melody to get interesting effect.
#
# by Nasca Octavian PAUL, Targu Mures, Romania
# http://www.paulnasca.com/

# this file is released under Public Domain

import sys
import gc
import warnings
import contextlib
import struct
import os
import os.path
import subprocess
import tempfile
import glob
from collections import defaultdict

from scipy import signal,ndimage
import wave
import scipy.io.wavfile #for debugging

import numpy as np

from optparse import OptionParser
from tempfile import TemporaryFile

tmpextension=".npy"

def debug_write_wav(filename,sample_rate,smp):
    if len(smp)==0:
        smp=np.zeros(1)
    scipy.io.wavfile.write(filename,sample_rate,smp/(max(np.abs(smp))+1e-6))

def cleanup_memory():
    gc.collect()

def get_tmpfft_filename(tmpdir,block_k,nchannel):
    return os.path.join(tmpdir,"tmpfft_%d_%d" % (block_k,nchannel)+tmpextension)

def get_tmpsmp_filename(tmpdir,block_k):
    return os.path.join(tmpdir,"tmpsmp_%d" % (block_k)+tmpextension)


def optimize_fft_size(n):
    orig_n=n
    while True:
        n=orig_n
        while (n%2)==0:
            n/=2
        while (n%3)==0:
            n/=3
        if n<2:
            break
        orig_n+=1
    return orig_n

def get_block_mixes(n_blocks):
    pos=defaultdict(lambda:{})
    
    for j in range(n_blocks):
        for i in range(n_blocks):
            val=(min(i,j),max(i,j))
            if val not in pos[i+j]:
                pos[i+j][val]=0
            pos[i+j][val]+=1
    result=[v for k,v in pos.items()]
    return result

def ramp_window(smp,ramp_size):
    smp[:ramp_size]*=np.linspace(0.0,1.0,ramp_size)
    smp[-ramp_size:]*=np.linspace(1.0,0.0,ramp_size)

#keep envelope modes: 0 - don't keep envelope, 1 - don't keep envelope but align the sound, 2 - keep envelope
def process_audiofile(input_filename,output_filename,options,keep_envelope_mode):
    tmpdir=tempfile.mkdtemp("2xautoconvolution")
    print("Using temporary directory:", tmpdir)
   
    cmdline=["ffmpeg", "-y", "-v","quiet", "-i",input_filename]
    if options.sample_rate>0:
        cmdline+=["-ar",str(options.sample_rate)]
    tmp_wav_filename=os.path.join(tmpdir,"tmp_input.wav")
    cmdline.append(tmp_wav_filename)
    subprocess.call(cmdline)

    envelopes=None
    sample_rate=0
    with contextlib.closing(wave.open(tmp_wav_filename,'rb')) as f:
        sample_rate=f.getframerate()
    input_block_size_samples=int(optimize_fft_size(options.blocksize_seconds*sample_rate))
    print("Input block size (samples):",input_block_size_samples)
    input_ramp_size=0
    if keep_envelope_mode==0:
        output_block_size_samples=input_block_size_samples*2
    if keep_envelope_mode==1:
        output_block_size_samples=input_block_size_samples*3
    if keep_envelope_mode==2:
        print("Spectrum envelope preservation: enabled")
        envelopes=[]
        output_block_size_samples=input_block_size_samples*3
        if options.limit_blocks>0:
            input_ramp_size=int(10.0*(sample_rate/1000.0))



    if options.limit_blocks>0:
        print("Limiting to %d adjacent blocks; resulted spread size is %.1f seconds" % (options.limit_blocks,options.limit_blocks*float(input_block_size_samples)/sample_rate))
    
    extra_output_samples=output_block_size_samples-input_block_size_samples*2

    nchannels=0

    fft_size=output_block_size_samples // 2 + 1

    #read 16 bit wave files
    with contextlib.closing(wave.open(tmp_wav_filename,'rb')) as f:
        nsamples=f.getnframes()
        nchannels= f.getnchannels()

        if envelopes is not None:
            for nchannel in range(nchannels):
                envelopes.append(np.zeros(fft_size,dtype=np.float32))

        
        n_blocks=nsamples // input_block_size_samples+1

        #force adding extra zero block to flush out all the samples
        n_blocks+=1
        print("Using %d blocks" % n_blocks)

        #compute DC noise removal (removal of anything below 20Hz)
        b20hz, a20hz = signal.butter(3,20.0/(float(sample_rate)/2.0),btype="highpass")
        
        zi20=[]
        for nchannel in range(nchannels):
            zi20.append(signal.lfilter_zi(b20hz, a20hz))


        #analyse audio and make frequency blocks
        for block_k in range(n_blocks):
            print("Doing FFT for block %d/%d  \r" % (block_k+1,n_blocks), end=' ')
            sys.stdout.flush()
            inbuf=f.readframes(input_block_size_samples)
            freq_block=[]
            for nchannel in range(nchannels):
                smp=np.frombuffer(inbuf,dtype=np.int16)[nchannel::nchannels]

                smp=smp*(1.0/32768)
                smp, zi20[nchannel] = signal.lfilter(b20hz, a20hz, smp, zi=zi20[nchannel])
                smp=np.float32(smp)
                
                if 0<input_ramp_size*2<len(smp):
                    ramp_window(smp,input_ramp_size)
                
                smp=np.concatenate((smp,np.zeros(output_block_size_samples-len(smp),dtype=np.float32)))
                in_freqs=np.complex64(np.fft.rfft(smp))
                tmp_filename=get_tmpfft_filename(tmpdir,block_k,nchannel)

                if envelopes is not None:
                    envelopes[nchannel]+=np.abs(in_freqs)
                np.save(tmp_filename,in_freqs)

                del in_freqs
                del smp

                cleanup_memory()
            del inbuf
        cleanup_memory()

    print()
        
    
    #smooth envelopes
    if envelopes is not None:
        print("Smoothing envelopes")
        for nchannel in range(nchannels):
            one_hz_size_output=2.0*float(fft_size)/float(sample_rate)
            envelopes[nchannel]=ndimage.filters.maximum_filter1d(envelopes[nchannel],size=max(int(one_hz_size_output+0.5),2))+1e-9
    
    #get the freq blocks and combine them, saving each output chunk
    block_mixes=get_block_mixes(n_blocks)
   
    max_smp=np.float32(1e-6)
    for k,block_mix in enumerate(block_mixes):
        size_shown=len(block_mix)
        if options.limit_blocks>0:
            size_shown=min(size_shown,options.limit_blocks)
        print("Mixing blocks %d/%d (size %d)       \r" % (k+1,len(block_mixes),size_shown), end=' ')
        sys.stdout.flush()
        multichannel_smps=[]
        for nchannel in range(nchannels): 
            #print(output_block_size_samples)
            sum_freqs=np.zeros(output_block_size_samples // 2 + 1, dtype=np.complex64)
            for ((b1_k,b2_k),mul) in block_mix.items():
                if options.limit_blocks>0:
                    if abs(b1_k-b2_k)>options.limit_blocks: 
                        continue
                freq1=np.load(get_tmpfft_filename(tmpdir,b1_k,nchannel))
                freq2=np.load(get_tmpfft_filename(tmpdir,b2_k,nchannel))
                sum_freqs+=(freq1*freq2)*mul
                cleanup_memory()
            if envelopes is not None:
                sum_freqs=sum_freqs/envelopes[nchannel]
            smp=np.float32(np.fft.irfft(sum_freqs))
            cleanup_memory()
            if extra_output_samples>0:
                extra=extra_output_samples // 2
                smp=np.roll(smp,extra)
                ramp_window(smp,extra)
                #debug_write_wav(os.path.join("tmp/out_%d_%04d.wav" % (nchannel,k)),sample_rate,smp) 
                cleanup_memory()
            del sum_freqs
            max_current_smp=max(np.amax(smp),-np.amin(smp))
            max_smp=max(max_current_smp,max_smp)
            multichannel_smps.append(smp)
            del smp
            cleanup_memory()
        multichannel_smps=np.dstack(multichannel_smps)[0]
        np.save(get_tmpsmp_filename(tmpdir,k),multichannel_smps)
        del multichannel_smps
        cleanup_memory()

    print()
    print("Combining blocks")
    #get the output chunks, normalize them and combine to one wav file
    with contextlib.closing(wave.open(output_filename,'wb')) as f:
        f.setnchannels(nchannels)
        f.setframerate(sample_rate)
        f.setsampwidth(2)
        
        old_buf=[]
        for k in range(len(block_mixes)):
            print("Output block %d/%d \r" % (k+1,len(block_mixes)), end=' ')
            sys.stdout.flush()
            current_smps=np.float32(np.load(get_tmpsmp_filename(tmpdir,k))*(0.7/max_smp))
            current_buf=current_smps[:input_block_size_samples]
            result_buf=current_buf
                 
            old_buf=[o for o in old_buf if o.shape[0]>=input_block_size_samples]
            for oldk,old in enumerate(old_buf):
                result_buf+=old[:input_block_size_samples]
                old_buf[oldk]=old[input_block_size_samples:]
            old_buf.append(current_smps[input_block_size_samples:])

            output_buf=np.int16(np.clip(result_buf,-1.0,1.0)*32767.0).flatten().tostring()
            f.writeframes(output_buf)

            del result_buf
            del current_smps
            del current_buf
            del output_buf
            cleanup_memory()

    print()

    #cleanup
    cleanup_size=0
    for fn in glob.glob(os.path.join(tmpdir,"*"+tmpextension)):
        cleanup_size+=os.path.getsize(fn)
        os.remove(fn)
    cleanup_size+=os.path.getsize(tmp_wav_filename)
    os.remove(tmp_wav_filename)
    try:
        os.rmdir(tmpdir)
    except OSError:
        pass

    print("%.3f GB was temporary used." % (cleanup_size/1e9))
    print("Output was written in:",output_filename)



parser = OptionParser(usage="usage: %prog [options] -o output.wav input.wav")
parser.add_option("-o", "--output", dest="output",help="output WAV file",type="string",default="")
parser.add_option("-k", "--keep-envelope", dest="keep_envelope", action="store_true",help="try to preserve the overall amplitude envelope",default=False)
parser.add_option("-K", "--both-keep-envelope-modes", dest="both_keep_envelope_modes", action="store_true",help="output two files: one without keeping envelope and the other without keeping envelope",default=False)
parser.add_option("-b", "--blocksize_seconds", dest="blocksize_seconds",help="blocksize (seconds)",type="float",default=60.0)
parser.add_option("-l", "--limit_blocks", dest="limit_blocks",help="limit to adjacent L blocks in order to avoid mixing too distant parts of the audio file (default 0 = unlimited)",type="int",default=0)
parser.add_option("-r", "--sample_rate", dest="sample_rate",help="convert to sample_rate",type="int",default=0)
(options, args) = parser.parse_args()

if len(args)!=1 or len(options.output)==0:
    print("Error in command line parameters. Run this program with --help for help.")
    sys.exit(1)

input_filename=args[0]
print("Input file: "+input_filename)
if not os.path.isfile(input_filename):
    print("Error: Could not open input file:",input_filename)
    sys.exit(1)

if options.both_keep_envelope_modes:
    (output_base,output_ext)=os.path.splitext(options.output)
    print("Making two output files (with/without envelope keeping)")
    for keep_mode in [1,2]:
        output_file=output_base+"_k"+str(keep_mode)+output_ext
        print("Output file: "+output_file)
        process_audiofile(input_filename,output_file,options,keep_mode)        

else:
    print("Output file: "+options.output)
    process_audiofile(input_filename,options.output,options,2 if options.keep_envelope else 0)
print()




