import concurrent.futures
import time
import multiprocessing as mp
from multiprocessing import Process, Pipe
import numpy as np
from typing import List, Tuple, Dict
from rlgym.rocket_league.rlviser import RLViserRenderer
from rlgym.rocket_league.state_mutators import FixedTeamSizeMutator
from .factory import get_env
from rlgym.rocket_league.done_conditions import TimeoutCondition
import select

def worker(remote: mp.connection.Connection, env_fn, render: bool, action_stacker=None, curriculum_config=None, debug=False):
    """Worker process that runs a single environment"""
    try:
        # Create environment first - with extra safety
        if debug:
            print(f"[DEBUG] Worker process creating environment with config: {curriculum_config['stage_name'] if curriculum_config else 'Default'}")
        
        env = None
        max_retries = 3
        for attempt in range(max_retries):
            try:
                env = env_fn(renderer=None, action_stacker=action_stacker, curriculum_config=curriculum_config, debug=debug)
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    if debug:
                        print(f"[DEBUG] Attempt {attempt + 1} failed: {e}, retrying...")
                    time.sleep(1)  # Small delay before retry
                else:
                    raise
        
        if env is None:
            raise RuntimeError("Failed to create environment after max retries")
            
        # Then create renderer if needed - with safety
        renderer = None
        if render:
            try:
                from rlgym.rocket_league.rlviser import RLViserRenderer
                renderer = RLViserRenderer()
                env.renderer = renderer
            except Exception as e:
                if debug:
                    print(f"[DEBUG] Error creating renderer: {e}")
                
        # Initialize tick count at creation time
        if hasattr(env, 'transition_engine'):
            env.transition_engine._tick_count = 0
        
        # Initialize initial_tick for TimeoutCondition if it exists
        if hasattr(env, 'truncation_cond'):
            def set_initial_tick(cond):
                if isinstance(cond, TimeoutCondition):
                    cond.initial_tick = 0
                elif hasattr(cond, 'conditions'):
                    for sub_cond in cond.conditions:
                        set_initial_tick(sub_cond)
            set_initial_tick(env.truncation_cond)
            
        curr_config = curriculum_config  # Keep track of current curriculum config

        while True:
            try:
                # Use select with timeout for safer IPC
                if hasattr(remote, 'poll') and hasattr(select, 'select'):
                    if not select.select([remote], [], [], 30.0)[0]:  # 30 second timeout
                        if debug:
                            print("[DEBUG] Worker timeout waiting for command")
                        continue
                
                cmd, data = remote.recv()
                
                if cmd == 'step':
                    actions_dict = data
                    # Format actions for RLGym API
                    formatted_actions = {}
                    for agent_id, action in actions_dict.items():
                        if isinstance(action, np.ndarray):
                            formatted_actions[agent_id] = action
                        else:
                            formatted_actions[agent_id] = np.array([action if isinstance(action, int) else int(action)])
                        # Add action to stacker history
                        if action_stacker is not None:
                            action_stacker.add_action(agent_id, formatted_actions[agent_id])
                    # Step the environment
                    next_obs_dict, reward_dict, terminated_dict, truncated_dict = env.step(formatted_actions)
                    remote.send((next_obs_dict, reward_dict, terminated_dict, truncated_dict))
                
                elif cmd == 'reset':
                    if debug:
                        print(f"[DEBUG] Worker resetting with stage: {curr_config.get('stage_name', 'Unknown') if curr_config else 'Default'}")
                    
                    # Add retry logic for resets
                    reset_success = False
                    for reset_attempt in range(3):  # Try up to 3 times
                        try:
                            obs = env.reset()
                            reset_success = True
                            break
                        except Exception as e:
                            if debug:
                                print(f"[DEBUG] Reset attempt {reset_attempt + 1} failed: {e}")
                            if reset_attempt == 2:  # Last attempt
                                raise
                            time.sleep(0.1)  # Small delay between attempts
                    
                    if reset_success:
                        # Update timeout initial tick
                        if hasattr(env, 'truncation_cond'):
                            def set_initial_tick(cond):
                                if isinstance(cond, TimeoutCondition):
                                    cond.initial_tick = env.transition_engine._tick_count
                                elif hasattr(cond, 'conditions'):
                                    for sub_cond in cond.conditions:
                                        set_initial_tick(sub_cond)
                            set_initial_tick(env.truncation_cond)
                        remote.send(obs)
                        if debug:
                            print(f"[DEBUG] Reset successful for {curr_config.get('stage_name', 'Unknown') if curr_config else 'Default'}")
                
                elif cmd == 'set_curriculum':
                    # Update environment with new curriculum configuration
                    old_config = curr_config
                    curr_config = data
                    
                    if debug:
                        old_stage = old_config.get('stage_name', 'Unknown') if old_config else 'Default'
                        new_stage = curr_config.get('stage_name', 'Unknown') if curr_config else 'Default'
                        print(f"[DEBUG] Changing stage from {old_stage} to {new_stage}")
                    
                    # Safely close and recreate environment
                    try:
                        if renderer:
                            temp_renderer = env.renderer
                            env.renderer = None  # Prevent renderer from being closed
                        env.close()
                        env = env_fn(renderer=renderer, action_stacker=action_stacker, curriculum_config=curr_config, debug=debug)
                        if renderer:
                            env.renderer = temp_renderer
                        remote.send(True)  # Acknowledge the update
                    except Exception as e:
                        if debug:
                            print(f"[DEBUG] Error recreating environment: {e}")
                        raise
                
                elif cmd == 'close':
                    if renderer:
                        renderer.close()
                    env.close()
                    remote.close()
                    break
                
                elif cmd == 'reset_action_stacker':
                    agent_id = data
                    if action_stacker is not None:
                        action_stacker.reset_agent(agent_id)
                    remote.send(True)  # Acknowledge
                    
            except EOFError:
                break
            except Exception as e:
                import traceback
                print(f"Error in worker: {str(e)}")
                print(traceback.format_exc())
                break
                
    except Exception as e:
        import traceback
        print(f"Fatal error in worker initialization: {str(e)}")
        print(traceback.format_exc())
        raise

