#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os
import time
from typing import Any, ClassVar, Dict, List, Tuple, Union

import torch
from numpy import ndarray
from torch import Tensor

from habitat import Config, logger
from habitat.core.env import Env, RLEnv
from habitat.core.vector_env import VectorEnv
from habitat_baselines.common.tensorboard_utils import (
    TensorboardWriter,
    get_writer,
)
from habitat_baselines.rl.ddppo.ddp_utils import SAVE_STATE, is_slurm_batch_job
from habitat_baselines.utils.common import (
    get_checkpoint_id,
    poll_checkpoint_folder,
)


class BaseTrainer:
    r"""Generic trainer class that serves as a base template for more
    specific trainer classes like RL trainer, SLAM or imitation learner.
    Includes only the most basic functionality.
    """

    supported_tasks: ClassVar[List[str]]

    def train(self) -> None:
        raise NotImplementedError

    def _setup_eval_config(self, checkpoint_config: Config) -> Config:
        r"""Sets up and returns a merged config for evaluation. Config
            object saved from checkpoint is merged into config file specified
            at evaluation time with the following overwrite priority:
                  eval_opts > ckpt_opts > eval_cfg > ckpt_cfg
            If the saved config is outdated, only the eval config is returned.

        Args:
            checkpoint_config: saved config from checkpoint.

        Returns:
            Config: merged config for eval.
        """

        config = self.config.clone()

        ckpt_cmd_opts = checkpoint_config.CMD_TRAILING_OPTS
        eval_cmd_opts = config.CMD_TRAILING_OPTS

        try:
            config.merge_from_other_cfg(checkpoint_config)
            config.merge_from_other_cfg(self.config)
            config.merge_from_list(ckpt_cmd_opts)
            config.merge_from_list(eval_cmd_opts)
        except KeyError:
            logger.info("Saved config is outdated, using solely eval config")
            config = self.config.clone()
            config.merge_from_list(eval_cmd_opts)
        config.defrost()
        if config.TASK_CONFIG.DATASET.SPLIT == "train":
            config.TASK_CONFIG.DATASET.SPLIT = "val"
        config.TASK_CONFIG.SIMULATOR.AGENT_0.SENSORS = self.config.SENSORS
        config.freeze()

        return config

    def eval(self) -> None:
        r"""Main method of trainer evaluation. Calls _eval_checkpoint() that
        is specified in Trainer class that inherits from BaseRLTrainer
        or BaseILTrainer

        Returns:
            None
        """
        self.device = (
            torch.device("cuda", self.config.TORCH_GPU_ID)
            if torch.cuda.is_available()
            else torch.device("cpu")
        )

        if "tensorboard" in self.config.VIDEO_OPTION:
            assert (
                len(self.config.TENSORBOARD_DIR) > 0
            ), "Must specify a tensorboard directory for video display"
            os.makedirs(self.config.TENSORBOARD_DIR, exist_ok=True)
        if "disk" in self.config.VIDEO_OPTION:
            assert (
                len(self.config.VIDEO_DIR) > 0
            ), "Must specify a directory for storing videos on disk"

        with get_writer(self.config, flush_secs=self.flush_secs) as writer:
            episode_success_histogram = {}
            episode_success_history = []
            if os.path.isfile(self.config.EVAL_CKPT_PATH_DIR):
                # evaluate singe checkpoint
                proposed_index = get_checkpoint_id(
                    self.config.EVAL_CKPT_PATH_DIR
                )
                if proposed_index is not None:
                    ckpt_idx = proposed_index
                else:
                    ckpt_idx = 0
                self._eval_checkpoint(
                    self.config.EVAL_CKPT_PATH_DIR,
                    writer,
                    checkpoint_index=ckpt_idx,
                )
            else:
                # evaluate multiple checkpoints in order
                log_file_dir = (
                    self.config.EVAL_CKPT_PATH_DIR + "/history_of_success"
                )
                if not os.path.exists(log_file_dir):
                    os.mkdir(
                        self.config.EVAL_CKPT_PATH_DIR + "/history_of_success"
                    )
                prev_ckpt_ind = -1
                over = False
                while not over:
                    current_ckpt = None
                    if current_ckpt is None:
                        current_ckpt, ckpt_ind = poll_checkpoint_folder(
                            self.config.EVAL_CKPT_PATH_DIR, prev_ckpt_ind
                        )
                        time.sleep(2)  # sleep for 2 secs before polling again
                    if current_ckpt is None:
                        over = True
                        print("No checkpoint found. Exiting...")
                        break
                    logger.info(f"=======current_ckpt: {current_ckpt}=======")
                    prev_ckpt_ind = ckpt_ind
                    success_counter, aggregated_stats = self._eval_checkpoint(
                        checkpoint_path=current_ckpt,
                        writer=writer,
                        checkpoint_index=prev_ckpt_ind,
                    )
                    for k, v in success_counter.items():
                        if k not in episode_success_histogram:
                            episode_success_histogram[k] = (0, 0)
                        episode_success_histogram[k] = (
                            episode_success_histogram[k][0] + v,
                            episode_success_histogram[k][1] + 1,
                        )
                    episode_success_history += [
                        (
                            prev_ckpt_ind,
                            sum(v for v in success_counter.values())
                            / len(success_counter),
                        )
                    ]

                    history_string = "=====================================================\n"
                    history_string += f"Episode success historgram at ckpt {prev_ckpt_ind}: \n"
                    for k, v in episode_success_histogram.items():
                        history_string += f"{k} : {v[0]} / {v[1]} \n"
                    history_string += "Episode success history: \n"
                    for v in episode_success_history:
                        history_string += f"ckpt : {v[0]} : {v[1]*100}%\n"
                    with open(
                        self.config.EVAL_CKPT_PATH_DIR
                        + f"/history_of_success/{prev_ckpt_ind}.log",
                        "w",
                    ) as f:
                        f.write(
                            f"ckpt : {current_ckpt} : {sum(v for v in success_counter.values()) / len(success_counter)*100}%\n"
                        )
                        for k, v in aggregated_stats.items():
                            f.write(f"{k} : {v}\n")
                        f.close()

                    print(history_string)
                    with open(
                        self.config.EVAL_CKPT_PATH_DIR
                        + "/history_of_success.log",
                        "w",
                    ) as f:
                        f.write(history_string)
                        f.close()
                    # print(
                    #     "====================================================="
                    # )
                    # print(
                    #     f"Episode success historgram at ckpt {prev_ckpt_ind}: "
                    # )
                    # print(episode_success_histogram)
                    # for k, v in episode_success_histogram.items():
                    #     print(k, " : ", v[0], " / ", v[1])
                    # print(
                    #     f"Episode success history: "
                    # )
                    # for v in episode_success_history:
                    #     print(f"ckpt : {v[0]} : {v[1]*100}%")
                    # print(
                    #     "====================================================="
                    # )

    def _eval_checkpoint(
        self,
        checkpoint_path: str,
        writer: TensorboardWriter,
        checkpoint_index: int = 0,
    ) -> None:
        raise NotImplementedError

    def save_checkpoint(self, file_name) -> None:
        raise NotImplementedError

    def load_checkpoint(self, checkpoint_path, *args, **kwargs) -> Dict:
        raise NotImplementedError


