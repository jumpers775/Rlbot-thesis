import os
import numpy as np
import torch
from rlgym.api import RLGym
from rlgym.rocket_league.action_parsers import LookupTableAction, RepeatAction
from rlgym.rocket_league.done_conditions import GoalCondition, AnyCondition, TimeoutCondition, NoTouchTimeoutCondition
from rlgym.rocket_league.obs_builders import DefaultObs
from rlgym.rocket_league.reward_functions import CombinedReward, GoalReward, TouchReward
from rlgym.rocket_league.sim import RocketSimEngine
import RocketSim as rocketsim
from rlgym.rocket_league.rlviser import RLViserRenderer
from rlgym.rocket_league.state_mutators import MutatorSequence, FixedTeamSizeMutator, KickoffMutator
from rewards import BallProximityReward, BallToGoalDistanceReward, BallVelocityToGoalReward, TouchBallReward, TouchBallToGoalAccelerationReward, AlignBallToGoalReward, PlayerVelocityTowardBallReward, KRCReward
from models import BasicModel, SimBa, fix_compiled_state_dict, extract_model_dimensions, load_partial_state_dict
from observation import StackedActionsObs, ActionStacker
from training import PPOTrainer
import concurrent.futures
import time
import argparse
from tqdm import tqdm
import signal
import sys
import asyncio
import multiprocessing as mp
from multiprocessing import Process, Pipe
from typing import List, Tuple, Dict
import numpy as np
from envs.factory import get_env
from envs.vectorized import VectorizedEnv
from envs.rlbot_vectorized import RLBotVectorizedEnv
from curriculum import create_lucy_skg_curriculum