class VectorizedEnv:
    """
    Runs multiple RLGym environments in parallel.
    Uses thread-based execution for rendered environments and
    multiprocessing for non-rendered environments.
    Now supports curriculum learning.
    """
    def __init__(self, num_envs, render=False, action_stacker=None, curriculum_manager=None, debug=False):
        self.num_envs = num_envs
        self.render = render
        self.action_stacker = action_stacker
        self.curriculum_manager = curriculum_manager
        self.render_delay = 0.025
        self.debug = debug

        # For tracking episode metrics for curriculum
        self.episode_rewards = [{} for _ in range(num_envs)]
        self.episode_successes = [False] * num_envs
        self.episode_timeouts = [False] * num_envs

        # Get curriculum configurations if available
        self.curriculum_configs = []
        for env_idx in range(num_envs):
            if self.curriculum_manager:
                # Get potentially different configs for each environment (for rehearsal)
                config = self.curriculum_manager.get_environment_config()
                
                if self.debug:
                    print(f"[DEBUG] Env {env_idx} initialized with stage: {config['stage_name']}")
                    # Check if config has car position mutator
                    state_mutator = config['state_mutator']
                    has_car_pos = False
                    if hasattr(state_mutator, 'mutators'):
                        for i, mutator in enumerate(state_mutator.mutators):
                            if 'CarPositionMutator' in mutator.__class__.__name__:
                                has_car_pos = True
                                print(f"[DEBUG] Env {env_idx}, stage {config['stage_name']} has CarPositionMutator at index {i}")
                                break
                    if not has_car_pos:
                        print(f"[DEBUG] WARNING: Env {env_idx}, stage {config['stage_name']} has NO CarPositionMutator!")
                
                # Process config to make it picklable for multiprocessing
                if not render:  # Only needed for multiprocessing mode
                    config = self._make_config_picklable(config)
                self.curriculum_configs.append(config)
            else:
                self.curriculum_configs.append(None)

        # Decide whether to use threading for rendering
        if render:
            # Use thread-based approach for all environments when rendering is enabled
            self.mode = "thread"
            # Create environments directly
            self.envs = []
            for i in range(num_envs):
                # Only create renderer for the first environment
                env_renderer = RLViserRenderer() if (i == 0) else None
                env = get_env(renderer=env_renderer, action_stacker=action_stacker,
                             curriculum_config=self.curriculum_configs[i], debug=self.debug)
                self.envs.append(env)
            # Reset all environments
            self.obs_dicts = [env.reset() for env in self.envs]
            # Explicitly render the first environment
            if num_envs > 0:
                self.envs[0].render()
            # Set up thread pool for parallel execution
            self.executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(32, num_envs),
                thread_name_prefix='EnvWorker'
            )
        else:
            # Use multiprocessing for maximum performance when not rendering
            self.mode = "multiprocess"
            # Create communication pipes
            self.remotes, self.work_remotes = zip(*[Pipe() for _ in range(num_envs)])
            # Create and start worker processes
            self.processes = []
            for idx, (work_remote, remote) in enumerate(zip(self.work_remotes, self.remotes)):
                process = Process(
                    target=worker,
                    args=(work_remote, get_env, False, action_stacker, self.curriculum_configs[idx], self.debug),
                    daemon=True
                )
                process.start()
                self.processes.append(process)
                work_remote.close()
            # Get initial observations
            for remote in self.remotes:
                remote.send(('reset', None))
        
                    
            self.obs_dicts = []
            for remote in self.remotes:
                if hasattr(remote, 'poll') and hasattr(select, 'select'):
                    # Use select with timeout
                    if select.select([remote], [], [], 30):  # 30 second timeout
                        self.obs_dicts.append(remote.recv())
                    else:
                        print(f"WARNING: Worker process timeout during initialization")
                        # Create empty dict as fallback
                        self.obs_dicts.append({})
                        # Send termination command to worker to prevent zombie processes
                        try:
                            remote.send(('close', None))
                        except:
                            pass
                else:
                    # Fallback if select not available
                    self.obs_dicts.append(remote.recv())

        # Common initialization
        self.dones = [False] * num_envs
        self.episode_counts = [0] * num_envs

    def _make_config_picklable(self, config):
        """Ensure valid team size configuration"""
        processed = dict(config)

        # Force required_agents field
        if "required_agents" not in processed:
            team_mutators = [m for m in processed["state_mutator"].mutators
                           if isinstance(m, FixedTeamSizeMutator)]
            if team_mutators:
                processed["required_agents"] = (
                    team_mutators[0].blue_size
                    + team_mutators[0].orange_size
                )
            else:
                processed["required_agents"] = 1

        return processed

    def _step_env(self, args):
        env_idx, env, actions_dict = args

        # Format actions for RLGym API
        formatted_actions = {}
        for agent_id, action in actions_dict.items():
            if isinstance(action, np.ndarray):
                formatted_actions[agent_id] = action
            else:
                formatted_actions[agent_id] = np.array([action if isinstance(action, int) else int(action)])

            # Add action to the stacker history
            if self.action_stacker is not None:
                self.action_stacker.add_action(agent_id, formatted_actions[agent_id])

        # Step the environment
        next_obs_dict, reward_dict, terminated_dict, truncated_dict = env.step(formatted_actions)

        # Add rendering and delay if this is the rendered environment
        if self.render and env_idx == 0:
            env.render()
            time.sleep(self.render_delay)

        # Track rewards for curriculum
        if self.curriculum_manager is not None:
            for agent_id, reward in reward_dict.items():
                if agent_id not in self.episode_rewards[env_idx]:
                    self.episode_rewards[env_idx][agent_id] = 0
                self.episode_rewards[env_idx][agent_id] += reward

        return env_idx, next_obs_dict, reward_dict, terminated_dict, truncated_dict

    def step(self, actions_dict_list):
        """Step all environments forward using appropriate method based on mode"""
        stats_dict = {}
        if self.mode == "thread":
            # Use thread pool for parallel execution
            futures = [
                self.executor.submit(self._step_env, (i, env, actions))
                for i, (env, actions) in enumerate(zip(self.envs, actions_dict_list))
                if actions  # Only submit if actions exist
            ]

            # Wait for all steps to complete
            results = []
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

            # Sort results by environment index
            results.sort(key=lambda x: x[0])

            processed_results = []
            for env_idx, next_obs_dict, reward_dict, terminated_dict, truncated_dict in results:
                # Check if episode is done
                self.dones[env_idx] = any(terminated_dict.values()) or any(truncated_dict.values())

                # Validate agents match expected
                config = self.curriculum_configs[i]
                if config is None:
                    required = len(next_obs_dict)
                else:
                    required = config.get("required_agents", len(next_obs_dict))

                # Track success/timeout for curriculum
                if self.dones[env_idx]:
                    self.episode_successes[env_idx] = any(terminated_dict.values())
                    self.episode_timeouts[env_idx] = any(truncated_dict.values()) and not self.episode_successes[env_idx]

                    # Update curriculum manager with episode results
                    if self.curriculum_manager:
                        # Calculate average reward across all agents
                        avg_reward = sum(self.episode_rewards[env_idx].values()) / max(len(self.episode_rewards[env_idx]), 1)

                        # Submit episode metrics
                        metrics = {
                            "success": self.episode_successes[env_idx],
                            "timeout": self.episode_timeouts[env_idx],
                            "episode_reward": avg_reward
                        }

                        self.curriculum_manager.update_progression_stats(metrics)

                        # Get new curriculum configuration for next episode
                        new_config = self.curriculum_manager.get_environment_config()
                        self.curriculum_configs[env_idx] = new_config

                        # In threaded mode, need to recreate the environment
                        if self.dones[env_idx]:
                            # 1) Detach the renderer before closing
                            existing_renderer = None
                            if env_idx == 0 and self.render:
                                existing_renderer = self.envs[env_idx].renderer
                                self.envs[env_idx].renderer = None  # Prevent renderer.close() in env.close()
                            
                            self.envs[env_idx].close()
                            
                            # 2) Reuse that same renderer (fall back to a new one if it was somehow None)
                            if env_idx == 0 and self.render:
                                if existing_renderer is None:
                                    existing_renderer = RLViserRenderer()
                                # Pass the old renderer back in
                                env_renderer = existing_renderer
                            else:
                                env_renderer = None
                            
                            self.envs[env_idx] = get_env(
                                renderer=env_renderer,
                                action_stacker=self.action_stacker,
                                curriculum_config=self.curriculum_configs[env_idx],
                                debug=self.debug
                            )
                            self.obs_dicts[env_idx] = self.envs[env_idx].reset()

                    if self.dones[env_idx]:
                        # If done, reset the environment
                        self.episode_counts[env_idx] += 1

                        max_reset_attempts = 3
                        for attempt in range(max_reset_attempts):
                            try:
                                obs = self.envs[env_idx].reset()
                                if len(obs) == required:
                                    self.obs_dicts[env_idx] = obs
                                    break
                            except Exception as e:
                                print(f"Reset attempt {attempt + 1} failed: {e}")
                                if attempt == max_reset_attempts - 1:
                                    print("Max reset attempts reached, recreating environment")
                                    self.envs[env_idx].close()
                                    self.envs[env_idx] = get_env(
                                        renderer=RLViserRenderer() if (env_idx == 0 and self.render) else None,
                                        action_stacker=self.action_stacker,
                                        curriculum_config=self.curriculum_configs[env_idx],
                                        debug=self.debug
                                    )
                                    self.obs_dicts[env_idx] = self.envs[env_idx].reset()

                        # Reset action history for all agents
                        if self.action_stacker is not None:
                            for agent_id in next_obs_dict.keys():
                                self.action_stacker.reset_agent(agent_id)

                        # Reset episode tracking variables
                        self.episode_rewards[env_idx] = {}
                        self.episode_successes[env_idx] = False
                        self.episode_timeouts[env_idx] = False

                        # Render again after reset if this is the rendered environment
                        if self.render and env_idx == 0:
                            self.envs[env_idx].render()
                            time.sleep(self.render_delay)
                    else:
                        # Otherwise just update observations
                        self.obs_dicts[env_idx] = next_obs_dict

                    processed_results.append((next_obs_dict, reward_dict, terminated_dict, truncated_dict))

        else:  # multiprocess mode
            # Send step command to all workers
            for remote, actions_dict in zip(self.remotes, actions_dict_list):
                remote.send(('step', actions_dict))

            # Collect results from all workers
            results = []
            for i, remote in enumerate(self.remotes):
                next_obs_dict, reward_dict, terminated_dict, truncated_dict = remote.recv()
                # Track rewards for curriculum
                if self.curriculum_manager is not None:
                    # FIX: Don't replace the dict, just ensure it's initialized
                    if not isinstance(self.episode_rewards[i], dict):
                        self.episode_rewards[i] = {}
                    
                    for agent_id, reward in reward_dict.items():
                        if agent_id not in self.episode_rewards[i]:
                            self.episode_rewards[i][agent_id] = 0
                        self.episode_rewards[i][agent_id] += reward
                
                # Check if episode is done
                self.dones[i] = any(terminated_dict.values()) or any(truncated_dict.values())

                # Validate agents match expected
                config = self.curriculum_configs[i]
                if config is None:
                    required = len(next_obs_dict)
                else:
                    required = config.get("required_agents", len(next_obs_dict))

                # Track success/timeout for curriculum
                if self.dones[i]:
                    self.episode_successes[i] = any(terminated_dict.values())
                    self.episode_timeouts[i] = any(truncated_dict.values()) and not self.episode_successes[i]

                    # Update curriculum manager with episode results
                    if self.curriculum_manager:
                        # Calculate average reward across all agents
                        if len(self.episode_rewards[i]) > 0:
                            avg_reward = sum(self.episode_rewards[i].values()) / len(self.episode_rewards[i])
                        else:
                            avg_reward = 0.0

                        # Submit episode metrics
                        self.curriculum_manager.update_progression_stats({
                            "success": self.episode_successes[i],
                            "timeout": self.episode_timeouts[i],
                            "episode_reward": avg_reward
                        })

                        # Get new curriculum configuration for next episode
                        new_config = self.curriculum_manager.get_environment_config()
                        # Process config to make it picklable
                        new_config = self._make_config_picklable(new_config)
                        self.curriculum_configs[i] = new_config

                        # Send the new curriculum configuration to the worker
                        remote.send(('set_curriculum', new_config))
                        remote.recv()  # Wait for acknowledgment

                    # If done, reset the environment with retry logic
                    self.episode_counts[i] += 1
                    max_reset_attempts = 3
                    for attempt in range(max_reset_attempts):
                        remote.send(('reset', None))
                        obs = remote.recv()
                        if len(obs) == required:
                            self.obs_dicts[i] = obs
                            break
                        print(f"Reset attempt {attempt + 1} failed")
                        if attempt == max_reset_attempts - 1:
                            print("Max reset attempts reached")

                    # Reset action stacker for all agents
                    if self.action_stacker is not None:
                        for agent_id in next_obs_dict.keys():
                            remote.send(('reset_action_stacker', agent_id))
                            remote.recv()  # Wait for confirmation

                    # Reset episode tracking variables
                    self.episode_rewards[i] = {}
                    self.episode_successes[i] = False
                    self.episode_timeouts[i] = False
                else:
                    # Otherwise just update observations
                    self.obs_dicts[i] = next_obs_dict

                results.append((next_obs_dict, reward_dict, terminated_dict, truncated_dict))

            processed_results = results

        return processed_results, self.dones.copy(), self.episode_counts.copy()

    def force_env_reset(self, env_idx):
        """Force reset a problematic environment (thread mode)"""
        if self.mode == "thread":
            existing_renderer = None
            if (env_idx == 0 and self.render and hasattr(self.envs[env_idx], 'renderer')):
                existing_renderer = self.envs[env_idx].renderer
            # Detach renderer before closing so it isn’t closed
            if existing_renderer is not None:
                self.envs[env_idx].renderer = None
            self.envs[env_idx].close()
            self.envs[env_idx] = get_env(
                renderer=existing_renderer,
                action_stacker=self.action_stacker,
                curriculum_config=self.curriculum_configs[env_idx],
                debug=self.debug
            )
            self.obs_dicts[env_idx] = self.envs[env_idx].reset()

    def close(self):
        """Clean up resources properly based on the mode"""
        if self.mode == "thread":
            # Close the thread pool
            if hasattr(self, 'executor'):
                self.executor.shutdown()

            # Close all environments
            if hasattr(self, 'envs'):
                for env in self.envs:
                    env.close()

        else:  # multiprocess mode
            # Close multiprocessing environments
            if hasattr(self, 'remotes'):
                for remote in self.remotes:
                    try:
                        remote.send(('close', None))
                    except (BrokenPipeError, EOFError):
                        pass  # Already closed

            if hasattr(self, 'processes'):
                for process in self.processes:
                    process.join(timeout=1.0)
                    if process.is_alive():
                        process.terminate()

            if hasattr(self, 'remotes'):
                for remote in self.remotes:
                    try:
                        remote.close()
                    except:
                        pass  # Already closed
