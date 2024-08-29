# Author: Tomas Hodan (hodantom@cmp.felk.cvut.cz)
# Center for Machine Perception, Czech Technical University in Prague

"""Evaluation script for the BOP Challenge 2023/2024."""

import os
import time
import argparse
import multiprocessing
import subprocess
import numpy as np

from bop_toolkit_lib import config
from bop_toolkit_lib import inout
from bop_toolkit_lib import misc

# Get the base name of the file without the .py extension
file_name = os.path.splitext(os.path.basename(__file__))[0]
logger = misc.get_logger(file_name)

# PARAMETERS (some can be overwritten by the command line arguments below).
################################################################################
p = {
    # Errors to calculate.
    "errors": [
        {
            "n_top": 0,
            "type": "mssd",
            "correct_th": [[th] for th in np.arange(0.05, 0.51, 0.05)],
        },
        {
            "n_top": 0,
            "type": "mspd",
            "correct_th": [[th] for th in np.arange(5, 51, 5)],
        },
    ],
    # Minimum visible surface fraction of a valid GT pose.
    # by default, we consider only objects that are at least 10% visible
    "visib_gt_min": 0.1,
    # Whether to use the visible surface fraction of a valid GT pose in the 6D detection
    "ignore_object_visible_less_than_visib_gt_min": True,
    # See misc.get_symmetry_transformations().
    "max_sym_disc_step": 0.01,
    # Type of the renderer (used for the VSD pose error function).
    "renderer_type": "vispy",  # Options: 'vispy', 'cpp', 'python'.
    # Names of files with results for which to calculate the errors (assumed to be
    # stored in folder p['results_path']). See docs/bop_challenge_2019.md for a
    # description of the format. Example results can be found at:
    # https://bop.felk.cvut.cz/media/data/bop_sample_results/bop_challenge_2019_sample_results.zip
    "result_filenames": [
        "/relative/path/to/csv/with/results",
    ],
    # Folder with results to be evaluated.
    "results_path": config.results_path,
    # Folder for the calculated pose errors and performance scores.
    "eval_path": config.eval_path,
    # File with a list of estimation targets to consider. The file is assumed to
    # be stored in the dataset folder.
    "targets_filename": "test_targets_bop24.json",
    "num_workers": config.num_workers,  # Number of parallel workers for the calculation of errors.
    "use_gpu": config.use_gpu,  # Use torch for the calculation of errors.
}
################################################################################


# Command line arguments.
# ------------------------------------------------------------------------------
parser = argparse.ArgumentParser()
parser.add_argument("--renderer_type", default=p["renderer_type"])
parser.add_argument(
    "--result_filenames",
    default=",".join(p["result_filenames"]),
    help="Comma-separated names of files with results.",
)
parser.add_argument("--results_path", default=p["results_path"])
parser.add_argument("--eval_path", default=p["eval_path"])
parser.add_argument("--targets_filename", default=p["targets_filename"])
parser.add_argument("--num_workers", default=p["num_workers"])
parser.add_argument("--use_gpu", action="store_true", default=p["use_gpu"])
args = parser.parse_args()

p["renderer_type"] = str(args.renderer_type)
p["result_filenames"] = args.result_filenames.split(",")
p["results_path"] = str(args.results_path)
p["eval_path"] = str(args.eval_path)
p["targets_filename"] = str(args.targets_filename)
p["num_workers"] = int(args.num_workers)
p["use_gpu"] = int(args.use_gpu)

