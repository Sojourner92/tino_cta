#!/usr/bin/env python

from sys import exit

from collections import namedtuple

from glob import glob

import numpy as np
import matplotlib.pyplot as plt

from astropy import units as u

from ctapipe.calib import CameraCalibrator
from ctapipe.io.hessio import hessio_event_source

from ctapipe.utils import linalg
from ctapipe.utils.CutFlow import CutFlow

from ctapipe.image.hillas import HillasParameterizationError, \
    hillas_parameters_4 as hillas_parameters

from helper_functions import *
from modules.ImageCleaning import ImageCleaner, EdgeEvent

try:
    from ctapipe.reco.event_classifier import *
    print("using ctapipe event_classifier")
except ImportError:
    from modules.event_classifier import *
    print("using tino_cta event_classifier")

try:
    from ctapipe.reco.energy_regressor import *
    print("using ctapipe energy_regressor")
except:
    from modules.energy_regressor import *
    print("using tino_cta energy_regressor")

try:
    from modules.HillasReconstructor import HillasReconstructor, \
        TooFewTelescopesException
    print("using tino_cta HillasReconstructor")
except:
    from ctapipe.reco.HillasReconstructor import \
        HillasReconstructor, TooFewTelescopesException
    print("using ctapipe.reco.HillasReconstructor")

from modules.prepare_event import EventPreparator

# PyTables
try:
    import tables as tb
except:
    print("no pytables installed")


# which models to load for classifier/regressor
cam_id_list = [
        # 'GATE',
        # 'HESSII',
        'NectarCam',
        'LSTCam',
        'DigiCam',
        # 'SST-1m',
        # 'FlashCam',
        # 'ASTRICam',
        # 'SCTCam',
]