class BaseRLTrainer(BaseTrainer):
    r"""Base trainer class for RL trainers. Future RL-specific
    methods should be hosted here.
    """
    device: torch.device  # type: ignore
    config: Config
    video_option: List[str]
    num_updates_done: int
    num_steps_done: int
    _flush_secs: int
    _last_checkpoint_percent: float

    def __init__(self, config: Config) -> None:
        super().__init__()
        assert config is not None, "needs config file to initialize trainer"
        self.config = config
        self._flush_secs = 30
        self.num_updates_done = 0
        self.num_steps_done = 0
        self._last_checkpoint_percent = -1.0

        if config.NUM_UPDATES != -1 and config.TOTAL_NUM_STEPS != -1:
            raise RuntimeError(
                "NUM_UPDATES and TOTAL_NUM_STEPS are both specified.  One must be -1.\n"
                " NUM_UPDATES: {} TOTAL_NUM_STEPS: {}".format(
                    config.NUM_UPDATES, config.TOTAL_NUM_STEPS
                )
            )

        if config.NUM_UPDATES == -1 and config.TOTAL_NUM_STEPS == -1:
            raise RuntimeError(
                "One of NUM_UPDATES and TOTAL_NUM_STEPS must be specified.\n"
                " NUM_UPDATES: {} TOTAL_NUM_STEPS: {}".format(
                    config.NUM_UPDATES, config.TOTAL_NUM_STEPS
                )
            )

        if config.NUM_CHECKPOINTS != -1 and config.CHECKPOINT_INTERVAL != -1:
            raise RuntimeError(
                "NUM_CHECKPOINTS and CHECKPOINT_INTERVAL are both specified."
                "  One must be -1.\n"
                " NUM_CHECKPOINTS: {} CHECKPOINT_INTERVAL: {}".format(
                    config.NUM_CHECKPOINTS, config.CHECKPOINT_INTERVAL
                )
            )

        if config.NUM_CHECKPOINTS == -1 and config.CHECKPOINT_INTERVAL == -1:
            raise RuntimeError(
                "One of NUM_CHECKPOINTS and CHECKPOINT_INTERVAL must be specified"
                " NUM_CHECKPOINTS: {} CHECKPOINT_INTERVAL: {}".format(
                    config.NUM_CHECKPOINTS, config.CHECKPOINT_INTERVAL
                )
            )

    def percent_done(self) -> float:
        if self.config.NUM_UPDATES != -1:
            return self.num_updates_done / self.config.NUM_UPDATES
        else:
            return self.num_steps_done / self.config.TOTAL_NUM_STEPS

    def is_done(self) -> bool:
        return self.percent_done() >= 1.0

    def should_checkpoint(self) -> bool:
        needs_checkpoint = False
        if self.config.NUM_CHECKPOINTS != -1:
            if self.config.NUM_CHECKPOINTS == 0:
                return False
            checkpoint_every = 1 / self.config.NUM_CHECKPOINTS
            if (
                self._last_checkpoint_percent + checkpoint_every
                < self.percent_done()
            ):
                needs_checkpoint = True
                self._last_checkpoint_percent = self.percent_done()
        else:
            needs_checkpoint = (
                self.num_updates_done % self.config.CHECKPOINT_INTERVAL
            ) == 0

        return needs_checkpoint

    def _should_save_resume_state(self) -> bool:
        return SAVE_STATE.is_set() or (
            (
                not self.config.RL.preemption.save_state_batch_only
                or is_slurm_batch_job()
            )
            and (
                (
                    int(self.num_updates_done + 1)
                    % self.config.RL.preemption.save_resume_state_interval
                )
                == 0
            )
        )

    @property
    def flush_secs(self):
        return self._flush_secs

    @flush_secs.setter
    def flush_secs(self, value: int):
        self._flush_secs = value

    def train(self) -> None:
        raise NotImplementedError

    def _eval_checkpoint(
        self,
        checkpoint_path: str,
        writer: TensorboardWriter,
        checkpoint_index: int = 0,
    ) -> None:
        r"""Evaluates a single checkpoint. Trainer algorithms should
        implement this.

        Args:
            checkpoint_path: path of checkpoint
            writer: tensorboard writer object for logging to tensorboard
            checkpoint_index: index of cur checkpoint for logging

        Returns:
            None
        """
        raise NotImplementedError

    def save_checkpoint(self, file_name) -> None:
        raise NotImplementedError

    def load_checkpoint(self, checkpoint_path, *args, **kwargs) -> Dict:
        raise NotImplementedError

    @staticmethod
    def _pause_envs(
        envs_to_pause: List[int],
        envs: Union[VectorEnv, RLEnv, Env],
        test_recurrent_hidden_states: Tensor,
        not_done_masks: Tensor,
        current_episode_reward: Tensor,
        prev_actions: Tensor,
        batch: Dict[str, Tensor],
        rgb_frames: Union[List[List[Any]], List[List[ndarray]]],
    ) -> Tuple[
        Union[VectorEnv, RLEnv, Env],
        Tensor,
        Tensor,
        Tensor,
        Tensor,
        Dict[str, Tensor],
        List[List[Any]],
    ]:
        # pausing self.envs with no new episode
        if len(envs_to_pause) > 0:
            state_index = list(range(envs.num_envs))
            for idx in reversed(envs_to_pause):
                state_index.pop(idx)
                envs.pause_at(idx)

            # indexing along the batch dimensions
            test_recurrent_hidden_states = test_recurrent_hidden_states[
                state_index
            ]
            not_done_masks = not_done_masks[state_index]
            current_episode_reward = current_episode_reward[state_index]
            prev_actions = prev_actions[state_index]

            for k, v in batch.items():
                batch[k] = v[state_index]

            rgb_frames = [rgb_frames[i] for i in state_index]

        return (
            envs,
            test_recurrent_hidden_states,
            not_done_masks,
            current_episode_reward,
            prev_actions,
            batch,
            rgb_frames,
        )