def run_training(
    actor,
    critic,
    device,
    num_envs: int,  # Required parameter first
    training_step_offset: int = 0,  # Optional parameter with default value after
    total_episodes: int = None,
    training_time: float = None,
    render: bool = False,
    update_interval: int = 1000,
    use_wandb: bool = False,
    debug: bool = False,
    use_compile: bool = True,
    use_amp: bool = False,
    save_interval: int = 200,
    output_path: str = None,
    use_curriculum: bool = False,
    # Hyperparameters
    lr_actor: float = 3e-4,
    lr_critic: float = 1e-3,
    gamma: float = 0.99,
    gae_lambda: float = 0.95,
    clip_epsilon: float = 0.2,
    critic_coef: float = 0.5,
    entropy_coef: float = 0.01,
    max_grad_norm: float = 0.5,
    ppo_epochs: int = 10,
    batch_size: int = 64,
    aux_amp: bool = False,
    aux_scale: float = 0.005,
    # Additional args
    auxiliary: bool = True,
    sr_weight: float = 1.0,
    rp_weight: float = 1.0,
    test: bool = False
):
    """
    Main training loop.  This sets up the agent, environment, and training process,
    then runs the training loop until either a specified number of episodes have
    been completed, or a specified amount of time has passed.
    """
    # Performance optimizations
    os.environ['OMP_NUM_THREADS'] = str(max(1, os.cpu_count() // 4))
    os.environ['KMP_BLOCKTIME'] = '0'
    os.environ['KMP_SETTINGS'] = '0'
    os.environ['KMP_AFFINITY'] = 'granularity=fine,compact,1,0'

    # We need at least one way to know when to stop training.
    if total_episodes is None and training_time is None:
        raise ValueError("Either total_episodes or training_time must be provided")

    actor.to(device)
    critic.to(device)

    # Initialize action stacker for keeping track of previous actions
    action_stacker = ActionStacker(stack_size=5, action_size=actor.action_shape)

    # Initialize the PPO trainer, which handles the core learning algorithm.
    trainer = PPOTrainer(
        actor,
        critic,
        action_dim=actor.action_shape,
        device=device,
        lr_actor=lr_actor,
        lr_critic=lr_critic,
        gamma=gamma,
        gae_lambda=gae_lambda,
        clip_epsilon=clip_epsilon,
        critic_coef=critic_coef,
        entropy_coef=entropy_coef,
        max_grad_norm=max_grad_norm,
        ppo_epochs=ppo_epochs,
        batch_size=batch_size,
        use_wandb=use_wandb,
        debug=debug,
        use_compile=use_compile,
        use_amp=use_amp,
        use_auxiliary_tasks=auxiliary,
        sr_weight=sr_weight * aux_scale,  # Apply scaling to auxiliary task weights
        rp_weight=rp_weight * aux_scale,
        aux_amp=aux_amp
    )

    #  Use train mode for training and eval for testing
    if test:
        trainer.actor.eval()
        trainer.critic.eval()
    else:
        trainer.actor.train()
        trainer.critic.train()

    # Initialize curriculum if enabled
    curriculum_manager = None
    if use_curriculum:
        curriculum_manager = create_lucy_skg_curriculum(debug=debug)
        # Bidirectional registration for proper wandb step synchronization
        curriculum_manager.register_trainer(trainer)
        trainer.register_curriculum_manager(curriculum_manager)
        
        if debug:
            print("[DEBUG] Curriculum learning enabled with basic curriculum")

        # Use a vectorized environment for parallel data collection.
        env_class = VectorizedEnv  # Default
        if curriculum_manager and curriculum_manager.requires_bots():
            env_class = RLBotVectorizedEnv

        vec_env = env_class(
            num_envs=num_envs,
            render=render,
            action_stacker=action_stacker,
            curriculum_manager=curriculum_manager,
            rlbotpack_path=os.path.join(os.path.dirname(__file__), "RLBotPack"),
            debug=debug
        )
    else:
        # Use a default vectorized environment without curriculum
        env_class = VectorizedEnv
        vec_env = env_class(
            num_envs=num_envs,
            render=render,
            action_stacker=action_stacker,
            debug=debug
        )

    # For time-based training, we'll need to know when we started.
    start_time = time.time()

    # Set up the progress bar. It'll track episodes or time, depending on how we're training.
    if training_time is not None:
        progress_bar = tqdm(
            total=int(training_time),
            desc="Time",
            bar_format='{desc}: {percentage:3.0f}% [{elapsed}<{remaining}] |{bar}| {postfix}',
            dynamic_ncols=True
        )
    else:
        progress_bar = tqdm(
            total=total_episodes,
            desc="Episodes",
            bar_format='{desc}: {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}] {percentage:3.0f}%|{bar}| {postfix}',
            dynamic_ncols=True
        )

    # This dictionary holds statistics we'll display in the progress bar.
    stats_dict = {
        "Device": device,
        "Envs": num_envs,
        "Exp": "0/0",  # Experiences collected / experiences per update
        "Episodes": 0,  # Total episodes completed
        "Reward": "0.0",  # Average reward per episode
        "PLoss": "0.0",  # Actor loss
        "VLoss": "0.0",  # Critic loss
        "Entropy": "0.0", # Entropy loss
        "SR_Loss": "0.0", # State reconstruction loss
        "RP_Loss": "0.0"  # Reward prediction loss
    }

    # Add curriculum info if enabled
    if curriculum_manager:
        curriculum_stats = curriculum_manager.get_curriculum_stats()
        stats_dict.update({
            "Stage": curriculum_stats["current_stage_name"],
            "Diff": f"{curriculum_stats['difficulty_level']:.2f}"
        })

    progress_bar.set_postfix(stats_dict)

    # Initialize variables to track training progress.
    collected_experiences = 0
    total_episodes_so_far = 0
    last_update_time = time.time()
    last_save_episode = 0
    
    # Add defensive initialization for episode_rewards
    episode_rewards = {}
    for i in range(num_envs):
        episode_rewards[i] = {}
        try:
            if vec_env.obs_dicts[i]:
                for agent_id in vec_env.obs_dicts[i]:
                    episode_rewards[i][agent_id] = 0
            else:
                if debug:
                    print(f"[DEBUG] Warning: Empty observations for env {i}")
        except Exception as e:
            if debug:
                print(f"[DEBUG] Error initializing rewards for env {i}: {e}")

    last_progress_update = start_time  # For updating time-based progress bar
    should_continue = True  # Initialize the control variable

    try:
        # Let's keep training until it's time to stop
        while should_continue:
            current_time = time.time()
            elapsed = current_time - start_time

            # Figure out if we should keep going based on time or episode count
            if training_time is not None:
                # Using a time-based training schedule - update progress bar once per second
                if current_time - last_progress_update >= 1.0:
                    progress_bar.n = min(int(elapsed), int(training_time))
                    progress_bar.refresh()
                    last_progress_update = current_time

                should_continue = elapsed < training_time
            else:
                # Using an episode-based schedule
                should_continue = total_episodes_so_far < total_episodes

            if not should_continue:
                break

            # Batch up observations from all environments for efficiency
            all_obs = []
            all_env_indices = []
            all_agent_ids = []
            # Organize observations into lists for batch processing.
            for env_idx, obs_dict in enumerate(vec_env.obs_dicts):
                for agent_id, obs in obs_dict.items():
                    all_obs.append(obs)
                    all_env_indices.append(env_idx)
                    all_agent_ids.append(agent_id)

            # Only proceed if we have observations. (It's possible all environments ended at the same time.)
            actions_dict_list = [{} for _ in range(num_envs)]  # Initialize outside the block to avoid unbound issues
            if len(all_obs) > 0:
                obs_batch = torch.FloatTensor(np.stack(all_obs)).to(device)
                with torch.no_grad():
                    # Get actions and values from the networks in a single forward pass
                    action_batch, log_prob_batch, value_batch, features_batch = trainer.get_action(obs_batch, return_features=True)

                # Organize actions into a list of dictionaries, one for each environment.
                for i, (action, log_prob, value) in enumerate(zip(action_batch, log_prob_batch, value_batch)):
                    env_idx = all_env_indices[i]
                    agent_id = all_agent_ids[i]
                    actions_dict_list[env_idx][agent_id] = action

                    # Store the experience (observations, actions, etc.).
                    # Reward and done are placeholders for now; we'll update them after the environment step.
                    trainer.store_experience(
                        all_obs[i],
                        action,
                        log_prob,
                        0,
                        value,
                        False
                    )

                    collected_experiences += 1

            # Step all environments forward in parallel - optimized implementation
            results, dones, episode_counts = vec_env.step(actions_dict_list)

            # Process the results from each environment.
            exp_idx = 0  # Index into our experience buffer.
            for env_idx, (next_obs_dict, reward_dict, terminated_dict, truncated_dict) in enumerate(results):
                for agent_id in reward_dict.keys():  # Use reward_dict to ensure we get the correct agent ID.
                    # Get the actual reward and done flag for this agent in this environment.
                    reward = reward_dict[agent_id]
                    done = terminated_dict[agent_id] or truncated_dict[agent_id]

                    # Reset action history if episode is done
                    if done:
                        action_stacker.reset_agent(agent_id)
                        trainer.reset_auxiliary_tasks()

                    # Update the stored experience with the correct reward and done flag.
                    mem_idx = trainer.memory.pos - len(all_obs) + exp_idx
                    if mem_idx < 0:  # Handle wrap-around in the circular buffer.
                        mem_idx += trainer.memory.buffer_size

                    trainer.store_experience_at_idx(mem_idx, None, None, None, reward, None, done)

                    # Accumulate rewards.
                    episode_rewards[env_idx][agent_id] += reward
                    exp_idx += 1

            # Check if any episodes have completed.
            newly_completed_episodes = sum(dones)
            if newly_completed_episodes > 0:
                # Update progress bar for episodes-based training
                if training_time is None:
                    progress_bar.update(newly_completed_episodes)

                total_episodes_so_far += newly_completed_episodes
                stats_dict["Episodes"] = total_episodes_so_far

                # Check if we should save the model.
                if save_interval > 0 and (total_episodes_so_far - last_save_episode) >= save_interval:
                    checkpoint_dir = "checkpoints"
                    if output_path:
                        if os.path.isdir(output_path):
                            checkpoint_dir = os.path.join(output_path, "checkpoints")
                        else:
                            # If output path is a file, use its directory
                            output_dir = os.path.dirname(output_path)
                            checkpoint_dir = os.path.join(output_dir if output_dir else ".", "checkpoints")

                    os.makedirs(checkpoint_dir, exist_ok=True)

                    # save checkpoint
                    checkpoint_path = os.path.join(checkpoint_dir, f"model_{total_episodes_so_far}.pt")
                    trainer.save_models(checkpoint_path)

                    # Also save as "latest" for easy loading (keep this for compatibility)
                    latest_path = os.path.join(checkpoint_dir, "model_latest.pt")
                    trainer.save_models(latest_path)

                    if debug:
                        print(f"[DEBUG] Saved checkpoint at episode {total_episodes_so_far} to {checkpoint_path}")

                    last_save_episode = total_episodes_so_far

                # Reset episode rewards for the environments that finished an episode.
                for env_idx, done in enumerate(dones):
                    if done:
                        avg_reward = sum(episode_rewards[env_idx].values()) / len(episode_rewards[env_idx])
                        if debug:
                            print(f"Episode {episode_counts[env_idx]} in env {env_idx} completed with avg reward: {avg_reward:.2f}")
                        episode_rewards[env_idx] = {agent_id: 0 for agent_id in vec_env.obs_dicts[env_idx]}

            # Check if it's time to update the policy.
            enough_experiences = collected_experiences >= update_interval

            # Update only if we've collected enough experiences - removed time-based fallback
            if enough_experiences and not test:
                if debug:
                    print(f"[DEBUG] Updating policy with {collected_experiences} experiences after {time.time() - last_update_time:.2f}s")

                # Perform the policy update.
                stats = trainer.update()

                # Update the statistics displayed in the progress bar.
                stats_dict.update({
                    "Device": device,
                    "Envs": num_envs,
                    "Exp": f"0/{update_interval}",  # Reset experience count
                    "Episodes": total_episodes_so_far,
                    "Reward": f"{stats.get('mean_episode_reward', 0):.2f}",
                    "PLoss": f"{stats.get('actor_loss', 0):.4f}",
                    "VLoss": f"{stats.get('critic_loss', 0):.4f}",
                    "Entropy": f"{stats.get('entropy_loss', 0):.4f}",
                    "SR_Loss": f"{stats.get('sr_loss', 0):.4f}",
                    "RP_Loss": f"{stats.get('rp_loss', 0):.4f}"
                })

                # Update curriculum stats if enabled
                if curriculum_manager:
                    curriculum_stats = curriculum_manager.get_curriculum_stats()
                    stats_dict.update({
                        "Stage": curriculum_stats["current_stage_name"],
                        "Diff": f"{curriculum_stats['difficulty_level']:.2f}"
                    })
                    
                    if use_wandb:
                        # Get current stage stats
                        current_stage_stats = curriculum_stats["current_stage_stats"]
                        
                        # Use trainer's _true_training_steps as source of truth for step counting
                        # This ensures we're using EXACTLY the same step as the trainer just used
                        current_step = trainer._true_training_steps
                        
                        # Synchronize curriculum manager step counter to match trainer exactly
                        curriculum_manager._last_wandb_step = current_step
                        
                        # Don't log curriculum metrics separately - the PPOTrainer.update() method 
                        # already logs these through the synchronized step counter
                        # This prevents duplicate/competing logging attempts
                        
                        # Instead, we'll update the trainer's curriculum manager reference
                        # to ensure that it has the most up-to-date curriculum statistics
                        # for its own logging in the next update
                        if hasattr(trainer, 'curriculum_manager'):
                            # Just make sure the trainer's reference is up to date
                            trainer.curriculum_manager = curriculum_manager
                
                progress_bar.set_postfix(stats_dict)

                collected_experiences = 0
                last_update_time = time.time()

            # Update the experience count in the progress bar.
            stats_dict["Exp"] = f"{collected_experiences}/{update_interval}"
            progress_bar.set_postfix(stats_dict)

    except KeyboardInterrupt:
        print("\nTraining interrupted. Cleaning up...")
    except Exception as e:
        print(f"Error during training: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        # Always clean up the environments and progress bar.
        vec_env.close()
        progress_bar.close()

        # Perform a final policy update if there are any remaining experiences.
        if collected_experiences > 0 and not test:
            if debug:
                print(f"[DEBUG] Final update with {collected_experiences} experiences")
            try:
                trainer.update()  # Final update
            except Exception as e:
                print(f"Error during final update: {str(e)}")
                import traceback
                traceback.print_exc()

        # Return the trainer regardless of potential errors
        return trainer

def parse_time(time_str):
    """
    Parses a string representing a time duration (e.g., '5m', '2h', '1d') and converts it to seconds.
    """
    if not time_str:
        return None

    # Get the number and the unit (minutes, hours, or days).
    time_str = time_str.lower().strip()
    if len(time_str) < 2:
        raise ValueError(f"Invalid time format: {time_str}. Use format like '5m', '2h', '1d'")

    try:
        value = float(time_str[:-1])
        unit = time_str[-1]

        # Convert to seconds.
        if unit == 'm':  # minutes
            return value * 60
        elif unit == 'h':  # hours
            return value * 3600
        elif unit == 'd':  # days
            return value * 86400
        else:
            raise ValueError(f"Unknown time unit: {unit}. Use 'm' for minutes, 'h' for hours, 'd' for days")
    except ValueError as e:
        if "Unknown time unit" in str(e):
            raise
        raise ValueError(f"Invalid time format: {time_str}. Use format like '5m', '2h', '1d'")


def signal_handler(sig, frame):
    """Handles Ctrl+C gracefully, so the program exits cleanly."""
    print("\nInterrupted by user. Cleaning up...")
    sys.exit(0)

if __name__ == "__main__":
    # Set start method
    if sys.platform == 'darwin':
        mp.set_start_method('spawn', force=True)
    elif sys.platform == 'linux':
        mp.set_start_method('fork', force=True)

    # Set up Ctrl+C handler to exit gracefully.
    signal.signal(signal.SIGINT, signal_handler)

    parser = argparse.ArgumentParser(description='RLBot Training Script')
    parser.add_argument('--render', action='store_true', help='Enable rendering of the game environment')

    # Allow user to specify training duration either by episode count OR by time.
    training_duration = parser.add_mutually_exclusive_group()
    training_duration.add_argument('-e', '--episodes', type=int, default=5000, help='Number of episodes to run')
    training_duration.add_argument('-t', '--time', type=str, default=None,
                                  help='Training duration in format: 5m (minutes), 5h (hours), 5d (days)')

    parser.add_argument('-n', '--num_envs', type=int, default=30 if sys.platform != "darwin" else 12,
                        help='Number of parallel environments to run for faster data collection')
    parser.add_argument('--update_interval', type=int, default=3072,
                        help='Number of experiences to collect before updating the policy')
    parser.add_argument('--device', type=str, default=None,
                       help='Device to use for training (cuda/mps/cpu).  Autodetects if not specified.')
    parser.add_argument('--wandb', action='store_true', help='Enable logging to Weights & Biases')
    parser.add_argument('--debug', action='store_true', help='Enable verbose debug logging')

    parser.add_argument('--render-delay', type=float, default=0.025,
                    help='Delay between rendered frames in seconds (higher values = slower visualization)')

    parser.add_argument('--curriculum', action='store_true',
                    help='Enable curriculum learning')
    parser.add_argument('--no-curriculum', action='store_false', dest='curriculum',
                    help='Disable curriculum learning')
    parser.set_defaults(curriculum=True)


    # Learning rates
    parser.add_argument('--lra', type=float, default=5e-5, help='Learning rate for actor network')
    parser.add_argument('--lrc', type=float, default=1e-4, help='Learning rate for critic network')

    # Discount factors
    parser.add_argument('--gamma', type=float, default=0.997, help='Discount factor for future rewards')
    parser.add_argument('--gae_lambda', type=float, default=0.95, help='Lambda para,meter for Generalized Advantage Estimation')

    # PPO parameters
    parser.add_argument('--clip_epsilon', type=float, default=0.15, help='PPO clipping parameter')
    parser.add_argument('--critic_coef', type=float, default=1.0, help='Weight of the critic loss')
    parser.add_argument('--entropy_coef', type=float, default=0.005, help='Weight of the entropy bonus (encourages exploration)')
    parser.add_argument('--max_grad_norm', type=float, default=1.0, help='Maximum gradient norm for clipping')

    # Training loop parameters
    parser.add_argument('--ppo_epochs', type=int, default=10, help='Number of PPO epochs per update')
    parser.add_argument('--batch_size', type=int, default=2048, help='Batch size for PPO updates')

    parser.add_argument('--compile', action='store_true', help='Use torch.compile for model optimization (if available)')
    parser.add_argument('--no-compile', action='store_false', dest='compile', help='Disable torch.compile')
    parser.set_defaults(compile=True)

    parser.add_argument('--amp', action='store_true', help='Use automatic mixed precision for faster training (requires CUDA)')
    parser.add_argument('--no-amp', action='store_false', dest='amp', help='Disable automatic mixed precision')
    parser.set_defaults(amp=False)

    parser.add_argument('-m', '--model', type=str, default=None,
                        help='Path to a pre-trained model file to load')

    parser.add_argument('-o', '--out', type=str, default=None,
                    help='Path where the trained model will be saved')

    parser.add_argument('--test', action='store_true',
                        help='Enable test mode (enables rendering and limits to 1 environment)')


    parser.add_argument('--save_interval', type=int, default=200,
                       help='Save the model every N episodes')

    parser.add_argument('--hidden_dim', type=int, default=512, help='Hidden dimension for the network')
    parser.add_argument('--num_blocks', type=int, default=3, help='Number of residual blocks in the network')
    parser.add_argument('--dropout', type=float, default=0.05, help='Dropout rate for regularization')

    # Action stacking parameters
    parser.add_argument('--stack_size', type=int, default=5, help='Number of previous actions to stack')

    # Auxiliary learning parameters
    parser.add_argument('--auxiliary', action='store_false', dest='auxiliary',
                        help='Disable auxiliary task learning (SR and RP tasks)')
    parser.add_argument('--no-auxiliary', action='store_true', dest='auxiliary',
                        help='Disable auxiliary task learning (SR and RP tasks) (for backward compatibility)')
    parser.set_defaults(auxiliary=True)

    parser.add_argument('--sr_weight', type=float, default=1.0,
                        help='Weight for the State Representation auxiliary task')
    parser.add_argument('--rp_weight', type=float, default=1.0,
                        help='Weight for the Reward Prediction auxiliary task')

    # Add option to control AMP for auxiliary tasks specifically
    parser.add_argument('--aux-amp', action='store_true',
                        help='Enable AMP for auxiliary tasks (can be disabled separately from main training)')
    parser.add_argument('--no-aux-amp', action='store_false', dest='aux_amp',
                        help='Disable AMP for auxiliary tasks even if main training uses AMP')
    parser.set_defaults(aux_amp=False)

    parser.add_argument('--aux_freq', type=int, default=8,
                        help='Auxiliary task update frequency (higher = less frequent updates)')

    parser.add_argument('--aux_scale', type=float, default=0.005,
                        help='Scaling factor for auxiliary task losses')


    # Backwards compatibility.
    parser.add_argument('-p', '--processes', type=int, default=None,
                        help='Legacy parameter; use --num_envs instead')

    args = parser.parse_args()

    # If a training time is provided, parse it into seconds.
    training_time_seconds = None
    if args.time is not None:
        try:
            training_time_seconds = parse_time(args.time)
            if args.debug:
                print(f"[DEBUG] Training time set to {args.time} ({training_time_seconds} seconds)")
        except ValueError as e:
            print(str(e))
            sys.exit(1)

    if args.test:
        args.render = True
        args.num_envs = 1
        args.auxiliary = False
        args.curriculum = False
        print("Test mode enabled: Rendering ON, using 1 environment")

    # Handle legacy --processes argument.
    if args.processes is not None and args.num_envs == 4:  # 4 is the default for num_envs
        args.num_envs = args.processes
        if args.debug:
            print(f"[DEBUG] Using --processes value ({args.processes}) for number of environments")

    # Create action stacker
    action_stacker = ActionStacker(stack_size=args.stack_size, action_size=8)  # RLGym uses 8 actions

    # Get the dimensions of the observation and action spaces.
    env = get_env(action_stacker=action_stacker)
    env.reset()
    obs_space = env.observation_space(env.agents[0])
    obs_space_dims = obs_space[0]
    action_space_dims = env.action_space(env.agents[0])[1]
    env.close()

    # Use the best available device (CUDA if available, then MPS, then CPU).
    device = args.device
    if device is None:
        if torch.cuda.is_available():
            device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

    actor = SimBa(obs_shape=obs_space_dims, action_shape=action_space_dims,
                    hidden_dim=args.hidden_dim, num_blocks=args.num_blocks,
                    dropout_rate=args.dropout, device=device)
    critic = SimBa(obs_shape=obs_space_dims, action_shape=1,
                    hidden_dim=args.hidden_dim, num_blocks=args.num_blocks,
                    dropout_rate=args.dropout, device=device)

    torch.set_printoptions(precision=10)

    if "cuda" in str(device):
        # CUDA-specific optimizations
        torch.set_float32_matmul_precision('high')  # Use Tensor Cores
        torch.backends.cuda.matmul.allow_tf32 = True  # Allow TF32 on Ampere and newer
        torch.backends.cudnn.allow_tf32 = True
        torch.backends.cudnn.benchmark = True  # Enable cudnn autotuner

        # Configure BLAS operations
        torch.backends.cuda.preferred_linalg_library('cusolver')  # Prefer cuSOLVER for stability

        # Try to set bailout depth for CUDA graphs
        try:
            torch._C._jit_set_bailout_depth(20)
        except AttributeError:
            if args.debug:
                print("[DEBUG] _jit_set_bailout_depth not available in this PyTorch version")

        torch.cuda.set_device(torch.cuda.current_device())

        # Improve CUDA graph memory allocation
        os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'

        # Configure Dynamo for safer CUDA graphs
        if hasattr(torch, '_dynamo'):
            try:
                torch._dynamo.config.cache_size_limit = 16  # Limit cache size
                torch._dynamo.config.suppress_errors = True
                if args.debug:
                    torch._dynamo.config.verbose = True
            except AttributeError:
                if args.debug:
                    print("[DEBUG] torch._dynamo.config not available in this PyTorch version")

    # Set up Weights & Biases for experiment tracking, if enabled.
    if args.wandb:
        import wandb
        # Initialize wandb with proper step counting setup
        run = wandb.init(
            project="rlbot-training",
            resume="allow",  # Allow resuming previous runs
            config={
                # Hyperparameters
                "learning_rate_actor": args.lra,
                "learning_rate_critic": args.lrc,
                "gamma": args.gamma,
                "gae_lambda": args.gae_lambda,
                "clip_epsilon": args.clip_epsilon,
                "critic_coef": args.critic_coef,
                "entropy_coef": args.entropy_coef,
                "max_grad_norm": args.max_grad_norm,
                "ppo_epochs": args.ppo_epochs,
                "batch_size": args.batch_size,

                # Model Details
                "hidden_dim": args.hidden_dim,
                "num_blocks": args.num_blocks,
                "dropout": args.dropout,
                "action_stack_size": args.stack_size,
                "auxiliary_tasks": args.auxiliary,
                "sr_weight": args.sr_weight,
                "rp_weight": args.rp_weight,

                # Environment details
                "action_repeat": 8,
                "num_agents": 4,  # 2v2

                # System configuration
                "episodes": args.episodes,
                "training_time": args.time,
                "num_envs": args.num_envs,
                "update_interval": args.update_interval,
                "device": device,
            },
            name=f"PPO_{time.strftime('%Y%m%d-%H%M%S')}",
            monitor_gym=False,  # Don't use wandb's default gym monitoring
        )

    if args.debug:
        print(f"[DEBUG] Starting training with {args.num_envs} environments on {device}")
        print(f"[DEBUG] Actor model: {actor}")
        print(f"[DEBUG] Critic model: {critic}")
        print(f"[DEBUG] Action stacking size: {args.stack_size}")
        if args.time:
            print(f"[DEBUG] Training for {args.time} ({training_time_seconds} seconds)")
        else:
            print(f"[DEBUG] Training for {args.episodes} episodes")


    if args.model:
        try:
            # Load a pre-trained model.
            # Load checkpoint and handle wandb run ID if present
            checkpoint = torch.load(args.model, map_location=device)
            wandb_run_id = checkpoint.get('wandb_run_id')

            if args.debug:
                print(f"[DEBUG] Loaded checkpoint from {args.model}")
                if wandb_run_id:
                    print(f"[DEBUG] Found wandb run ID: {wandb_run_id}")

            # If using wandb and checkpoint has a run ID, try to resume that run
            if args.wandb and wandb_run_id:
                try:
                    wandb.init(id=wandb_run_id, resume="must")
                    print(f"Resuming wandb run {wandb_run_id}")
                except Exception as e:
                    print(f"Could not resume wandb run {wandb_run_id}: {e}")
                    # Fall back to new run
                    wandb.init()

            # If the checkpoint contains both actor and critic, extract their parameters.
            if isinstance(checkpoint, dict) and 'actor' in checkpoint and 'critic' in checkpoint:
                actor_obs_shape, actor_hidden_dim, actor_action_shape, actor_num_blocks = extract_model_dimensions(checkpoint['actor'])
                critic_obs_shape, critic_hidden_dim, critic_action_shape, critic_num_blocks = extract_model_dimensions(checkpoint['critic'])

                # Validate and fix observation shape if needed
                if actor_obs_shape != obs_space_dims:
                    print(f"Warning: Model expects observation shape {actor_obs_shape}, but environment has {obs_space_dims}.")
                    print("Using environment's observation shape for model.")
                    actor_obs_shape = obs_space_dims
                    critic_obs_shape = obs_space_dims

                if args.debug:
                    print("[DEBUG] Extracted model dimensions from checkpoint:")
                    print(f"[DEBUG] Actor: obs_shape={actor_obs_shape}, hidden_dim={actor_hidden_dim}, action_shape={actor_action_shape}, num_blocks={actor_num_blocks}")
                    print(f"[DEBUG] Critic: obs_shape={critic_obs_shape}, hidden_dim={critic_hidden_dim}, action_shape={critic_action_shape}, num_blocks={critic_num_blocks}")

                # Recreate the models with the correct dimensions
                actor = SimBa(
                    obs_shape=actor_obs_shape,
                    action_shape=actor_action_shape,
                    hidden_dim=int(actor_hidden_dim) if actor_hidden_dim is not None else 1024,
                    num_blocks=actor_num_blocks if actor_num_blocks is not None else 4
                )

                critic = SimBa(
                    obs_shape=critic_obs_shape,
                    action_shape=critic_action_shape,
                    hidden_dim=int(critic_hidden_dim) if critic_hidden_dim is not None else 1024,
                    num_blocks=critic_num_blocks if critic_num_blocks is not None else 4
                )

                # Load the model weights, skipping mismatched layers
                load_partial_state_dict(actor, checkpoint['actor'])
                load_partial_state_dict(critic, checkpoint['critic'])
                # Get training step count from checkpoint if available
                if 'training_step' in checkpoint:
                    trainer_offset = checkpoint.get('training_step', 0)
                    if args.debug:
                        print(f"[DEBUG] Loaded training step offset: {trainer_offset}")
                else:
                    trainer_offset = 0

                print(f"Successfully loaded model from {args.model} with adjusted dimensions")
                print(f"Continuing from training step {trainer_offset}")

            else:
                print(f"Error: Unsupported model format in {args.model}")
        except Exception as e:
            print(f"Error loading model: {e}")
            if args.debug:
                import traceback
                traceback.print_exc()

    # Get the training step offset if we loaded a checkpoint
    trainer_offset = trainer_offset if 'trainer_offset' in locals() else 0

    # Start the main training process with proper step counting
    trainer = run_training(
        actor=actor,
        critic=critic,
        training_step_offset=trainer_offset,  # Pass the offset to maintain step counting
        device=device,
        num_envs=args.num_envs,
        total_episodes=args.episodes if args.time is None else None,
        training_time=training_time_seconds,
        render=args.render,
        update_interval=args.update_interval,
        use_wandb=args.wandb,
        debug=args.debug,
        use_compile=args.compile,
        use_amp=args.amp,
        save_interval=args.save_interval,
        output_path=args.out,
        use_curriculum=args.curriculum,
        # Hyperparameters
        lr_actor=args.lra,
        lr_critic=args.lrc,
        gamma=args.gamma,
        gae_lambda=args.gae_lambda,
        clip_epsilon=args.clip_epsilon,
        critic_coef=args.critic_coef,
        entropy_coef=args.entropy_coef,
        max_grad_norm=args.max_grad_norm,
        ppo_epochs=args.ppo_epochs,
        batch_size=args.batch_size,
        aux_amp=args.aux_amp if args.aux_amp is not None else args.amp
    )



    # Save the final trained models, unless we're in test mode or training failed.
    saved_path = None
    if trainer is not None:  # Only check if trainer exists, not test mode
        # Always save when trainer exists and small number of episodes were run - these are likely evaluation runs
        output_path = args.out if args.out else None
        # Save model with step count and wandb info
        metadata = {
            'training_step': getattr(trainer, '_true_training_steps', 0),
            'wandb_run_id': wandb.run.id if args.wandb and wandb.run else None
        }
        saved_path = trainer.save_models(output_path, metadata)  # Capture the returned path
        print(f"Training complete - Model saved to {saved_path} at step {metadata['training_step']}")
    else:
        print("Training failed - no model saved.")

    # Upload the saved model to WandB as an artifact
    if args.wandb and saved_path and os.path.exists(saved_path):
        try:
            import wandb
            if wandb.run is not None:
                # Create a name for the artifact
                artifact_name = f"model_{wandb.run.id}"
                if args.time is not None:
                    artifact_name += f"_t{int(parse_time(args.time) if args.time else 0)}s"
                else:
                    artifact_name += f"_ep{args.episodes}"

                # Log the model as an artifact with metadata
                artifact = wandb.Artifact(
                    name=artifact_name,
                    type="model",
                    description=f"RL model trained for {args.episodes if args.time is None else args.time}"
                )

                # Add the model file
                artifact.add_file(saved_path)

                # Add metadata
                metadata = {
                    "episodes": args.episodes if args.time is None else None,
                    "training_time": args.time,
                    "device": str(device),
                    "lr_actor": args.lra,
                    "lr_critic": args.lrc,
                    "gamma": args.gamma,
                    "gae_lambda": args.gae_lambda,
                    "clip_epsilon": args.clip_epsilon,
                    "critic_coef": args.critic_coef,
                    "entropy_coef": args.entropy_coef,
                    "model_type": type(actor).__name__,
                    "num_envs": args.num_envs,
                    "update_interval": args.update_interval,
                    "saved_path": saved_path,
                    'model_config': {
                        'hidden_dim': args.hidden_dim,
                        'num_blocks': args.num_blocks,
                        'dropout': args.dropout
                    },
                }

                # Add metadata to the artifact
                for key, value in metadata.items():
                    if value is not None:  # Only add non-None values
                        artifact.metadata[key] = value

                # Log the artifact to wandb
                wandb.log_artifact(artifact)

                if args.debug:
                    print(f"[DEBUG] Uploaded model to WandB as artifact '{artifact_name}'")
                else:
                    print(f"Uploaded model to WandB as artifact '{artifact_name}'")
        except ImportError:
            print("WandB not available, skipping artifact upload")
        except Exception as e:
            print(f"Error uploading model to WandB: {str(e)}")
            if args.debug:
                import traceback
                traceback.print_exc()