def main():

    # your favourite units here
    energy_unit = u.TeV
    angle_unit = u.deg
    dist_unit = u.m

    agree_threshold = .5
    min_tel = 3

    parser = make_argparser()
    parser.add_argument('--classifier', type=str,
                        default='data/classifier_pickle/classifier'
                                '_prod3b_{mode}_{cam_id}_{classifier}.pkl')
    parser.add_argument('--regressor', type=str,
                        default='data/classifier_pickle/regressor'
                                '_prod3b_{mode}_{cam_id}_{regressor}.pkl')
    parser.add_argument('-o', '--out_file', type=str,
                        default="data/reconstructed_events/classified_events_{}_{}.h5",
                        help="location to write the classified events to. placeholders "
                             "are meant as {particle type} and {cleaning mode}")
    parser.add_argument('--proton',  action='store_true',
                        help="do protons instead of gammas")
    parser.add_argument('--wave_dir',  type=str, default=None,
                        help="directory where to find mr_filter. "
                             "if not set look in $PATH")
    parser.add_argument('--wave_temp_dir',  type=str, default='/tmp/',
                        help="directory where mr_filter to store the temporary fits files"
                        )

    args = parser.parse_args()

    if args.infile_list:
        filenamelist = []
        for f in args.infile_list:
            filenamelist += glob("{}/{}".format(args.indir, f))
        filenamelist.sort()
    elif args.proton:
        filenamelist = sorted(glob("{}/proton/*gz".format(args.indir)))[100:]
    else:
        filenamelist = sorted(glob("{}/gamma/*gz".format(args.indir)))[14:]

    if not filenamelist:
        print("no files found; check indir: {}".format(args.indir))
        exit(-1)


    # keeping track of events and where they were rejected
    Eventcutflow = CutFlow("EventCutFlow")
    Imagecutflow = CutFlow("ImageCutFlow")

    # takes care of image cleaning
    cleaner = ImageCleaner(mode=args.mode, cutflow=Imagecutflow,
                           wavelet_options=args.raw,
                           skip_edge_events=False, island_cleaning=True)

    # the class that does the shower reconstruction
    shower_reco = HillasReconstructor()

    preper = EventPreparator(calib=None, cleaner=cleaner,
                             hillas_parameters=hillas_parameters, shower_reco=shower_reco,
                             event_cutflow=Eventcutflow, image_cutflow=Imagecutflow,
                             # event/image cuts:
                             allowed_cam_ids=[],
                             min_ntel=2,
                             min_charge=args.min_charge, min_pixel=3)

    # wrapper for the scikit-learn classifier
    classifier = EventClassifier.load(
                    args.classifier.format(**{
                            "mode": args.mode,
                            "wave_args": "mixed",
                            "classifier": 'RandomForestClassifier',
                            "cam_id": "{cam_id}"}),
                    cam_id_list=cam_id_list)

    # wrapper for the scikit-learn regressor
    regressor = EnergyRegressor.load(
                    args.regressor.format(**{
                            "mode": args.mode,
                            "wave_args": "mixed",
                            "regressor": "RandomForestRegressor",
                            "cam_id": "{cam_id}"}),
                    cam_id_list=cam_id_list)

    ClassifierFeatures = namedtuple("ClassifierFeatures", (
                                "impact_dist",
                                "sum_signal_evt",
                                "max_signal_cam",
                                "sum_signal_cam",
                                "N_LST",
                                "N_MST",
                                "N_SST",
                                "width",
                                "length",
                                "skewness",
                                "kurtosis",
                                "h_max",
                                "err_est_pos",
                                "err_est_dir"))

    EnergyFeatures = namedtuple("EnergyFeatures", (
                                "impact_dist",
                                "sum_signal_evt",
                                "max_signal_cam",
                                "sum_signal_cam",
                                "N_LST",
                                "N_MST",
                                "N_SST",
                                "width",
                                "length",
                                "skewness",
                                "kurtosis",
                                "h_max",
                                "err_est_pos",
                                "err_est_dir"))

    # catch ctr-c signal to exit current loop and still display results
    signal_handler = SignalHandler()
    signal.signal(signal.SIGINT, signal_handler)

    # this class defines the reconstruction parameters to keep track of
    class RecoEvent(tb.IsDescription):
        NTels_trig = tb.Int16Col(dflt=1, pos=0)
        NTels_reco = tb.Int16Col(dflt=1, pos=1)
        NTels_reco_lst = tb.Int16Col(dflt=1, pos=2)
        NTels_reco_mst = tb.Int16Col(dflt=1, pos=3)
        NTels_reco_sst = tb.Int16Col(dflt=1, pos=4)
        MC_Energy = tb.Float32Col(dflt=1, pos=5)
        reco_Energy = tb.Float32Col(dflt=1, pos=6)
        phi = tb.Float32Col(dflt=1, pos=7)
        theta = tb.Float32Col(dflt=1, pos=8)
        off_angle = tb.Float32Col(dflt=1, pos=9)
        ErrEstPos = tb.Float32Col(dflt=1, pos=10)
        gammaness = tb.Float32Col(dflt=1, pos=11)

    channel = "gamma" if "gamma" in " ".join(filenamelist) else "proton"
    reco_outfile = tb.open_file(
            # trying to put particle type and cleaning mode into the filename
            # `format` puts in each argument as long as there is a free "{}" token
            # if `out_file` was set without any "{}", nothing will be replaced
            args.out_file.format(channel, args.mode), mode="w",
            # if we don't want to write the event list to disk, need to add more arguments
            **({} if args.store else {"driver": "H5FD_CORE",
                                      "driver_core_backing_store": False}))
    reco_table = reco_outfile.create_table("/", "reco_events", RecoEvent)
    reco_event = reco_table.row

    source_orig = None

    allowed_tels = None  # all telescopes
    allowed_tels = prod3b_tel_ids("L+N+D")
    for filename in filenamelist[:args.last]:
        print("filename = {}".format(filename))

        source = hessio_event_source(filename,
                                     allowed_tels=allowed_tels,
                                     max_events=args.max_events)

        # loop that cleans and parametrises the images and performs the reconstruction
        for (event, hillas_dict, n_tels,
             tot_signal, max_signals, pos_fit, dir_fit, h_max,
             err_est_pos, err_est_dir) in preper.prepare_event(source):

            n_lst, n_mst, n_sst = n_tels["LST"], n_tels["MST"], n_tels["SST"]

            # now prepare the features for the classifier
            cls_features_evt = {}
            reg_features_evt = {}
            for tel_id in hillas_dict.keys():
                Imagecutflow.count("pre-features")

                tel_pos = np.array(event.inst.tel_pos[tel_id][:2]) * u.m

                moments = hillas_dict[tel_id]

                impact_dist = linalg.length(tel_pos-pos_fit)
                cls_features_tel = ClassifierFeatures(
                            impact_dist/u.m,
                            tot_signal,
                            max_signals[tel_id],
                            moments.size,
                            n_tels["LST"],
                            n_tels["MST"],
                            n_tels["SST"],
                            moments.width/u.m,
                            moments.length/u.m,
                            moments.skewness,
                            moments.kurtosis,
                            h_max/u.m,
                            err_est_pos/u.m,
                            err_est_dir/u.deg
                            )

                reg_features_tel = EnergyFeatures(
                            impact_dist/u.m,
                            tot_signal,
                            max_signals[tel_id],
                            moments.size,
                            n_tels["LST"],
                            n_tels["MST"],
                            n_tels["SST"],
                            moments.width/u.m,
                            moments.length/u.m,
                            moments.skewness,
                            moments.kurtosis,
                            h_max/u.m,
                            err_est_pos/u.m,
                            err_est_dir/u.deg
                            )

                if np.isnan(cls_features_tel).any() or np.isnan(reg_features_tel).any():
                    continue

                Imagecutflow.count("features nan")

                cam_id = event.inst.subarray.tel[tel_id].camera.cam_id

                try:
                    reg_features_evt[cam_id] += [reg_features_tel]
                    cls_features_evt[cam_id] += [cls_features_tel]
                except KeyError:
                    reg_features_evt[cam_id] = [reg_features_tel]
                    cls_features_evt[cam_id] = [cls_features_tel]

            if not cls_features_evt or not reg_features_evt:
                continue

            predict_energ = regressor.predict_by_event([reg_features_evt])["mean"][0]
            predict_proba = classifier.predict_proba_by_event([cls_features_evt])
            gammaness = predict_proba[0, 0]

            # the MC direction of origin of the simulated particle
            source_orig = linalg.set_phi_theta(
                event.mc.tel[tel_id].azimuth_raw * u.rad,
                (np.pi/2-event.mc.tel[tel_id].altitude_raw)*u.rad)

            # and how the reconstructed direction compares to that
            off_angle = linalg.angle(dir_fit, source_orig)
            phi, theta = linalg.get_phi_theta(dir_fit)
            phi = (phi if phi > 0 else phi+360*u.deg)

            reco_event["NTels_trig"] = len(event.dl0.tels_with_data)
            reco_event["NTels_reco"] = len(hillas_dict)
            reco_event["NTels_reco_lst"] = n_lst
            reco_event["NTels_reco_mst"] = n_mst
            reco_event["NTels_reco_sst"] = n_sst
            reco_event["MC_Energy"] = event.mc.energy.to(energy_unit).value
            reco_event["reco_Energy"] = predict_energ.to(energy_unit).value
            reco_event["phi"] = phi / angle_unit
            reco_event["theta"] = theta / angle_unit
            reco_event["off_angle"] = off_angle / angle_unit
            reco_event["ErrEstPos"] = err_est_pos / dist_unit
            reco_event["gammaness"] = gammaness
            reco_event.append()
            reco_table.flush()

            if signal_handler.stop:
                break
        if signal_handler.stop:
            break

    print()
    Eventcutflow()
    print()
    Imagecutflow()

    # do some simple event selection and print the corresponding selection efficiency
    N_selected = len([x for x in reco_table.where(
        """(NTels_reco > min_tel) & (gammaness > agree_threshold)""")])
    N_total = len(reco_table)
    print("\nfraction selected events:")
    print("{} / {} = {} %".format(N_selected, N_total, N_selected/N_total*100))

    print("\nlength filenamelist:", len(filenamelist[:args.last]))

    # do some plotting if so desired
    if args.plot:
        gammaness = [x['gammaness'] for x in reco_table]
        NTels_rec = [x['NTels_reco'] for x in reco_table]
        NTel_bins = np.arange(np.min(NTels_rec), np.max(NTels_rec)+2) - .5

        NTels_rec_lst = [x['NTels_reco_lst'] for x in reco_table]
        NTels_rec_mst = [x['NTels_reco_mst'] for x in reco_table]
        NTels_rec_sst = [x['NTels_reco_sst'] for x in reco_table]

        reco_energy = np.array([x['reco_Energy'] for x in reco_table])
        mc_energy = np.array([x['MC_Energy'] for x in reco_table])

        fig = plt.figure(figsize=(15, 5))
        plt.suptitle(" ** ".join([args.mode, "protons" if args.proton else "gamma"]))
        plt.subplots_adjust(left=0.05, right=0.97, hspace=0.39, wspace=0.2)

        ax = plt.subplot(131)
        histo = np.histogram2d(NTels_rec, gammaness,
                               bins=(NTel_bins, np.linspace(0, 1, 11)))[0].T
        histo_normed = histo / histo.max(axis=0)
        im = ax.imshow(histo_normed, interpolation='none', origin='lower',
                       aspect='auto',
                       # extent=(*NTel_bins[[0, -1]], 0, 1),
                       cmap=plt.cm.inferno)
        ax.set_xlabel("NTels")
        ax.set_ylabel("drifted gammaness")
        plt.title("Total Number of Telescopes")

        # next subplot

        ax = plt.subplot(132)
        histo = np.histogram2d(NTels_rec_sst, gammaness,
                               bins=(NTel_bins, np.linspace(0, 1, 11)))[0].T
        histo_normed = histo / histo.max(axis=0)
        im = ax.imshow(histo_normed, interpolation='none', origin='lower',
                       aspect='auto',
                       # extent=(*NTel_bins[[0, -1]], 0, 1),
                       cmap=plt.cm.inferno)
        ax.set_xlabel("NTels")
        plt.setp(ax.get_yticklabels(), visible=False)
        plt.title("Number of SSTs")

        # next subplot

        ax = plt.subplot(133)
        histo = np.histogram2d(NTels_rec_mst, gammaness,
                               bins=(NTel_bins, np.linspace(0, 1, 11)))[0].T
        histo_normed = histo / histo.max(axis=0)
        im = ax.imshow(histo_normed, interpolation='none', origin='lower',
                       aspect='auto',
                       # extent=(*NTel_bins[[0, -1]], 0, 1),
                       cmap=plt.cm.inferno)
        cb = fig.colorbar(im, ax=ax)
        ax.set_xlabel("NTels")
        plt.setp(ax.get_yticklabels(), visible=False)
        plt.title("Number of MSTs")

        plt.subplots_adjust(wspace=0.05)

        # plot the energy migration matrix
        plt.figure()
        plt.hist2d(np.log10(reco_energy), np.log10(mc_energy), bins=20,
                   cmap=plt.cm.inferno)
        plt.xlabel("E_MC / TeV")
        plt.ylabel("E_rec / TeV")
        plt.colorbar()

        plt.show()


if __name__ == '__main__':
    main()
