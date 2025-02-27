import numpy as np
import torch
import torch.multiprocessing as mp
from rlgym.api import RLGym
from rlgym.rocket_league.action_parsers import LookupTableAction, RepeatAction
from rlgym.rocket_league.done_conditions import GoalCondition, AnyCondition, TimeoutCondition, NoTouchTimeoutCondition
from rlgym.rocket_league.obs_builders import DefaultObs
from rlgym.rocket_league.reward_functions import CombinedReward, GoalReward, TouchReward
from rlgym.rocket_league.sim import RocketSimEngine
from rlgym.rocket_league.rlviser import RLViserRenderer
from rlgym.rocket_league.state_mutators import MutatorSequence, FixedTeamSizeMutator, KickoffMutator
from rewards import ProximityReward
from models import BasicModel
from training import PPOTrainer
import time
import argparse
from tqdm import tqdm
import os
import signal
import sys
from queue import Empty

def get_env():
    return RLGym(
        state_mutator=MutatorSequence(
            FixedTeamSizeMutator(blue_size=2, orange_size=2),
            KickoffMutator()
        ),
        obs_builder=DefaultObs(zero_padding=2),
        action_parser=RepeatAction(LookupTableAction(), repeats=8),
        reward_fn=CombinedReward(
            (GoalReward(), 12.),
            (TouchReward(), 3.),
            (ProximityReward(), 1.),
        ),
        termination_cond=GoalCondition(),
        truncation_cond=AnyCondition(
            TimeoutCondition(300.),
            NoTouchTimeoutCondition(30.)
        ),
        transition_engine=RocketSimEngine(),
        renderer=RLViserRenderer()
    )

