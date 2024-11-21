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

        print(f'Updates at t={t}: stats={json.dumps(stats, indent=4)}, updates={json.dumps(updates, indent=4)}')
        return updates
    
def benchmark(output_dir, namespace, nodes, deploy, teardown, scalers, tower):
    node_sockets = {}
    for node, node_components in nodes.items():
        node_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        node_socket.connect((node, PORT))
        node_sockets[node] = node_socket.makefile('rw')
        node_sockets[node].write(json.dumps({
            'method': 'start',
            'namespace': namespace,
            'components': node_components,
            'scalers': {i: scalers[i] for i in node_components if i in scalers},
        }) + '\n')
        node_sockets[node].flush()
    for node_socket in node_sockets.values():
        line = node_socket.readline()
        data = json.loads(line)
        assert data['ok']

    time_ = datetime.datetime.utcnow().isoformat() + 'Z'
    temp_dir = pathlib.Path('tmp')/time_
    temp_dir.mkdir(parents=True, exist_ok=True)

    time.sleep(1)
    stats_history = collections.defaultdict(list)
    try:
        time_base = time.time()
        monotonic_base = time.time() - time.perf_counter()
        while True:
            t = time.perf_counter()
            tt = (-t) % 1
            t += tt
            time.sleep(tt)
            
            stats = collections.defaultdict(dict)
            try:
                mock_stats = {'target': 0}
                stats['_tower'] = mock_stats
            except Exception:
                traceback.print_exc()

            print(f'At t={t}, tower see stats={json.dumps(stats, indent=4)}')
            do_tower = False
            if stats:
                do_tower = True
                print(f'At={t}, fetch all stats')
                for node_socket in node_sockets.values():
                    node_socket.write(json.dumps({
                        'method': 'stats',
                    }) + '\n')
                    node_socket.flush()
                local_stats = {}
                for node_socket in node_sockets.values():
                    line = node_socket.readline()
                    data = json.loads(line)
                    assert data['ok']
                    local_stats.update(data['stats'])
                print(f'At={t}, tower got stats {json.dumps(data["stats"], indent=4)}')
                allocation = 0
                for component in scalers:
                    l = [i[1]['scaler.limit'] for i in local_stats[component]]
                    if l:
                        allocation += sum(l) / len(l)
                    else:
                        print('empty local stats')
                        do_tower = False
                stats['_tower']['allocation'] = allocation
                print(f'At={t}, fetch all stats done')

            if do_tower:
                print(f'At={t}, call tower')
                tower_updates = tower(t, stats, scalers)
                if tower_updates:
                    print('tower update')
                    for node_socket in node_sockets.values():
                        node_socket.write(json.dumps({
                            'method': 'update',
                            'update': tower_updates,
                        }) + '\n')
                        print(f'At={t}, tower writing update={json.dumps(tower_updates, indent=4)}')
                        node_socket.flush()
                    for node_socket in node_sockets.values():
                        line = node_socket.readline()
                        data = json.loads(line)
                        assert data['ok']
                    print('tower update done')

            if '_tower' in stats:
                print(json.dumps(stats['_tower'], indent=4))
            for name in stats:
                stats_history[name].append((t + monotonic_base, stats[name]))
    except Exception as e:
        traceback.print_exc()
        raise e
 
    # for node_socket in node_sockets.values():
    #     node_socket.write(json.dumps({
    #         'method': 'stop',
    #     }) + '\n')
    #     node_socket.flush()
    # for node_socket in node_sockets.values():
    #     line = node_socket.readline()
    #     data = json.loads(line)
    #     assert data['ok']
    #     for k, v in data['stats'].items():
    #         assert k not in stats_history
    #         stats_history[k] = v

    # teardown()
    # print('finished')
    # return True

def application(name, nodes, target1components, deploy, teardown):
    namespace = name
    components = sorted(sum(nodes.values(), []))
    tower_targets = [0.0, 0.02, 0.04, 0.06, 0.1, 0.15, 0.2, 0.25, 0.3]  # see section 4 in the paper
    initial_limit = 1

    # see section A.7 in the paper for the warmup process
    for i in range(1):
        path = f'data/{name}/autothrottle-warmup/a{i + 1}'
        if benchmark(
            output_dir=path,
            namespace=namespace,
            nodes=nodes,
            deploy=deploy,
            teardown=teardown,
            scalers={i: {'type': 'captain', 'params': (0.0, initial_limit)} for i in components},
            tower=VwTower(
                scaler='captain',
                targets=tower_targets,
                target1components=target1components,
                slo=0.2,
                samples=[],
            ),
        ):
            print(f'Benchmark done!')

def nextflow():
    def deploy():
        print('ON DEPLOY')
        # kubectl_apply(['social-network/1.json', 'social-network/2.json'], 'social-network', 29)
        time.sleep(3)
        # populate the database, see section A.7 in the paper
        # subprocess.run([sys.executable, 'social-network/src/scripts/setup_social_graph_init_data_sync.py'], check=True)

    def teardown():
        # kubectl_delete(['social-network/1.json', 'social-network/2.json'], 'social-network')
        print('On TEARDOWN')

    def get_running_containers(root_dir: str):
        # traverse root directory, and list directories as dirs and files as files
        ctrs: List[str] = []
        for root, dirs, files in os.walk(f"{root_dir}"):
            path = root.split(os.sep)
            print((len(path) - 1) * '---', os.path.basename(root))
            for d in dirs:
                if d.startswith('docker-') and d.endswith('.scope'):
                    ctrs.append(d)

        return ctrs

    running_ctrs: List[str] = []
    while len(running_ctrs) == 0:
        running_ctrs = get_running_containers(f'/sys/fs/cgroup/system.slice')
        print(f'Waiting for containers to start ...')
        time.sleep(1)
    application(
        name='nextflow',
        nodes={
            'localhost': running_ctrs
            # 'autothrottle-4': [
            # ],
            # 'autothrottle-5': [  # see section A.3 in the paper
            # ],
        },
        target1components={
            running_ctrs[0],
            # 'media-filter-service-2',
            # 'media-filter-service-3',
        },
        deploy=deploy,
        teardown=teardown,
    )

nextflow()
# stub()