import logging
from pathlib import Path
import sys
import time
import typing

import hydra
import numpy as np
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import seed_everything
import torch

from calvin.evaluation.multistep_sequences import get_sequences
from calvin.evaluation.utils import get_eval_env_state, imshow_tensor, format_sftp_path, get_checkpoint
from calvin.models.play_lmp import PlayLMP

logger = logging.getLogger(__name__)


@hydra.main(config_path="../../conf/inference", config_name="config_inference")
def evaluate_policy_multistep(input_cfg: DictConfig) -> None:
    """
    Run inference on trained policy.
     Arguments:
        train_folder (str): path of trained model.
        load_checkpoint (str): optional model checkpoint. If not specified, the last checkpoint is taken by default.
        +datamodule.root_data_dir (str): /path/dataset when running inference on another machine than were it was trained
        visualize (bool): wether to visualize the policy rollouts (default True).
    """
    # when mounting remote folder with sftp, format path
    format_sftp_path(input_cfg)
    # load config used during training
    train_cfg_path = Path(input_cfg.train_folder) / ".hydra/config.yaml"
    train_cfg = OmegaConf.load(train_cfg_path)

    # merge configs to keep current cmd line overrides
    cfg = OmegaConf.merge(train_cfg, input_cfg)
    seed_everything(cfg.seed)

    device = torch.device("cuda:0")

    # since we don't use the trainer during inference, manually set up data_module
    data_module = hydra.utils.instantiate(cfg.datamodule, num_workers=0)
    data_module.prepare_data()
    data_module.setup()
    dataloader = data_module.val_dataloader()
    dataset = dataloader.dataset.datasets["lang"]
    env = hydra.utils.instantiate(cfg.callbacks.rollout.env_cfg, dataset, device, show_gui=False)

    task_checker = hydra.utils.instantiate(cfg.callbacks.rollout.tasks)
    checkpoint = get_checkpoint(cfg)
    logger.info("Loading model from checkpoint.")
    model = PlayLMP.load_from_checkpoint(checkpoint)
    model.freeze()
    if train_cfg.model.decoder.get("load_action_bounds", False):
        model.action_decoder._setup_action_bounds(cfg.datamodule.root_data_dir, None, None, True)
    model = model.cuda(device)
    logger.info("Successfully loaded model.")

    eval_sequences = get_sequences()
    task_embeddings = np.load(dataset.abs_datasets_dir / dataset.lang_folder / "embeddings.npy", allow_pickle=True).item()
    results = {}

    for eval_sequence in eval_sequences:
        result = evaluate_sequence(env, model, task_checker, eval_sequence, task_embeddings, cfg, device)
        print(f"{' '.join(eval_sequence)}: achieved {result} / {len(eval_sequence)} subtasks")
        results[eval_sequence] = result


def evaluate_sequence(env, model, task_checker, eval_sequence, embeddings, cfg, device):
    robot_obs, scene_obs = get_eval_env_state()
    env.reset(robot_obs=robot_obs, scene_obs=scene_obs)

    success_counter = 0
    for subtask in eval_sequence:
        success = rollout(env, model, task_checker, cfg, subtask, embeddings, device)
        if success:
            success_counter += 1
        else:
            return success_counter
    return success_counter


def rollout(env, model, task_checker, cfg, subtask, embeddings, device):
    obs = env.get_obs()
    current_img_obs = obs["rgb_obs"]
    current_depth_obs = obs["depth_obs"]
    current_state_obs = obs["state_obs"]
    goal_lang = torch.from_numpy(embeddings[subtask]["emb"]).to(device).squeeze(0)
    start_info = env.get_info()

    for step in range(cfg.ep_len):
        #  replan every replan_freq steps (default 30 i.e every second)
        if step % cfg.replan_freq == 0:
            plan, latent_goal = model.get_pp_plan_lang(
                current_img_obs, current_depth_obs, current_state_obs, goal_lang
            )  # type: ignore
        if cfg.visualize:
            imshow_tensor("current_img", current_img_obs[0], wait=1)

        # use plan to predict actions with current observations
        action = model.predict_with_plan(current_img_obs, current_depth_obs, current_state_obs, latent_goal, plan)
        obs, _, _, current_info = env.step(action)
        # check if current step solves a task
        current_task_info = task_checker.get_task_info_for_set(start_info, current_info, set(subtask))
        if len(current_task_info) > 0:
            return True
        # update current observation
        current_img_obs = obs["rgb_obs"]
        current_depth_obs = obs["depth_obs"]
        current_state_obs = obs["state_obs"]
    return False


if __name__ == "__main__":
    evaluate_policy_multistep()
