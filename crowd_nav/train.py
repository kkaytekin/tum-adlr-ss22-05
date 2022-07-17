import sys
import logging
import configparser
import os
import shutil
import torch
import gym
import git
from crowd_sim.envs.utils.robot import Robot
from crowd_nav.utils.trainer import Trainer
from crowd_nav.utils.memory import ReplayMemory
from crowd_nav.utils.explorer import Explorer
from crowd_nav.policy.policy_factory import policy_factory
from crowd_nav.args import Parser
import re


def main():
    parser = Parser(mode='train')
    args = parser.parse()

    # configure paths
    make_new_dir = True
    if os.path.exists(args.output_dir):
        key = input('Output directory already exists! Overwrite the folder? (y/n)')
        if key == 'y' and not args.resume:
            shutil.rmtree(args.output_dir)
        else:
            make_new_dir = False
            args.env_config = os.path.join(args.output_dir, os.path.basename(args.env_config))
            args.policy_config = os.path.join(args.output_dir, os.path.basename(args.policy_config))
            args.train_config = os.path.join(args.output_dir, os.path.basename(args.train_config))
    if make_new_dir:
        os.makedirs(args.output_dir)
        shutil.copy(args.env_config, args.output_dir)
        shutil.copy(args.policy_config, args.output_dir)
        shutil.copy(args.train_config, args.output_dir)
    log_file = os.path.join(args.output_dir, 'output.log')
    il_weight_file = os.path.join(args.output_dir, 'il_model.pth')
    rl_weight_file = os.path.join(args.output_dir, 'rl_model.pth')
    best_rl_weight_file = os.path.join(args.output_dir, 'best_rl_model.pth')
    # configure logging
    mode = 'a' if args.resume else 'w'
    file_handler = logging.FileHandler(log_file, mode=mode)
    stdout_handler = logging.StreamHandler(sys.stdout)
    level = logging.INFO if not args.debug else logging.DEBUG
    logging.basicConfig(level=level, handlers=[stdout_handler, file_handler],
                        format='%(asctime)s, %(levelname)s: %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    repo = git.Repo(search_parent_directories=True)
    logging.info('Current git head hash code: %s'.format(repo.head.object.hexsha))
    device = torch.device("cuda:0" if torch.cuda.is_available() and args.gpu else "cpu")
    logging.info('Using device: %s', device)

    # configure policy
    policy = policy_factory[args.policy]()
    if not policy.trainable:
        parser.error('Policy has to be trainable')
    if args.policy_config is None:
        parser.error('Policy config has to be specified for a trainable network')
    policy_config = configparser.RawConfigParser()
    policy_config.read(args.policy_config)
    policy.configure(policy_config)
    policy.set_device(device)

    # configure environment
    env_config = configparser.RawConfigParser()
    env_config.read(args.env_config)
    env = gym.make('CrowdSim-v0')
    env.configure(env_config)
    robot = Robot(env_config, 'robot')
    env.set_robot(robot)

    # read training parameters
    if args.train_config is None:
        parser.error('Train config has to be specified for a trainable network')
    train_config = configparser.RawConfigParser()
    train_config.read(args.train_config)
    rl_learning_rate = train_config.getfloat('train', 'rl_learning_rate')
    train_batches = train_config.getint('train', 'train_batches')
    train_episodes = train_config.getint('train', 'train_episodes')
    sample_episodes = train_config.getint('train', 'sample_episodes')
    target_update_interval = train_config.getint('train', 'target_update_interval')
    evaluation_interval = train_config.getint('train', 'evaluation_interval')
    capacity = train_config.getint('train', 'capacity')
    epsilon_start = train_config.getfloat('train', 'epsilon_start')
    epsilon_end = train_config.getfloat('train', 'epsilon_end')
    epsilon_decay = train_config.getfloat('train', 'epsilon_decay')
    checkpoint_interval = train_config.getint('train', 'checkpoint_interval')

    # configure trainer and explorer
    memory = ReplayMemory(capacity)
    model = policy.get_model()
    batch_size = train_config.getint('trainer', 'batch_size')

    if args.training_method == 'v_learning':
        trainer = Trainer(model, memory, device, batch_size)
        explorer = Explorer(env, robot, device, memory, policy.gamma, target_policy=policy)

        # imitation learning
        if args.resume:
            if not os.path.exists(rl_weight_file):
                logging.error('RL weights does not exist')
            model.load_state_dict(torch.load(rl_weight_file))
            rl_weight_file = os.path.join(args.output_dir, 'resumed_rl_model.pth')
            logging.info('Load reinforcement learning trained weights. Resume training')
        elif os.path.exists(il_weight_file):
            model.load_state_dict(torch.load(il_weight_file))
            logging.info('Load imitation learning trained weights.')
        else:
            il_episodes = train_config.getint('imitation_learning', 'il_episodes')
            il_policy = train_config.get('imitation_learning', 'il_policy')
            il_epochs = train_config.getint('imitation_learning', 'il_epochs')
            il_learning_rate = train_config.getfloat('imitation_learning', 'il_learning_rate')
            trainer.set_learning_rate(il_learning_rate)
            if robot.visible:
                safety_space = 0
            else:
                safety_space = train_config.getfloat('imitation_learning', 'safety_space')
            il_policy = policy_factory[il_policy]()
            il_policy.multiagent_training = policy.multiagent_training
            il_policy.safety_space = safety_space
            robot.set_policy(il_policy)
            if args.debug:
                explorer.run_k_episodes(1, 'train', update_memory=True, imitation_learning=True)
            else:
                explorer.run_k_episodes(il_episodes, 'train', update_memory=True, imitation_learning=True)
            trainer.optimize_epoch(il_epochs)
            torch.save(model.state_dict(), il_weight_file)
            logging.info('Finish imitation learning. Weights saved.')
            logging.info('Experience set size: %d/%d', len(memory), memory.capacity)
        explorer.update_target_model(model)

        # reinforcement learning
        policy.set_env(env)
        robot.set_policy(policy)
        robot.print_info()
        trainer.set_learning_rate(rl_learning_rate)
        # fill the memory pool with some RL experience
        if args.resume:
            robot.policy.set_epsilon(epsilon_end)
            explorer.run_k_episodes(100, 'train', update_memory=True, episode=0)
            logging.info('Experience set size: %d/%d', len(memory), memory.capacity)
        episode = 0
        while episode < train_episodes:
            if args.resume:
                epsilon = epsilon_end
            else:
                if episode < epsilon_decay:
                    epsilon = epsilon_start + (epsilon_end - epsilon_start) / epsilon_decay * episode
                else:
                    epsilon = epsilon_end
            robot.policy.set_epsilon(epsilon)

            # evaluate the model
            if episode % evaluation_interval == 0:
                if not args.debug: # validation takes too long
                    explorer.run_k_episodes(env.case_size['val'], 'val', episode=episode)
                else:
                    pass

            # sample k episodes into memory and optimize over the generated memory
            explorer.run_k_episodes(sample_episodes, 'train', update_memory=True, episode=episode)
            trainer.optimize_batch(train_batches)
            episode += 1

            if episode % target_update_interval == 0:
                explorer.update_target_model(model)
            if args.debug: # so that we can use the saved model to test visualization
                    torch.save(model.state_dict(), rl_weight_file)
            else:
                if episode != 0 and episode % checkpoint_interval == 0:
                    torch.save(model.state_dict(), rl_weight_file)

        # final test
        explorer.run_k_episodes(env.case_size['test'], 'test', episode=episode)
    elif args.training_method == 'ddqn':
        trainer = Trainer(model, memory, device, batch_size, dqn = True, gamma = policy.gamma)
        explorer = Explorer(env, robot, device, memory, policy.gamma, target_policy=policy)

        if args.resume:
            if not os.path.exists(rl_weight_file):
                logging.error('RL weights does not exist')
            model.load_state_dict(torch.load(rl_weight_file, map_location = device))
            rl_weight_file = os.path.join(args.output_dir, 'resumed_rl_model.pth')
            logging.info('Load reinforcement learning trained weights. Resume training')
            with open(log_file, 'r') as file:
                log = file.read()

            train_pattern = r"TRAIN in episode (?P<episode>\d+) has success rate: (?P<sr>[0-1].\d+), " \
                            r"collision rate: (?P<cr>[0-1].\d+), nav time: (?P<time>\d+.\d+), " \
                            r"total reward: (?P<reward>[-+]?\d+.\d+)"
            train_episode = []
    
            for r in re.findall(train_pattern, log):
                train_episode.append(int(r[0]))
                

        trainer.init_target_model(model)
        # explorer.update_target_model(model)

        # reinforcement learning
        best_success_rate = 0
        sr = 0
        policy.set_env(env)
        robot.set_policy(policy)
        robot.print_info()
        trainer.set_learning_rate(rl_learning_rate, args.optimizer)
        # fill the memory pool with some RL experience
        if args.resume:
            robot.policy.set_epsilon(epsilon_end)
            explorer.run_k_episodes(100, 'train', update_memory=True, episode=0, dqn = True)
            logging.info('Experience set size: %d/%d', len(memory), memory.capacity)
        else:
            robot.policy.set_epsilon(epsilon_start)
            explorer.run_k_episodes(batch_size, 'train', update_memory=True, episode=0, dqn = True)
            logging.info('Experience set size: %d/%d', len(memory), memory.capacity)
        episode = 0

        if args.resume:
            episode = train_episode[-1] + 1
        while episode < train_episodes:
            if args.resume:
                epsilon = epsilon_end
            else:
                if episode < epsilon_decay:
                    epsilon = epsilon_start + (epsilon_end - epsilon_start) / epsilon_decay * episode
                else:
                    epsilon = epsilon_end
            robot.policy.set_epsilon(epsilon)

            # evaluate the model
            if episode % evaluation_interval == 0:
                if not args.debug: # validation takes too long
                    sr = explorer.run_k_episodes(env.case_size['val'], 'val', episode=episode, dqn = True)
                else:
                    pass

            # sample k episodes into memory and optimize over the generated memory
            explorer.run_k_episodes(sample_episodes, 'train', update_memory=True, episode=episode, dqn = True)
            trainer.optimize_batch(train_batches)
            episode += 1

            if args.debug: # so that we can use the saved model to test visualization
                torch.save(model.state_dict(), rl_weight_file)
            else:
                if episode != 0 and episode % checkpoint_interval == 0:
                    torch.save(model.state_dict(), rl_weight_file)
                    if sr > best_success_rate:
                        torch.save(model.state_dict(), best_rl_weight_file)
                        best_success_rate = sr

        # final test
        explorer.run_k_episodes(env.case_size['test'], 'test', episode=episode, dqn = True)


if __name__ == '__main__':
    main()
