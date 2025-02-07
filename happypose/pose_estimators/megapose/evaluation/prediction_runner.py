"""
Copyright (c) 2022 Inria & NVIDIA CORPORATION & AFFILIATES. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""


# Standard Library
import time
from collections import defaultdict
from typing import Dict, Optional
from pathlib import Path


# Third Party
import numpy as np
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm

# MegaPose
import happypose.pose_estimators.megapose
import happypose.toolbox.utils.tensor_collection as tc
from happypose.pose_estimators.megapose.inference.pose_estimator import (
    PoseEstimator,
)
from happypose.pose_estimators.megapose.inference.types import (
    DetectionsType,
    InferenceConfig,
    ObservationTensor,
    PoseEstimatesType,
)
from happypose.pose_estimators.megapose.config import (
    BOP_DS_DIR
)
from happypose.pose_estimators.megapose.evaluation.bop import (
    get_sam_detections,
    load_sam_predictions
)

from happypose.pose_estimators.megapose.training.utils import CudaTimer
from happypose.toolbox.datasets.samplers import DistributedSceneSampler
from happypose.toolbox.datasets.scene_dataset import SceneDataset, SceneObservation, ObjectData
from happypose.toolbox.utils.distributed import get_rank, get_tmp_dir, get_world_size
from happypose.toolbox.utils.logging import get_logger


# Temporary
from happypose.toolbox.inference.utils import make_detections_from_object_data
import pandas as pd
import json

logger = get_logger(__name__)

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class PredictionRunner:
    def __init__(
        self,
        scene_ds: SceneDataset,
        inference_cfg: InferenceConfig,
        batch_size: int = 1,
        n_workers: int = 4,
    ) -> None:

        self.inference_cfg = inference_cfg
        self.rank = get_rank()
        self.world_size = get_world_size()
        self.tmp_dir = get_tmp_dir()

        sampler = DistributedSceneSampler(scene_ds, num_replicas=self.world_size, rank=self.rank)
        self.sampler = sampler
        self.scene_ds = scene_ds
        dataloader = DataLoader(
            scene_ds,
            batch_size=batch_size,
            num_workers=n_workers,
            sampler=sampler,
            collate_fn=SceneObservation.collate_fn,
        )

        self.batch_size = batch_size
        self.load_depth = scene_ds.load_depth
        self.dataloader = dataloader

    def run_inference_pipeline(
        self,
        pose_estimator: PoseEstimator,
        obs_tensor: ObservationTensor,
        gt_detections: DetectionsType,
        sam_detections: DetectionsType,
        initial_estimates: Optional[PoseEstimatesType] = None,
    ) -> Dict[str, PoseEstimatesType]:
        """Runs inference pipeline, extracts the results.

        Returns: A dict with keys
            - 'final': final preds
            - 'refiner/final': preds at final refiner iteration (before depth refinement)
            - 'depth_refinement': preds after depth refinement.


        """
        # TODO: this check could be done outside of run_inference_pipeline
        # and then only check if detections are None
        if self.inference_cfg.detection_type == "gt":
            detections = gt_detections
            run_detector = False
        elif self.inference_cfg.detection_type == "sam":
            # print("sam_detections =", sam_detections.bboxes)
            detections = sam_detections
            run_detector = False
        elif self.inference_cfg.detection_type == "detector":
            detections = None
            run_detector = True

        else:
            raise ValueError(f"Unknown detection type {self.inference_cfg.detection_type}")

        coarse_estimates = None
        if self.inference_cfg.coarse_estimation_type == "external":
            # TODO (ylabbe): This is hacky, clean this for modelnet eval.
            coarse_estimates = initial_estimates
            coarse_estimates = happypose.toolbox.inference.utils.add_instance_id(coarse_estimates)
            coarse_estimates.infos["instance_id"] = 0
            run_detector = False

        t = time.time()
        preds, extra_data = pose_estimator.run_inference_pipeline(
            obs_tensor,
            detections=detections,
            run_detector=run_detector,
            coarse_estimates=coarse_estimates,
            n_refiner_iterations=self.inference_cfg.n_refiner_iterations,
            n_pose_hypotheses=self.inference_cfg.n_pose_hypotheses,
            run_depth_refiner=self.inference_cfg.run_depth_refiner,
            bsz_images=self.inference_cfg.bsz_images,
            bsz_objects=self.inference_cfg.bsz_objects,
        )
        elapsed = time.time() - t

        # TODO (lmanuelli): Process this into a dict with keys like
        # - 'refiner/iteration=1`
        # - 'refiner/iteration=5`
        # - `depth_refiner`
        # Note: Since we support multi-hypotheses we need to potentially
        # go back and extract out the 'refiner/iteration=1`, `refiner/iteration=5` things for the ones that were actually the highest scoring at the end.

        all_preds = dict()
        data_TCO_refiner = extra_data["refiner"]["preds"]

        all_preds = {
            "final": preds,
            f"refiner/iteration={self.inference_cfg.n_refiner_iterations}": data_TCO_refiner,
            "refiner/final": data_TCO_refiner,
            "coarse": extra_data["coarse"]["preds"],
        }

        if self.inference_cfg.run_depth_refiner:
            all_preds[f"depth_refiner"] = extra_data["depth_refiner"]["preds"]

        for k, v in all_preds.items():
            if "mask" in v.tensors:
                breakpoint()
                v.delete_tensor("mask")

        return all_preds

    def get_predictions(self, pose_estimator: PoseEstimator) -> Dict[str, PoseEstimatesType]:
        """Runs predictions

        Returns: A dict with keys
            - 'refiner/iteration=1`
            - 'refiner/iteration=5`
            - 'depth_refiner'

            With the predictions at the various settings/iterations.


        """

        predictions_list = defaultdict(list)

        ######
        # This section opens the detections stored in "baseline.json"
        # format it and store it in a dataframe that will be accessed later
        ######
        # Temporary solution
        if self.inference_cfg.detection_type == "sam":
            df_all_dets, df_targets = load_sam_predictions(self.scene_ds.ds_dir.name, self.scene_ds.ds_dir)

        for n, data in enumerate(tqdm(self.dataloader)):
            # data is a dict
            rgb = data["rgb"]
            depth = data["depth"]
            K = data["cameras"].K
            im_info = data['im_infos'][0]
            scene_id, view_id = im_info['scene_id'], im_info['view_id']
            # Dirty but avoids creating error when running with real detector
            dt_det = 0

            ######
            # Filter the dataframe according to scene id and view id
            # Transform the data in ObjectData and then Detections
            ######
            # Temporary solution
            if self.inference_cfg.detection_type == "sam":
                # We assume a unique image ("view") associated with a unique scene_id is 
                sam_detections = get_sam_detections(data=data, df_all_dets=df_all_dets, df_targets=df_targets, dt_det=dt_det)
            else:
                sam_detections = None
            gt_detections = data["gt_detections"].cuda()
            initial_data = None
            if data["initial_data"]:
                initial_data = data["initial_data"].cuda()

            obs_tensor = ObservationTensor.from_torch_batched(rgb, depth, K)
            obs_tensor = obs_tensor.cuda()

            # GPU warmup for timing
            if n == 0:
                with torch.no_grad():
                    self.run_inference_pipeline(
                        pose_estimator, obs_tensor, gt_detections, sam_detections, initial_estimates=initial_data
                    )

            cuda_timer = CudaTimer()
            cuda_timer.start()
            with torch.no_grad():
                all_preds = self.run_inference_pipeline(
                    pose_estimator, obs_tensor, gt_detections, sam_detections, initial_estimates=initial_data
                )
            cuda_timer.end()
            duration = cuda_timer.elapsed()

            total_duration = duration + dt_det

            # Add metadata to the predictions for later evaluation
            for k, v in all_preds.items():
                v.infos['time'] = total_duration
                v.infos['scene_id'] = scene_id
                v.infos['view_id'] = view_id
                predictions_list[k].append(v)

    # Concatenate the lists of PandasTensorCollections
        predictions = dict()
        for k, v in predictions_list.items():
            predictions[k] = tc.concatenate(v)

        return predictions