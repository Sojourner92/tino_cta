from sys import exit

from collections import namedtuple

from glob import glob

import numpy as np
import matplotlib.pyplot as plt

from astropy import units as u

from ctapipe.calib import CameraCalibrator
from ctapipe.plotting.camera import CameraGeometry
from ctapipe.io.hessio import hessio_event_source

from ctapipe.utils import linalg

from ctapipe.image.hillas import HillasParameterizationError, \
    hillas_parameters_4 as hillas_parameters

from helper_functions import *
from modules.CutFlow import CutFlow
from modules.ImageCleaning import ImageCleaner, EdgeEventException

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
    from ctapipe.reco.FitGammaHillas import \
        FitGammaHillas as HillasReconstructor, TooFewTelescopesException
except:
    from ctapipe.reco.HillasReconstructor import \
        HillasReconstructor, TooFewTelescopesException


# PyTables
try:
    import tables as tb
except:
    print("no pytables installed")


cam_id_list = [
        # 'GATE',
        # 'HESSII',
        # 'NectarCam',
        # 'LSTCam',
        # 'SST-1m',
        'FlashCam',
        'ASTRICam',
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
                                '_{mode}_{wave_args}_{classifier}_{cam_id}.pkl')
    parser.add_argument('--regressor', type=str,
                        default='data/classifier_pickle/regressor'
                                '_{mode}_{wave_args}_{regressor}_{cam_id}.pkl')
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

    if len(filenamelist) == 0:
        print("no files found; check indir: {}".format(args.indir))
        exit(-1)

    cam_geom = {}
    tel_phi = {}
    tel_theta = {}
    tel_orientation = (tel_phi, tel_theta)

    # counting events and where they might have gone missing
    Eventcutflow = CutFlow("EventCutFlow")
    Eventcutflow.set_cut("noCuts", None)
    Eventcutflow.set_cut("min2Tels trig", lambda x: x < 2)
    Eventcutflow.set_cut("min2Tels reco", lambda x: x < 2)

    Imagecutflow = CutFlow("ImageCutFlow")
    Imagecutflow.set_cut("min charge", lambda x: x < args.min_charge)

    # pass in config and self if part of a Tool
    calib = CameraCalibrator(None, None)

    # use this in the selection of the gain channels
    np_true_false = np.array([[True], [False]])

    # class that wraps tail cuts and wavelet cleaning
    Cleaner = ImageCleaner(mode=args.mode, wavelet_options=args.raw,
                           tmp_files_directory=args.wave_temp_dir,
                           mrfilter_directory=args.wave_dir,
                           skip_edge_events=False)  # args.skip_edge_events)

    # simple hillas-based shower reco
    fit = HillasReconstructor()

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
                                    "err_est_pos"))

    EnergyFeatures = namedtuple("EnergyFeatures", (
                                    "impact_dist",
                                    "sum_signal_evt",
                                    "max_signal_cam",
                                    "sum_signal_cam",
                                    "N_LST",
                                    "N_MST",
                                    "N_SST",
                                    #  "cen_r2",
                                    #  "phi""_minus_""psi",
                                    "width",
                                    "length",
                                    "skewness",
                                    "kurtosis",
                                    "err_est_pos"))

    LST_List = ["LSTCam"]
    MST_List = ["NectarCam", "FlashCam"]
    SST_List = ["ASTRICam", "SCTCam", "GATE", "DigiCam", "CHEC"]

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
    allowed_tels = range(10)  # smallest ASTRI array
    # allowed_tels = range(34)  # all ASTRI telescopes
    allowed_tels = range(34, 39)  # FlashCam telescopes
    allowed_tels = np.arange(10).tolist() + np.arange(34, 39).tolist()
    for filename in filenamelist[:args.last]:
        print("filename = {}".format(filename))

        source = hessio_event_source(filename,
                                     allowed_tels=allowed_tels,
                                     max_events=args.max_events)

        # event loop
        for event in source:

            Eventcutflow.count("noCuts")

            mc_shower = event.mc
            mc_shower_core = np.array([mc_shower.core_x.value,
                                       mc_shower.core_y.value]) * u.m

            if Eventcutflow.cut("min2Tels trig", len(event.dl0.tels_with_data)):
                continue

            # calibrate the event
            calib.calibrate(event)

            # telescope loop
            tot_signal = 0
            max_signal = 0
            n_lst = 0
            n_mst = 0
            n_sst = 0
            hillas_dict = {}
            for tel_id in event.dl0.tels_with_data:
                Imagecutflow.count("noCuts")

                if source_orig is None:
                    source_orig = linalg.set_phi_theta(
                        event.mc.tel[tel_id].azimuth_raw * u.rad,
                        (np.pi/2-event.mc.tel[tel_id].altitude_raw)*u.rad)

                # pmt_signal = apply_mc_calibration_ASTRI(
                #                 event.r0.tel[tel_id].adc_sums,
                #                 event.mc.tel[tel_id].dc_to_pe,
                #                 event.mc.tel[tel_id].pedestal)

                # calibrate the image and pick the proper gain channel if necessary
                pmt_signal = event.dl1.tel[tel_id].image
                if pmt_signal.shape[0] > 1:
                    pick = (pmt_signal > 14).any(axis=0) != np_true_false
                    pmt_signal = pmt_signal.T[pick.T]
                else:
                    pmt_signal = pmt_signal.ravel()

                max_signal = np.max(pmt_signal)

                # guessing camera geometry
                if tel_id not in cam_geom:
                    cam_geom[tel_id] = CameraGeometry.guess(
                                        event.inst.pixel_pos[tel_id][0],
                                        event.inst.pixel_pos[tel_id][1],
                                        event.inst.optical_foclen[tel_id])
                    tel_phi[tel_id] = event.mc.tel[tel_id].azimuth_raw * u.rad
                    tel_theta[tel_id] = (np.pi/2-event.mc.tel[tel_id].altitude_raw)*u.rad

                # counting different sized telescops
                if cam_geom[tel_id].cam_id in LST_List:
                    n_lst += 1
                elif cam_geom[tel_id].cam_id in MST_List:
                    n_mst += 1
                elif cam_geom[tel_id].cam_id in SST_List:
                    n_sst += 1
                else:
                    raise ValueError(
                            "unknown camera id: {}".format(cam_geom[tel_id].cam_id) +
                            "-- please add to corresponding list")

                # switch off FlashCam for now (but still count them in n_mst)
                if cam_geom[tel_id].cam_id is "FlashCam":
                    continue

                # trying to clean the image
                try:
                    pmt_signal, new_geom = \
                        Cleaner.clean(pmt_signal.copy(), cam_geom[tel_id])

                    if np.count_nonzero(pmt_signal) < 3:
                        continue

                    if Imagecutflow.cut("min charge", np.sum(pmt_signal)):
                        continue

                except FileNotFoundError as e:
                    print(e)
                    continue
                except EdgeEventException:
                    continue

                Imagecutflow.count("cleaning")

                # the hillas reconstruction of the images
                try:
                    moments = hillas_parameters(new_geom.pix_x,
                                                new_geom.pix_y,
                                                pmt_signal)

                    if not (moments.width > 0 and moments.length > 0):
                        continue

                except HillasParameterizationError as e:
                    print(e)
                    print("ignoring this camera")
                    continue

                hillas_dict[tel_id] = moments
                tot_signal += moments.size

                Imagecutflow.count("Hillas")

            if Eventcutflow.cut("min2Tels reco", len(hillas_dict)):
                continue

            try:
                # telescope loop done, now do the core fit
                fit.get_great_circles(hillas_dict,
                                      event.inst,
                                      *tel_orientation)
                pos_fit_cr, err_est_pos = fit.fit_core_crosses()
            except Exception as e:
                print(e)
                continue

            if np.isnan(pos_fit_cr.value).any():
                continue

            pos_fit = pos_fit_cr

            Eventcutflow.count("position fit")

            # now prepare the features for the classifier
            cls_features_evt = {}
            reg_features_evt = {}
            for tel_id in hillas_dict.keys():
                Imagecutflow.count("pre-features")

                tel_pos = np.array(event.inst.tel_pos[tel_id][:2]) * u.m

                moments = hillas_dict[tel_id]

                impact_dist_sim = linalg.length(tel_pos-mc_shower_core)
                impact_dist_rec = linalg.length(tel_pos-pos_fit)
                cls_features_tel = ClassifierFeatures(
                            impact_dist_rec/u.m,
                            tot_signal,
                            max_signal,
                            moments.size,
                            n_lst,
                            n_mst,
                            n_sst,
                            moments.width/u.m,
                            moments.length/u.m,
                            moments.skewness,
                            moments.kurtosis,
                            err_est_pos/u.m)

                reg_features_tel = EnergyFeatures(
                            impact_dist_rec/u.m,
                            tot_signal,
                            max_signal,
                            moments.size,
                            n_lst, n_mst, n_sst,
                            # the distance of the shower core from the centre
                            # since the camera might lose efficiency towards the edge
                            # (not a good idea for on-axis point-source MC)
                            # (moments.cen_x**2 +
                            #  moments.cen_y**2) / u.m**2,
                            # orientation of the hillas ellipsis wrt. the camera centre
                            # (moments.phi -
                            #  moments.psi)/u.deg,
                            moments.width/u.m,
                            moments.length/u.m,
                            moments.skewness,
                            moments.kurtosis,
                            err_est_pos/u.m
                          )

                if np.isnan(cls_features_tel).any() or np.isnan(reg_features_tel).any():
                    continue

                Imagecutflow.count("features nan")

                cam_id = cam_geom[tel_id].cam_id

                if cam_id not in reg_features_evt:
                    reg_features_evt[cam_id] = [reg_features_tel]
                else:
                    reg_features_evt[cam_id] += [reg_features_tel]

                if cam_id not in cls_features_evt:
                    cls_features_evt[cam_id] = [cls_features_tel]
                else:
                    cls_features_evt[cam_id] += [cls_features_tel]

            if not cls_features_evt or not reg_features_evt:
                continue

            predict_energ = regressor.predict_by_event([reg_features_evt])["mean"][0]
            predict_proba = classifier.predict_proba_by_event([cls_features_evt])
            gammaness = predict_proba[0, 0]

            fit_dir, crossings = fit.fit_origin_crosses()

            off_angle = linalg.angle(fit_dir, source_orig)
            phi, theta = linalg.get_phi_theta(fit_dir)
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

    Eventcutflow()
    Imagecutflow()

    if args.plot:
        gammaness = [x['gammaness'] for x in reco_table]
        NTels_rec = [x['NTels_reco'] for x in reco_table]
        NTel_bins = np.arange(np.min(NTels_rec), np.max(NTels_rec)+2)

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
                       aspect='auto', extent=(*NTel_bins[[0, -1]], 0, 1),
                       cmap=plt.cm.inferno)
        cb = fig.colorbar(im, ax=ax)
        ax.set_xlabel("NTels")
        ax.set_ylabel("drifted gammaness")
        plt.title("Total Number of Telescopes")




        ax = plt.subplot(132)
        histo = np.histogram2d(NTels_rec_sst, gammaness,
                               bins=(NTel_bins, np.linspace(0, 1, 11)))[0].T
        histo_normed = histo / histo.max(axis=0)
        im = ax.imshow(histo_normed, interpolation='none', origin='lower',
                       aspect='auto', extent=(*NTel_bins[[0, -1]], 0, 1),
                       cmap=plt.cm.inferno)
        cb = fig.colorbar(im, ax=ax)
        ax.set_xlabel("NTels")
        ax.set_ylabel("drifted gammaness")
        plt.title("Number of SSTs")




        ax = plt.subplot(133)
        histo = np.histogram2d(NTels_rec_mst, gammaness,
                               bins=(NTel_bins, np.linspace(0, 1, 11)))[0].T
        histo_normed = histo / histo.max(axis=0)
        im = ax.imshow(histo_normed, interpolation='none', origin='lower',
                       aspect='auto', extent=(*NTel_bins[[0, -1]], 0, 1),
                       cmap=plt.cm.inferno)
        cb = fig.colorbar(im, ax=ax)
        ax.set_xlabel("NTels")
        ax.set_ylabel("drifted gammaness")
        plt.title("Number of MSTs")


        # plot the energy migration matrix
        plt.figure()
        plt.hist2d(np.log10(reco_energy), np.log10(mc_energy), bins=20,
                   cmap=plt.cm.inferno)
        plt.colorbar()

    # do some simple event selection and print the corresponding selection efficiency
    N_selected = len([x for x in reco_table.where(
        """(NTels_reco > min_tel) & (gammaness > agree_threshold)""")])
    N_total = len(reco_table)
    print("\nfraction selected events:")
    print("{} / {} = {} %".format(N_selected, N_total, N_selected/N_total*100))

    print("\nlength filenamelist:", len(filenamelist[:args.last]))

    plt.show()


if __name__ == '__main__':
    main()
