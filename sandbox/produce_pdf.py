from sys import exit
from glob import glob
import argparse
from ctapipe.io.hessio import hessio_event_source
from FitGammaLikelihood import FitGammaLikelihood

from Telescope_Mask import TelDict

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='show single telescope')
    parser.add_argument('-m', '--max-events', type=int, default=None)
    parser.add_argument('-i', '--indir',   type=str, 
                        default="/local/home/tmichael/software/corsika_simtelarray/Data/sim_telarray/cta-ultra6/0.0deg/Data/gamma_20deg_180deg_run")
    parser.add_argument('-r', '--runnr',   type=str, default="*")
    parser.add_argument('-t', '--teltype', type=str, default="LST")
    args = parser.parse_args()
    
    filenamelist = glob( "{}*run{}*gz".format(args.indir,args.runnr ))
    if len(filenamelist) == 0: 
        print("no files found; check indir: {}".format(args.indir))
        exit(-1)
        
    fit = FitGammaLikelihood()

    for filename in filenamelist:
        print("filename = {}".format(filename))
        
        source = hessio_event_source(filename,
                                    # for now use only identical telescopes...
                                    allowed_tels=TelDict[args.teltype],
                                    max_events=args.max_events)

        for event in source:

            print('Scanning input file... count = {}'.format(event.count))
            print('available telscopes: {}'.format(event.dl0.tels_with_data))

            fit.fill_pdf(event=event)
    if args.runnr == '*':
        fit.write_raw("pdf/{}_raw.npz".format(args.teltype))
    else:
        fit.write_raw("pdf/{}_{}_raw.npz".format(args.teltype, args.runnr))
        