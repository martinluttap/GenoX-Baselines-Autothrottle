import logging

import collections
import dataclasses
import datetime
import json
import lzma
import numpy
import os
import pathlib
import random
import socket
import subprocess
import sys
import time
import traceback
import vowpalwabbit

from typing import Any, Callable, Dict, List, Set, Tuple

logging.basicConfig(
    filename='master-cgv2.log',
    filemode='a',
    format='%(asctime)s %(levelname)s %(message)s',
    level=logging.INFO
)
PORT = int(sys.argv[1])
assert PORT, 'port not provided'

class VwTower:
    learning_rate = 0.5

    def __init__(self, scaler, targets, target1components, slo, samples=(), explore=0.1, drop_samples=0, aggregate_samples=20):
        self.scaler = scaler
        self.targets = targets
        self.target1components = target1components
        self.slo = slo
        self.samples = list(samples)
        self.explore = explore
        self.drop_samples = drop_samples
        self.aggregate_samples = aggregate_samples
        self.last_rps = None
        self.last_action = None
        self.last_action_p = None

    def __call__(self, t, stats, scalers):
        if self.last_rps is not None:
            if self.drop_samples:
                self.drop_samples -= 1
            else:
                latency = 200
                allocation = 0.5
                # latency = stats['_tower']['p99_latency']
                # allocation = stats['_tower']['allocation']
                self.samples.append((self.last_rps, self.last_action, self.last_action_p, latency, allocation))

        train_samples = list(self.samples)

        try:
            min_allocation = min(i[4] for i in train_samples if i[3] <= self.slo)
            max_allocation = max(i[4] for i in train_samples if i[3] <= self.slo)
        except ValueError:
            min_allocation = None
            max_allocation = None
        try:
            min_latency = min(i[3] for i in train_samples if i[3] > self.slo)
            max_latency = max(i[3] for i in train_samples if i[3] > self.slo)
        except ValueError:
            min_latency = None
            max_latency = None
        for i, (rps, action, action_p, latency, allocation) in enumerate(train_samples):
            if latency <= self.slo:
                try:
                    cost = (allocation - min_allocation) / (max_allocation - min_allocation)
                except ZeroDivisionError:
                    cost = 0.5
            else:
                try:
                    cost = (latency - min_latency) / (max_latency - min_latency) + 2
                except ZeroDivisionError:
                    cost = 2.5
            train_samples[i] = (rps, action, action_p, cost)

        def median(l):
            l = sorted(l)
            if not l:
                return None
            if len(l) % 2:
                return l[len(l) // 2]
            else:
                return (l[len(l) // 2 - 1] + l[len(l) // 2]) / 2
        sample_categories = collections.defaultdict(lambda: collections.defaultdict(list))
        for i in train_samples:
            action = i[1]
            rps = round(i[0] / self.aggregate_samples) * self.aggregate_samples
            sample_categories[action][rps].append(i)
        aggregated_samples = []
        for action in sample_categories:
            for rps in sample_categories[action]:
                aggregated_samples.append((rps, action, 1 / len(self.targets) ** 2, median(i[3] for i in sample_categories[action][rps])))
        train_samples = []
        if aggregated_samples:
            for i in range(10000):
                train_samples.append(random.choice(aggregated_samples))

        vw = vowpalwabbit.Workspace(f'--cb_explore {len(self.targets) ** 2} --epsilon 0 -l {self.learning_rate} --nn 3 --quiet')
        for rps, action, action_p, cost in train_samples:
            vw.learn(f'{action+1}:{cost}:{action_p} | rps:{rps}')

        # rps = stats['_tower']['rps']
        # distribution = vw.predict(f'| rps:{rps}')
        # action = numpy.random.choice(len(distribution), p=numpy.array(distribution) / sum(distribution))
        # action_p = distribution[action]

        vw.finish()

        # if action_p == 1:
        #     stats['_tower']['explore'] = action
        #     distribution = [0] * len(self.targets) ** 2
        #     distribution[action] += 1 - self.explore
        #     explore_actions = []
        #     x = action // len(self.targets)
        #     y = action % len(self.targets)
        #     if x - 1 >= 0:
        #         explore_actions.append(action - len(self.targets))
        #     if x + 1 < len(self.targets):
        #         explore_actions.append(action + len(self.targets))
        #     if y - 1 >= 0:
        #         explore_actions.append(action - 1)
        #     if y + 1 < len(self.targets):
        #         explore_actions.append(action + 1)
        #     for i in explore_actions:
        #         distribution[i] += self.explore / len(explore_actions)
        #     action = numpy.random.choice(len(distribution), p=numpy.array(distribution) / sum(distribution))
        #     action_p = distribution[action]

        # stats['_tower']['action'] = action
        # stats['_tower']['action_p'] = action_p
        # All stub values here
        rps = 100
        action = 1
        action_p = 0.5
        self.last_rps = rps
        self.last_action = action
        self.last_action_p = action_p

        # target1 = self.targets[action // len(self.targets)]
        # target2 = self.targets[action % len(self.targets)]
        target1 = 0.0
        target2 = 0.0
        updates = {}
        for k, v in scalers.items():
            if v['type'] == self.scaler:
                if k in self.target1components:
                    updates[k] = (target1,)
                else:
                    updates[k] = (target2,)

        logging.info(f'Updates at t={t}: stats={json.dumps(stats, indent=4)}, updates={json.dumps(updates, indent=4)}')
        return updates
    
def benchmark(output_dir, namespace, nodes, scalers, tower):
    # Initialize direct container management (no agent communication needed)
    all_components = []
    for node, node_components in nodes.items():
        logging.info(f'Managing {len(node_components)} containers on {node}')
        all_components.extend(node_components)
    
    logging.info(f'Direct container management initialized for {len(all_components)} containers')

    # Main control loop - continuously adjust CPU limits based on SLO targets
    logging.info('Starting autothrottle control loop')
    
    try:
        while True:
            t = time.perf_counter()
            time.sleep(5)  # Check every 5 seconds
            
            # Simulate container stats (in real scenario, would read from cgroups)
            active_components = len(all_components)
            avg_allocation = 0.5  # Mock allocation value
            
            # Create mock stats for tower (simulating SLO metrics)
            stats = {
                '_tower': {
                    'allocation': avg_allocation,
                    'active_containers': active_components,
                    'timestamp': t
                }
            }
            
            # Get CPU limit updates from tower based on SLO targets
            tower_updates = tower(t, stats, scalers)
            if tower_updates:
                logging.info(f'Would apply updates to {len(tower_updates)} containers: {list(tower_updates.keys())[:3]}...')
                # In real scenario, would apply CPU limits directly to cgroup files
                for container_id, limits in tower_updates.items():
                    logging.info(f'Container {container_id}: new limit={limits}')
            
            # Log current state every cycle
            logging.info(f'Active containers: {active_components}, Avg allocation: {avg_allocation:.3f}, Updates: {len(tower_updates) if tower_updates else 0}')
                
    except KeyboardInterrupt:
        logging.info('Shutting down autothrottle')
    except Exception as e:
        logging.error(f'Error in control loop: {e}')
        raise

def application(name, nodes, target1components):
    namespace = name
    components = sorted(sum(nodes.values(), []))
    tower_targets = [0.0, 0.02, 0.04, 0.06, 0.1, 0.15, 0.2, 0.25, 0.3]
    initial_limit = 1

    logging.info(f'Starting autothrottle for {len(components)} containers')
    
    # Core autothrottle mechanism - continuous CPU limit adjustment
    benchmark(
        output_dir=None,
        namespace=namespace,
        nodes=nodes,
        scalers={i: {'type': 'captain', 'params': (0.0, initial_limit)} for i in components},
        tower=VwTower(
            scaler='captain',
            targets=tower_targets,
            target1components=target1components,
            slo=0.2,  # 200ms SLO target
            samples=[],
        ),
    )

def nextflow():
    def find_container_cgroups():
        """
        Return a list of all docker-<containerid>.scope names running on the node (cri-docker, cgroup v2).
        """
        import pathlib
        cgroupv2_base = pathlib.Path('/sys/fs/cgroup')
        containers = []
        for scope in cgroupv2_base.glob('**/docker-*.scope'):
            containers.append(scope.name)
        for scope in cgroupv2_base.glob('**/cri-containerd-*.scope'):
            containers.append(scope.name)
        return containers

    # Find running containers
    running_ctrs: List[str] = []
    while len(running_ctrs) == 0:
        running_ctrs = find_container_cgroups()
        if not running_ctrs:
            logging.info(f'Waiting for containers to start ...')
            time.sleep(1)
    
    logging.info(f'Found {len(running_ctrs)} containers: {running_ctrs[:3]}...')
    
    # Run the core autothrottle mechanism
    application(
        name='nextflow',
        nodes={
            'localhost': running_ctrs
        },
        target1components=set(running_ctrs[:len(running_ctrs)//2]),  # Use half as target1
    )

nextflow()
# stub()