#!/usr/bin/env python3

# Copyright (c) Meta Platforms, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os
import shutil

import numpy as np

import habitat
from habitat.core.utils import try_cv2_import
from habitat.tasks.nav.shortest_path_follower import ShortestPathFollower
from habitat.utils.visualizations import maps
from habitat.utils.visualizations.utils import images_to_video

cv2 = try_cv2_import()

IMAGE_DIR = os.path.join("examples", "images")
if not os.path.exists(IMAGE_DIR):
    os.makedirs(IMAGE_DIR)


class SimpleRLEnv(habitat.RLEnv):
    def get_reward_range(self):
        return [-1, 1]

    def get_reward(self, observations):
        return 0

    def get_done(self, observations):
        return self.habitat_env.episode_over

    def get_info(self, observations):
        return self.habitat_env.get_metrics()


def draw_top_down_map(info, output_size):
    return maps.colorize_draw_agent_and_fit_to_height(
        info["top_down_map"], output_size
    )


def shortest_path_example():
    config = habitat.get_config(
        # config_path="benchmark/nav/pointnav/pointnav_habitat_test.yaml",
        config_path="benchmark/nav/objectnav/objectnav_hm3d.yaml",
        overrides=[
            "+habitat/task/measurements@habitat.task.measurements.top_down_map=top_down_map",
            "habitat.task.measurements.top_down_map.draw_goal_aabbs=False",
            "habitat.simulator.action_space_config=velocitycontrol",
            # "habitat/task/actions=velocity_control",
            # "habitat/task/actions=[move_forward, turn_left, turn_right, stop]"
            "habitat/task/actions=[move_forward_waypoint, turn_left_waypoint, turn_right_waypoint, stop]",
        ],
    )

    with SimpleRLEnv(config=config) as env:
        goal_radius = env.episodes[0].goals[0].radius
        if goal_radius is None:
            goal_radius = config.habitat.simulator.forward_step_size
        follower = ShortestPathFollower(
            env.habitat_env.sim, goal_radius, False
        )

        print("Environment creation successful")
        for episode in range(1):
            env.reset()
            dirname = os.path.join(
                IMAGE_DIR, "shortest_path_example", "%02d" % episode
            )
            if os.path.exists(dirname):
                shutil.rmtree(dirname)
            os.makedirs(dirname)
            print("Agent stepping around inside environment.")
            images = []
            while not env.habitat_env.episode_over:
                if len(images) > 30:
                    action = {'action': 'stop', 'action_args': None}
                elif len(images) % 3 == 0:
                    action = {'action': 'move_forward_waypoint', 'action_args': None}
                elif len(images) % 3 == 1:
                    action = {'action': 'turn_left_waypoint', 'action_args': None}
                else:
                    action = {'action': 'turn_right_waypoint', 'action_args': None}

                print(f"action={action}")

                observations, reward, done, info = env.step(action)
                im = observations["rgb"]

                depth_im = observations["depth"] * 255
                depth_im = np.repeat(depth_im, 3, axis=2)

                top_down_map = draw_top_down_map(info, im.shape[0])

                output_im = np.concatenate(
                    (im, depth_im, top_down_map), axis=1
                )
                images.append(output_im)
            images_to_video(images, dirname, "trajectory")
            print("Episode finished")


def main():
    shortest_path_example()


if __name__ == "__main__":
    main()