def worker(
    rank,
    shared_actor,
    shared_critic,
    experience_queue,
    episode_counter,
    update_event,
    terminate_event,
    total_episodes,
    render,
    debug=False
):
    """Worker process that runs environment with local inference."""
    try:
        if debug:
            print(f"[DEBUG] Worker {rank} starting")

        # Create local copies of models for inference
        local_actor = BasicModel(
            input_size=shared_actor.network[0].in_features,
            output_size=shared_actor.network[-1].out_features,
            hidden_size=shared_actor.network[0].out_features
        )
        local_critic = BasicModel(
            input_size=shared_critic.network[0].in_features,
            output_size=shared_critic.network[-1].out_features,
            hidden_size=shared_critic.network[0].out_features
        )

        # Initialize with shared model weights
        local_actor.load_state_dict(shared_actor.state_dict())
        local_critic.load_state_dict(shared_critic.state_dict())

        # Create local trainer for action selection only
        local_trainer = PPOTrainer(local_actor, local_critic, device="cpu", debug=False)

        # Set up environment
        env = get_env()
        obs_dict = env.reset()

        # Process variables
        episode_steps = 0
        sync_interval = 10  # Sync models every N steps

        if debug:
            print(f"[DEBUG] Worker {rank} initialized")

        # Main worker loop
        while not terminate_event.is_set():
            # Check if we've reached total episodes
            with episode_counter.get_lock():
                if episode_counter.value >= total_episodes:
                    break

            # Sync with shared models periodically or when update is ready
            if update_event.is_set() or episode_steps % sync_interval == 0:
                local_actor.load_state_dict(shared_actor.state_dict())
                local_critic.load_state_dict(shared_critic.state_dict())

            # Process each agent's observation and compute actions locally
            actions = {}
            values = {}
            log_probs = {}

            for agent_id, obs in obs_dict.items():
                # Get action using local model
                action, log_prob, value = local_trainer.get_action(obs)
                actions[agent_id] = action
                log_probs[agent_id] = log_prob
                values[agent_id] = value

            # Render if enabled and this is the designated rendering worker
            if render and rank == 0:
                env.render()
                time.sleep(6/120)

            # Step the environment with the actions
            next_obs_dict, reward_dict, terminated_dict, truncated_dict = env.step(actions)

            # Send experiences to main process
            for agent_id in obs_dict.keys():
                obs = obs_dict[agent_id]
                action = actions[agent_id]
                log_prob = log_probs[agent_id]
                value = values[agent_id]
                reward = reward_dict[agent_id]

                # Check if episode ended for this agent
                terminated = terminated_dict[agent_id]
                truncated = truncated_dict[agent_id]
                done = terminated or truncated

                # Send experience tuple to queue
                experience = (obs, action, log_prob, reward, value, done)
                experience_queue.put(experience)

            # Update for next iteration
            obs_dict = next_obs_dict
            episode_steps += 1

            # Check if episode is done
            terminated = any(terminated_dict.values())
            truncated = any(truncated_dict.values())

            if terminated or truncated:
                if debug:
                    print(f"[DEBUG] Worker {rank} finished episode after {episode_steps} steps")

                # Reset environment
                obs_dict = env.reset()
                episode_steps = 0

                # Update episode counter
                with episode_counter.get_lock():
                    episode_counter.value += 1
                    current_episodes = episode_counter.value

                if debug:
                    print(f"[DEBUG] Worker {rank} completed episode. Total: {current_episodes}/{total_episodes}")

    except Exception as e:
        print(f"Error in worker process {rank}: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc()

    finally:
        # Clean up environment
        if 'env' in locals():
            env.close()
        if debug:
            print(f"[DEBUG] Worker {rank} shut down")

def run_parallel_training(
    actor,
    critic,
    device,
    num_processes: int,
    total_episodes: int,
    render: bool = False,
    update_interval: int = 1000,
    use_wandb: bool = False,
    debug: bool = False
):
    # Initialize variables that might be accessed in finally block
    collected_experiences = 0
    processes = []
    trainer = None  # Initialize trainer to None for safety

    try:
        # IMPORTANT: Move models to CPU first for sharing
        actor_cpu = actor.cpu()
        critic_cpu = critic.cpu()

        # Make CPU models shareable across processes
        shared_actor = actor_cpu.share_memory()
        shared_critic = critic_cpu.share_memory()

        # Create experience queue for collecting transitions
        experience_queue = mp.Queue(maxsize=update_interval*2)  # Allow buffer room

        # Create synchronization primitives
        update_event = mp.Event()     # Signal that model has been updated
        terminate_event = mp.Event()  # Signal workers to terminate
        episode_counter = mp.Value('i', 0)  # Shared episode counter

        # Setup progress tracking
        progress_bar = tqdm(total=total_episodes, bar_format='{n_fmt}/{total_fmt} [{bar}] {percentage:3.0f}% | {postfix}')
        progress_bar.set_postfix({"Device": device, "Workers": num_processes})

        # Start worker processes
        processes = []
        for rank in range(num_processes):
            p = mp.Process(
                target=worker,
                args=(
                    rank,
                    shared_actor,
                    shared_critic,
                    experience_queue,
                    episode_counter,
                    update_event,
                    terminate_event,
                    total_episodes,
                    render,
                    debug
                )
            )
            p.daemon = True
            p.start()
            processes.append(p)

        if debug:
            print(f"[DEBUG] Started {num_processes} worker processes")

        # Create separate models for the trainer on the target device
        # Don't share the CUDA models with worker processes
        if device != "cpu":
            trainer_actor = BasicModel(
                input_size=actor.network[0].in_features,
                output_size=actor.network[-1].out_features,
                hidden_size=actor.network[0].out_features
            ).to(device)

            trainer_critic = BasicModel(
                input_size=critic.network[0].in_features,
                output_size=critic.network[-1].out_features,
                hidden_size=critic.network[0].out_features
            ).to(device)

            # Copy the weights from CPU models
            trainer_actor.load_state_dict(actor_cpu.state_dict())
            trainer_critic.load_state_dict(critic_cpu.state_dict())
        else:
            # For CPU, we can use the shared models directly
            trainer_actor = shared_actor
            trainer_critic = shared_critic

        # Create trainer with the device-specific models
        trainer = PPOTrainer(trainer_actor, trainer_critic, device=device, use_wandb=use_wandb, debug=debug)

        # Main training loop variables
        collected_experiences = 0
        last_episode_count = 0
        last_update_time = time.time()

        # Main loop: collect experiences and update policy
        while episode_counter.value < total_episodes:
            # Update progress bar based on completed episodes
            current_episodes = episode_counter.value
            if current_episodes > last_episode_count:
                progress_increment = current_episodes - last_episode_count
                progress_bar.update(progress_increment)
                last_episode_count = current_episodes

                # Log to wandb if enabled
                if use_wandb:
                    wandb.log({
                        "episodes_completed": current_episodes,
                        "completion_percentage": current_episodes / total_episodes * 100
                    })

            # Check if processes are still alive
            alive_count = sum(1 for p in processes if p.is_alive())
            if alive_count < num_processes:
                print(f"WARNING: Only {alive_count}/{num_processes} processes alive")

            # Collect experiences from queue (non-blocking)
            try:
                # Try to get as many experiences as possible without blocking
                experience_batch_start = time.time()
                experiences_to_process = min(
                    experience_queue.qsize(),
                    update_interval - collected_experiences
                )

                for _ in range(experiences_to_process):
                    experience = experience_queue.get_nowait()
                    trainer.store_experience(*experience)
                    collected_experiences += 1

                if experiences_to_process > 0 and debug:
                    print(f"[DEBUG] Processed {experiences_to_process} experiences in {time.time() - experience_batch_start:.3f}s")

                # Update progress bar description
                if collected_experiences > 0:
                    progress_bar.set_description(f"Collecting: {collected_experiences}/{update_interval}")

            except Empty:
                # No experiences available right now, short sleep
                time.sleep(0.001)

            except Exception as e:
                print(f"Error collecting experiences: {str(e)}")
                import traceback
                traceback.print_exc()

            # Update policy when enough experiences are collected or enough time has passed
            time_since_update = time.time() - last_update_time
            enough_experiences = collected_experiences >= update_interval

            if enough_experiences or (collected_experiences > 100 and time_since_update > 30):
                if debug:
                    print(f"[DEBUG] Updating policy with {collected_experiences} experiences")

                if collected_experiences > 0:
                    update_start = time.time()
                    stats = trainer.update()
                    update_duration = time.time() - update_start

                    # The keys in stats match what your update method returns
                    avg_reward = np.mean(trainer.memory.rewards) if hasattr(trainer.memory, 'rewards') else 0
                    policy_loss = stats.get('actor_loss', 0)
                    value_loss = stats.get('critic_loss', 0)

                    # Use postfix with the correct metric names
                    progress_bar.set_postfix({
                        "Reward": f"{avg_reward:.2f}",
                        "P-Loss": f"{policy_loss:.4f}",
                        "V-Loss": f"{value_loss:.4f}",
                        "Update": f"{update_duration:.2f}s",
                        "Device": device
                    })

                    # After policy update, sync the weights back to the shared CPU models
                    if device != "cpu":
                        # Copy weights from trainer models back to the shared CPU models
                        with torch.no_grad():
                            for param_cpu, param_device in zip(shared_actor.parameters(), trainer_actor.parameters()):
                                param_cpu.copy_(param_device.cpu())
                            for param_cpu, param_device in zip(shared_critic.parameters(), trainer_critic.parameters()):
                                param_cpu.copy_(param_device.cpu())

                        # Signal workers to update their models
                        update_event.set()
                        # Clear the event after a short delay to ensure workers have time to see it
                        time.sleep(0.01)
                        update_event.clear()

                    collected_experiences = 0

                last_update_time = time.time()

    except KeyboardInterrupt:
        print("\nTraining interrupted. Cleaning up...")

    finally:
        # Signal all processes to terminate
        terminate_event.set()

        # Wait for processes to finish
        for i, p in enumerate(processes):
            if debug:
                print(f"[DEBUG] Waiting for worker {i} to terminate...")
            p.join(timeout=3.0)
            if p.is_alive():
                if debug:
                    print(f"[DEBUG] Forcing termination of worker {i}")
                p.terminate()

        progress_bar.close()

        # Final update with any remaining experiences
        if collected_experiences > 0 and trainer is not None:
            if debug:
                print(f"[DEBUG] Final update with {collected_experiences} experiences")
            trainer.update()

        return trainer

def signal_handler(sig, frame):
    print("\nInterrupted by user. Cleaning up...")
    sys.exit(0)

if __name__ == "__main__":
    # Register signal handler for clean exit
    signal.signal(signal.SIGINT, signal_handler)

    # Enable PyTorch multiprocessing support with spawn method
    mp.set_start_method('spawn', force=True)

    parser = argparse.ArgumentParser(description='RLBot Training Script')
    parser.add_argument('--render', action='store_true', help='Enable rendering')
    parser.add_argument('-e', '--episodes', type=int, default=200, help='Number of episodes to run')
    parser.add_argument('-p', '--processes', type=int, default=min(os.cpu_count() or 1, 4),
                        help='Number of parallel processes')
    parser.add_argument('--update_interval', type=int, default=1000,
                        help='Experiences before policy update')
    parser.add_argument('--device', type=str, default=None,
                       help='Device to use (cuda/mps/cpu). If not specified, will use best available.')
    parser.add_argument('--wandb', action='store_true', help='Enable Weights & Biases logging')
    parser.add_argument('--debug', action='store_true', help='Enable verbose debug logging')

    args = parser.parse_args()

    # Ensure at least one process
    args.processes = max(1, args.processes)

    # Initialize environment to get dimensions
    env = get_env()
    env.reset()
    obs_space_dims = env.observation_space(env.agents[0])[1]
    action_space_dims = env.action_space(env.agents[0])[1]
    env.close()

    # Initialize models
    actor = BasicModel(input_size=obs_space_dims, output_size=action_space_dims, hidden_size=obs_space_dims//2)
    critic = BasicModel(input_size=obs_space_dims, output_size=1, hidden_size=obs_space_dims//2)

    # Determine device
    device = args.device
    if device is None:
        if torch.cuda.is_available():
            device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

    # Move models to the device
    actor.to(device)
    critic.to(device)

    # Initialize wandb if requested
    if args.wandb:
        import wandb
        wandb.init(project="rlbot-training", config={
            "episodes": args.episodes,
            "processes": args.processes,
            "update_interval": args.update_interval,
            "device": device,
            "obs_space_dims": obs_space_dims,
            "action_space_dims": action_space_dims
        })

    # Start training with descriptive progress bar
    trainer = run_parallel_training(
        actor=actor,
        critic=critic,
        device=device,
        num_processes=args.processes,
        total_episodes=args.episodes,
        render=args.render,
        update_interval=args.update_interval,
        use_wandb=args.wandb,
        debug=args.debug
    )

    # Create models directory and save models
    os.makedirs("models", exist_ok=True)
    trainer.save_models("models/actor.pth", "models/critic.pth")
    print("Training complete - Models saved")
