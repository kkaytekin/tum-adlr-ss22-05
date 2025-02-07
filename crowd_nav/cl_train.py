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
from crowd_nav.utils.cl_explorer import Explorer
from crowd_nav.policy.policy_factory import policy_factory
from crowd_nav.args import Parser


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
    # curriculum learning
    env.configure_cl(train_config)

    # mode = train_config.get('curriculum', 'increase_obst_radius') # 'increasing_obst_num','single obstacle in the middle
    # radius_start = train_config.getfloat('curriculum','radius_start')
    # radius_max = train_config.getfloat('curriculum','radius_max')
    # radius_increment = train_config.getfloat('curriculum','radius_increment')
    # largest_obst_ratio = train_config.getfloat('curriculum','largest_obst_ratio')
    # level_up_mode = train_config.get('curriculum', 'level_up_mode')
    success_rate_milestone = train_config.getfloat('curriculum','success_rate_milestone')
    success_rate_window_size = train_config.getint('curriculum','success_rate_window_size')
    # p_handcrafted = train_config.getfloat('curriculum','p_handcrafted')
    # p_hard_deck = train_config.getfloat('curriculum','p_hard_deck')
    # hard_deck_cap = train_config.getint('curriculum','hard_deck_cap')

    # configure trainer and explorer
    memory = ReplayMemory(capacity)
    model = policy.get_model()
    batch_size = train_config.getint('trainer', 'batch_size')
    trainer = Trainer(model, memory, device, batch_size)
    explorer = Explorer(env, robot, device, memory, policy.gamma, target_policy=policy,
                        success_rate_milestone=success_rate_milestone,
                        success_rate_window_size=success_rate_window_size)

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
        robot.policy.set_epsilon(epsilon_end) # todo: make curriculum learning resumable
        # problem with this approach: epsilon_end is read from config. ideally, when the training is broken, we should
        # save epsilon_end, current episode number and others in a separate file and read everything from there.
        # until its implemented, we assume there is no "resume" option for curriculum learning
        # todo: understand the motivation behind running 100 episodes like this
        explorer.run_k_episodes(100, 'train', update_memory=True, episode=0)
        logging.info('Experience set size: %d/%d', len(memory), memory.capacity)
    # else: # this else statement is part of the above todo.


    episode = 0
    last_level_up = 0
    current_level = 0
    successes = 0
    fails = 0
    # max_level = (radius_max - radius_start) / radius_increment
    level_starts = {current_level:episode}
    while episode < train_episodes:
        if args.resume:
            epsilon = epsilon_end
        else:
            epsilon = epsilon_start + ( (epsilon_end - epsilon_start) / epsilon_decay ) * (episode - last_level_up)
            if epsilon < epsilon_end:
                epsilon = epsilon_end
        robot.policy.set_epsilon(epsilon)

        # evaluate the model
        if episode % evaluation_interval == 0:
            if not args.debug: # validation takes too long
                explorer.run_k_episodes(env.case_size['val'], 'val', episode=episode,epsilon=epsilon)
            else:
                pass

        # sample k episodes into memory and optimize over the generated memory
        # hack: sample_episodes = 1 by default. so we calculate the success rate for level increase
        # outside the run_k_episodes. reason: increasing sample_episodes lead to a nasty bug.

        level_up = explorer.run_k_episodes(sample_episodes, 'train', update_memory=True, episode=episode, epsilon=epsilon)

        trainer.optimize_batch(train_batches)
        episode += 1

        if episode % target_update_interval == 0:
            explorer.update_target_model(model)
        if args.debug: # so that we can use the saved model to test visualization
                torch.save(model.state_dict(), rl_weight_file)
        else:
            if episode != 0 and episode % checkpoint_interval == 0:
                torch.save(model.state_dict(), rl_weight_file)

        if level_up:
            if explorer.increase_cl_level():
                last_level_up = episode
                current_level += 1
                level_starts[current_level] = last_level_up
                logging.info('Level %d starts at episode: %d Epsilon value: %f', current_level, last_level_up,epsilon)

    # final test
    explorer.run_k_episodes(env.case_size['test'], 'test', episode=episode)
    # log level ups:
    for key, value in level_starts:
        logging.info('Level %d started at episode: %d',key,value)


if __name__ == '__main__':
    main()