eval_time_start = time.time()
# Evaluation.
# ------------------------------------------------------------------------------
for result_filename in p["result_filenames"]:
    logger.info("===========")
    logger.info("EVALUATING: {}".format(result_filename))
    logger.info("===========")

    time_start = time.time()

    # Volume under recall surface (VSD) / area under recall curve (MSSD, MSPD).
    mAP = {}

    # Name of the result and the dataset.
    result_name = os.path.splitext(os.path.basename(result_filename))[0]
    dataset = str(result_name.split("_")[1].split("-")[0])

    # Calculate the average estimation time per image.
    ests = inout.load_bop_results(
        os.path.join(p["results_path"], result_filename), version="bop19"
    )
    times = {}
    times_available = True
    for est in ests:
        result_key = "{:06d}_{:06d}".format(est["scene_id"], est["im_id"])
        if est["time"] < 0:
            # All estimation times must be provided.
            times_available = False
            break
        elif result_key in times:
            if abs(times[result_key] - est["time"]) > 0.001:
                raise ValueError(
                    "The running time for scene {} and image {} is not the same for "
                    "all estimates.".format(est["scene_id"], est["im_id"])
                )
        else:
            times[result_key] = est["time"]

    if times_available:
        average_time_per_image = np.mean(list(times.values()))
    else:
        average_time_per_image = -1.0

    # Loop 1: Over the error types (mssd, mspd)
    mAP_per_error_type = {}
    # Evaluate the pose estimates.
    for error in p["errors"]:
        # Calculate error of the pose estimates.
        calc_error_script = misc.get_eval_calc_errors_script_name(p["use_gpu"], error["type"], dataset)
        calc_errors_cmd = [
            "python",
            os.path.join(
                os.path.dirname(os.path.realpath(__file__)),
                calc_error_script,
            ),
            "--n_top={}".format(error["n_top"]),
            "--visib_gt_min={}".format(p["visib_gt_min"]),
            "--eval_mode=detection",
            "--error_type={}".format(error["type"]),
            "--result_filenames={}".format(result_filename),
            "--renderer_type={}".format(p["renderer_type"]),
            "--results_path={}".format(p["results_path"]),
            "--eval_path={}".format(p["eval_path"]),
            "--targets_filename={}".format(p["targets_filename"]),
            "--max_sym_disc_step={}".format(p["max_sym_disc_step"]),
            "--skip_missing=1",
            "--num_workers={}".format(p["num_workers"]),
        ]

        logger.info("Running: " + " ".join(calc_errors_cmd))
        if subprocess.call(calc_errors_cmd) != 0:
            raise RuntimeError("Calculation of pose errors failed.")

        # Paths (rel. to p['eval_path']) to folders with calculated pose errors.
        error_dir_paths = {}
        error_sign = misc.get_error_signature(error["type"], error["n_top"])
        error_dir_paths[error_sign] = os.path.join(result_name, error_sign)

        # Recall scores for all settings of the threshold of correctness (and also
        # of the misalignment tolerance tau in the case of VSD).

        calc_scores_cmds = []
        # Calculate performance scores.
        for error_sign, error_dir_path in error_dir_paths.items():
            for correct_th in error["correct_th"]:
                calc_scores_cmd = [
                    "python",
                    os.path.join(
                        os.path.dirname(os.path.realpath(__file__)),
                        "eval_calc_scores.py",
                    ),
                    "--error_dir_paths={}".format(error_dir_path),
                    "--eval_path={}".format(p["eval_path"]),
                    "--targets_filename={}".format(p["targets_filename"]),
                    "--visib_gt_min={}".format(p["visib_gt_min"]),
                    "--eval_mode=detection",
                ]
                if p["ignore_object_visible_less_than_visib_gt_min"]:
                    calc_scores_cmd += [
                        "--ignore_object_visible_less_than_visib_gt_min"
                    ]

                calc_scores_cmd += [
                    "--correct_th_{}={}".format(
                        error["type"], ",".join(map(str, correct_th))
                    )
                ]
                calc_scores_cmds.append(calc_scores_cmd)

        if p["num_workers"] == 1:
            for calc_scores_cmd in calc_scores_cmds:
                logger.info("Running: " + " ".join(calc_scores_cmd))
                if subprocess.call(calc_scores_cmd) != 0:
                    raise RuntimeError("Calculation of performance scores failed.")
        else:
            with multiprocessing.Pool(p["num_workers"]) as pool:
                pool.map_async(misc.run_command, calc_scores_cmds)
                pool.close()
                pool.join()

        # After calculating the scores, load them and calculate the average precision and recall.
        # There are three loops to calculate mAP of 6D detection:
        # Loop 1: Iterate over the error types (mssd, mspd)
        # Loop 2: Iterate over the object IDs  (~ averaging over classes in COCO)
        # Loop 3: Iterate over the each threshold of correctness (~ IoU in COCO)
        # For a given (error type, object ID, threshold of correctness), compute mAP with recall range (0, 1, 0.01)
        # COCO reference for loop 4: https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocotools/cocoeval.py#L507
        num_object_ids_ignored = []
        for error_sign, error_dir_path in error_dir_paths.items():
            mAP_scores_per_object = {}
            # Loop 3: Over the each threshold of correctness
            for correct_th in error["correct_th"]:
                # Path to file with calculated scores.
                score_sign = misc.get_score_signature(correct_th, p["visib_gt_min"])

                scores_filename = "scores_{}.json".format(score_sign)
                scores_path = os.path.join(
                    p["eval_path"], result_name, error_sign, scores_filename
                )

                # Load the scores and number of instances.
                logger.info("Loading calculated scores from: {}".format(scores_path))
                scores = inout.load_json(scores_path)["scores"]
                num_instances_per_object = inout.load_json(scores_path)[
                    "num_instances_per_object"
                ]

                for obj_id in scores:
                    if num_instances_per_object[obj_id] > 0:
                        mAP_scores_per_object.setdefault(obj_id, []).append(scores[obj_id])
                    else:
                        num_object_ids_ignored.append(obj_id)
                        logger.warning(
                            f"Object {obj_id} not found in the dataset. Skipping object {obj_id} in mAP calculation."
                        )
            # Loop 2: Over the object IDs
            if len(num_object_ids_ignored) == 0:
                logger.info("Considering all objects for mAP calculation.")
            else:
                logger.info(
                    f"WARNING: Ignoring {len(num_object_ids_ignored)} objects: {num_object_ids_ignored} in mAp calculation!!!"
                )
            mAP_over_correct_ths = []
            for obj_id in mAP_scores_per_object:
                # make sure that the object is not ignored
                assert obj_id not in num_object_ids_ignored

                mAP_over_correct_th = np.mean(mAP_scores_per_object[obj_id])
                logger.info(
                    f"mAP, {error['type']}, {obj_id}: {mAP_over_correct_th:.3f}"
                )
                mAP_over_correct_ths.append(mAP_over_correct_th)
            mAP_per_error_type[error["type"]] = np.mean(mAP_over_correct_ths)
            logger.info(
                f"{error['type']}, Final mAP: {mAP_per_error_type[error['type']]:.3f}"
            )

    time_total = time.time() - time_start
    logger.info("Evaluation of {} took {}s.".format(result_filename, time_total))

    # Calculate the final scores.
    final_scores = {}
    for error in p["errors"]:
        final_scores["bop24_mAP_{}".format(error["type"])] = mAP_per_error_type[
            error["type"]
        ]

    # Final score for the given dataset.
    final_scores["bop24_mAP"] = np.mean(
        [mAP_per_error_type["mssd"], mAP_per_error_type["mspd"]]
    )

    # Average estimation time per image.
    final_scores["bop24_average_time_per_image"] = average_time_per_image

    # Save the final scores.
    final_scores_path = os.path.join(p["eval_path"], result_name, "scores_bop24.json")
    inout.save_json(final_scores_path, final_scores)

    # Print the final scores.
    logger.info("FINAL SCORES:")
    for score_name, score_value in final_scores.items():
        logger.info("- {}: {}".format(score_name, score_value))

total_eval_time = time.time() - eval_time_start
logger.info("Evaluation took {}s.".format(total_eval_time))
logger.info("Done.")