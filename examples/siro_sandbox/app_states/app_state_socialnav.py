from typing import List, Set

import magnum as mn
from app_states.app_state_abc import AppState
from camera_helper import CameraHelper
from controllers.gui_controller import GuiHumanoidController
from gui_navigation_helper import GuiNavigationHelper
from hablab_utils import get_agent_art_obj_transform
from sandbox_service import SandboxService

from habitat.gui.gui_input import GuiInput
from habitat.gui.text_drawer import TextOnScreenAlignment


class AppStateSocialNav(AppState):
    def __init__(
        self,
        sandbox_service: SandboxService,
        gui_agent_ctrl: GuiHumanoidController,
    ) -> None:
        self._sandbox_service: SandboxService = sandbox_service
        self._gui_agent_ctrl: GuiHumanoidController = gui_agent_ctrl

        self._cam_transform = None
        self._camera_helper = CameraHelper(
            self._sandbox_service.args, self._sandbox_service.gui_input
        )
        self._nav_helper = GuiNavigationHelper(
            self._sandbox_service, self.get_gui_controlled_agent_index()
        )

        # task-specific parameters:
        self._num_iter_episodes: int = len(self._sandbox_service.env.episode_iterator.episodes)  # type: ignore
        self._num_episodes_done: int = 0
        self._object_found_radius: float = 1.2
        self._episode_found_obj_ids: Set = (
            None  # will be set in on_environment_reset
        )
        self._episode_target_obj_ids: List[
            str
        ] = None  # will be set in on_environment_reset

    def on_environment_reset(self, episode_recorder_dict):
        self._episode_found_obj_ids = set()

        sim = self.get_sim()
        obj_ids, _ = sim.get_targets()
        self._episode_target_obj_ids = [
            sim._scene_obj_ids[obj_id] for obj_id in obj_ids
        ]

        self._nav_helper.on_environment_reset()
        self._camera_helper.update(self._get_camera_lookat_pos(), dt=0)

        if episode_recorder_dict:
            episode_recorder_dict[
                "target_obj_ids"
            ] = self._episode_target_obj_ids

        self._num_episodes_done += 1

    def sim_update(self, dt, post_sim_update_dict):
        if self._sandbox_service.gui_input.get_key_down(GuiInput.KeyNS.ESC):
            self._sandbox_service.end_episode()
            post_sim_update_dict["application_exit"] = True

        if self._sandbox_service.gui_input.get_key_down(GuiInput.KeyNS.M):
            self._sandbox_service.end_episode(do_reset=True)

        if self._env_episode_active():
            self._update_task()
            self._set_act_hints()
            self._sandbox_service.compute_action_and_step_env()

        self._camera_helper.update(self._get_camera_lookat_pos(), dt)

        self.cam_transform = self._camera_helper.get_cam_transform()
        post_sim_update_dict["cam_transform"] = self.cam_transform

        self._update_help_text()

    def record_state(self):
        pass

    def get_sim(self):
        return self._sandbox_service.sim

    def get_gui_controlled_agent_index(self):
        return self._gui_agent_ctrl._agent_idx

    def _env_episode_active(self) -> bool:
        return not (
            self._sandbox_service.env.episode_over or self._env_task_complete()
        )

    def _env_task_complete(self) -> bool:
        return len(self._episode_target_obj_ids) == len(
            self._episode_found_obj_ids
        )

    def _next_episode_exists(self):
        return self._num_episodes_done < self._num_iter_episodes - 1

    def _get_camera_lookat_pos(self):
        agent_root = get_agent_art_obj_transform(
            self.get_sim(), self.get_gui_controlled_agent_index()
        )
        lookat = agent_root.translation + mn.Vector3(0, 1, 0)
        return lookat

    def _update_task(self):
        end_radius = 0.3
        self._num_remaining_objects = 0

        # draw nav_hint and found object area
        for obj_id in self._episode_target_obj_ids:
            if obj_id in self._episode_found_obj_ids:
                continue

            self._num_remaining_objects += 1

            this_target_pos = self._get_target_object_position(obj_id)

            self._nav_helper._draw_nav_hint_from_agent(
                self._camera_helper.get_xz_forward(),
                mn.Vector3(this_target_pos),
                end_radius,
                mn.Color3(255 / 255, 128 / 255, 0),  # orange
            )

            # draw found object area
            can_grasp_position = mn.Vector3(this_target_pos)
            can_grasp_position[1] = self._get_agent_feet_height()
            self._sandbox_service.line_render.draw_circle(
                can_grasp_position,
                self._object_found_radius,
                mn.Color3(255 / 255, 255 / 255, 0),  # yellow
                24,
            )

    def _set_act_hints(self):
        translation = self._get_agent_translation()

        min_dist = self._object_found_radius
        closest_object_id = None
        for obj_id in self._episode_target_obj_ids:
            if obj_id in self._episode_found_obj_ids:
                continue

            this_target_pos = self._get_target_object_position(obj_id)
            # compute distance in xz plane
            offset = this_target_pos - translation
            offset.y = 0
            dist_xz = offset.length()
            if dist_xz < min_dist:
                min_dist = dist_xz
                closest_object_id = obj_id

        if closest_object_id is not None:
            self._episode_found_obj_ids.add(closest_object_id)

        walk_dir = None
        if not self._camera_helper._first_person_mode:
            candidate_walk_dir = (
                self._nav_helper.viz_and_get_humanoid_walk_dir()
            )
            if self._sandbox_service.gui_input.get_mouse_button(
                GuiInput.MouseNS.RIGHT
            ):
                walk_dir = candidate_walk_dir

        self._gui_agent_ctrl.set_act_hints(
            walk_dir,
            None,
            None,
            self._camera_helper.lookat_offset_yaw,
        )

    def _get_agent_translation(self):
        assert isinstance(self._gui_agent_ctrl, GuiHumanoidController)
        return (
            self._gui_agent_ctrl._humanoid_controller.obj_transform_base.translation
        )

    def _get_agent_feet_height(self):
        assert isinstance(self._gui_agent_ctrl, GuiHumanoidController)
        base_offset = (
            self._gui_agent_ctrl.get_articulated_agent().params.base_offset
        )
        agent_feet_translation = self._get_agent_translation() + base_offset
        return agent_feet_translation[1]

    def _get_target_object_position(self, target_obj_id):
        sim = self.get_sim()
        rom = sim.get_rigid_object_manager()
        return rom.get_object_by_id(target_obj_id).translation

    def _update_help_text(self):
        controls_str = self._get_controls_text()
        if len(controls_str) > 0:
            self._sandbox_service.text_drawer.add_text(
                controls_str, TextOnScreenAlignment.TOP_LEFT
            )

        status_str = self._get_status_text()
        if len(status_str) > 0:
            self._sandbox_service.text_drawer.add_text(
                status_str,
                TextOnScreenAlignment.TOP_CENTER,
                text_delta_x=-280,
                text_delta_y=-50,
            )

        progress_str = f"{self._num_iter_episodes - (self._num_episodes_done + 1)} episodes remaining"
        self._sandbox_service.text_drawer.add_text(
            progress_str,
            TextOnScreenAlignment.TOP_RIGHT,
            text_delta_x=370,
        )

    def _get_controls_text(self):
        found_object_controls_text = "Spacebar: mark object as found\n"

        controls_str: str = ""
        controls_str += "ESC: exit\n"
        if self._next_episode_exists():
            controls_str += "M: next episode\n"

        if self._env_episode_active():
            if self._camera_helper._first_person_mode:
                # controls_str += "Left-click: toggle cursor\n"  # make this "unofficial" for now
                controls_str += "I, K: look up, down\n"
                controls_str += "A, D: turn\n"
                controls_str += "W, S: walk\n"
                controls_str += found_object_controls_text
            # third-person mode
            else:
                controls_str += "R + drag: rotate camera\n"
                controls_str += "Right-click: walk\n"
                controls_str += "A, D: turn\n"
                controls_str += "W, S: walk\n"
                controls_str += "Scroll: zoom\n"
                controls_str += found_object_controls_text

        return controls_str

    def _get_status_text(self):
        assert self._num_remaining_objects is not None

        status_str = ""
        if not self._env_episode_active():
            if self._env_task_complete():
                status_str += "Task complete!\n"
            else:
                status_str += "Oops! Something went wrong.\n"
        elif self._num_remaining_objects > 0:
            status_str += "Find the remaining {} object{}!".format(
                self._num_remaining_objects,
                "s" if self._num_remaining_objects > 1 else "",
            )
        else:
            # we don't expect to hit this case ever
            status_str += "Oops! Something went wrong.\n"

        # center align the status_str
        max_status_str_len = 50
        status_str = "/n".join(
            line.center(max_status_str_len) for line in status_str.split("/n")
        )

        return status_str
